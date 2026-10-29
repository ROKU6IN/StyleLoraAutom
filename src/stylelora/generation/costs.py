"""F04 dispatch boundary and C06 local estimates. No network or billing claims."""
from copy import deepcopy
from dataclasses import dataclass
import re
from uuid import uuid4

from ..exchange.common import project_operation
from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import sources
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, encode, relative_path
from ..runtime.contracts import strict_json, Versions
from ..runtime.errors import ServiceError
from .profile import build_request, locked_projection

MAX_ESTIMATE = 9007199254740991
MAX_RECORD = 8192
MAX_ATTEMPTS = 100000


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _amount(value):
    return type(value) is int and 0 <= value <= MAX_ESTIMATE


def _path(project, item_id, attempt):
    if type(item_id) is not int or item_id < 1 or type(attempt) is not int or attempt < 1:
        raise ServiceError(422, 'cost:invalid_attempt')
    return relative_path(project.root, f'images/.costs/{item_id}/{attempt}.json')


def _read(project, item_id, attempt):
    try:
        path = _path(project, item_id, attempt)
        if not path.exists():
            return None
        with path.open('rb') as stream:
            data = stream.read(MAX_RECORD + 1)
        if len(data) > MAX_RECORD:
            raise ValueError()
        envelope = strict_json(data)
        row = envelope['content']
        if set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(row):
            raise ValueError()
        if (set(row) != {'schema_version', 'source_id', 'item_id', 'attempt', 'cycle_id', 'operation_key',
                        'request_digest', 'config_digest', 'instance_id', 'contract_digest', 'estimated_anlas',
                        'policy', 'outcome', 'evidence_digest'}
                or type(row['schema_version']) is not int or row['schema_version'] not in (1, 2)
                or type(row['source_id']) is not int or row['source_id'] < 1
                or type(row['item_id']) is not int or row['item_id'] != item_id
                or type(row['attempt']) is not int or row['attempt'] != attempt
                or not isinstance(row['cycle_id'], str) or not re.fullmatch('[0-9a-f]{32}', row['cycle_id'])
                or not all(_hash(row[k]) for k in ('operation_key', 'request_digest', 'config_digest', 'instance_id', 'contract_digest'))
                or not (_amount(row['estimated_anlas']) or row['schema_version'] == 2 and row['estimated_anlas'] is None)
                or row['policy'] not in ('deny', 'ask', 'allow')
                or row['outcome'] not in ('intent', 'unknown', 'submitted', 'rejected')):
            raise ValueError()
        if row['outcome'] in ('submitted', 'rejected'):
            if not _hash(row['evidence_digest']):
                raise ValueError()
        elif row['evidence_digest'] is not None:
            raise ValueError()
        return row
    except Exception:
        raise ServiceError(503, 'cost:invalid_receipt') from None


def _write(project, row, previous):
    try:
        if _read(project, row['item_id'], row['attempt']) != previous:
            raise ServiceError(409, 'cost:receipt_changed')
        path = _path(project, row['item_id'], row['attempt'])
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'content': row, 'digest': digest(row)}
        if len(encode(payload).encode('utf-8')) > MAX_RECORD:
            raise ValueError()
        atomic_json(path, payload)
    except Exception:
        # An exception after replace must not cause a second dispatch or overwrite.
        try:
            if _read(project, row['item_id'], row['attempt']) == row:
                return
        except ServiceError:
            pass
        raise ServiceError(503, 'cost:receipt_write_failed', outcome='unknown') from None


@dataclass(frozen=True)
class CostEvidence:
    """Only a reviewed adapter may attest an exact attempt's outcome, never HTTP input.

    rejected proves no generation/charge for this attempt (not just a timeout).
    submitted proves dispatch; it does not prove the amount actually charged.
    """
    operation_key: str
    instance_id: str
    contract_digest: str
    outcome: str
    evidence_digest: str


class CostService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = {}

    @property
    def coordinator(self):
        return self.runtime.generation_sources

    def state(self, context, source_id):
        with self.coordinator.lock:
            def snapshot(db):
                source = self.coordinator._source(db, source_id)
                view = self.runtime.dataset.source_view(context, db, source_id)
                return source, view, deepcopy(context.handle._cycles.get(source_id))
            source, view, cycle = submit_write(context.handle, snapshot).result()
            pending = self.pending.get((context.project_id, source_id))
            if not cycle or not pending or pending['cycle_id'] != cycle['id'] or not cycle['pending_cost']:
                pending = None
            applicable = source.status == 'paused' and source.cost_policy == 'ask' and pending is not None
            blocked = source.config_json.get('backend') != 'nai_direct' and self.runtime.settings_leases.status()['blocked']
            driver = self.coordinator.driver_for(source)
            return {'source_id': source_id, 'status': source.status, 'cost_policy': source.cost_policy,
                    'source_version': view['version'], 'cycle_id': cycle['id'] if cycle else None,
                    'acknowledged': bool(cycle and cycle['ack'] and source.status == 'running'),
                    'pending': deepcopy(pending), 'confirmation_required': applicable,
                    'can_confirm': applicable and driver is not None and not blocked,
                    'reason': 'profile:restore_pending' if blocked else
                        'generation:adapter_unavailable' if driver is None else None}

    def set_policy(self, context, source_id, body, expected):
        with self.coordinator.lock:
            policy = body['cost_policy']
            if policy == 'allow' and body.get('confirm_allow') is not True:
                raise ServiceError(422, 'cost:allow_confirmation_required')
            def write(db):
                source = self.coordinator._source(db, source_id)
                Versions.require(expected, self.runtime.dataset.source_view(context, db, source_id)['version'])
                result = commands.set_cost_policy(db, source_id, policy=policy)
                if result.changed:
                    if source.status == 'running':
                        commands._cycle_on_commit(db, source_id, running=True)
                    db.after_commit.append(lambda: self.pending.pop((context.project_id, source_id), None))
                    db.after_commit.append(lambda: self.runtime.versions.bump(self.runtime.dataset.key(context, 'source', source_id)))
            submit_write(context.handle, write).result()
            return self.coordinator._emit(context, source_id)

    def confirm(self, context, source_id, body, expected):
        with self.coordinator.lock:
            state = self.state(context, source_id)
            Versions.require(expected, state['source_version'])
            if state['cost_policy'] == 'deny':
                raise ServiceError(409, 'cost:denied')
            if not state['confirmation_required'] or state['cycle_id'] != body['cycle_id']:
                raise ServiceError(409, 'cost:cycle_expired')
            with read_snapshot(context.handle) as db:
                source = self.coordinator._source(db, source_id)
            self.coordinator._gate(source)
            driver = self.coordinator.driver_for(source)
            if driver.is_active(context, source_id):
                raise ServiceError(409, 'source:in_flight')
            try:
                commands.confirm_cost_and_resume(context.handle, source_id, expected_cycle_id=body['cycle_id'],
                    validate=lambda db: Versions.require(expected, self.runtime.dataset.source_view(context, db, source_id)['version']))
            except Exception:
                self.coordinator._pause(context, source_id, 'cost:confirmation_unknown')
                raise
            self.pending.pop((context.project_id, source_id), None)
            try:
                driver.dispatch(context, source_id)
            except Exception:
                self.coordinator._pause(context, source_id, 'generation:dispatch_unknown')
            else:
                self.coordinator.reasons.pop((context.project_id, source_id), None)
            return self.coordinator._emit(context, source_id)

    def reserve_dispatch(self, context, item_id, *, estimated_anlas, expected_cycle_id, instance_id, contract_digest,
                         unknown_cost=False):
        """After fresh prepare, before remote submit. A successful return is ONE dispatch grant.

        Never retry this method to obtain another grant for a sent attempt. A DB
        commit exception is unknown even if sent is subsequently observed.
        """
        project = context.handle
        with self.coordinator.lock, project_operation(project):
            def inspect(db):
                item = sources.get_item(db, item_id)
                source = self.coordinator._source(db, item.source_id)
                cycle = project._cycles.get(source.source_id)
                if (source.status != 'running' or item.status != 'pending' or not cycle
                        or cycle['id'] != expected_cycle_id or sources.count_item_states(db, source.source_id)['sent']):
                    raise ServiceError(409, 'cost:cycle_expired')
                return source, item, deepcopy(cycle)
            source, item, cycle = submit_write(project, inspect).result()
            unknown = unknown_cost is True and estimated_anlas is None and source.config_json.get('backend') == 'nai_direct'
            if not _amount(estimated_anlas) and not unknown:
                self.coordinator._pause(context, source.source_id, 'cost:invalid_estimate')
                raise ServiceError(422, 'cost:invalid_estimate')
            if not _hash(instance_id) or not _hash(contract_digest):
                raise ServiceError(422, 'cost:invalid_identity')
            decision = commands.evaluate_cost(project, source.source_id, estimated_anlas=estimated_anlas, unknown_cost=unknown)
            if decision == 'paused':
                token = commands.cost_cycle_id(project, source.source_id)
                with read_snapshot(project) as db:
                    policy = sources.get_source(db, source.source_id).cost_policy
                self.pending[(context.project_id, source.source_id)] = {
                    'cycle_id': token, 'item_id': item_id, 'estimated_anlas': estimated_anlas}
                self.coordinator.reasons[(context.project_id, source.source_id)] = 'cost:' + policy
                self.coordinator._emit(context, source.source_id)
                return None
            attempt = max(1, item.attempts)
            try:
                previous = _read(project, item_id, attempt)
            except ServiceError:
                self.coordinator._pause(context, source.source_id, 'cost:invalid_receipt')
                raise
            if previous is not None and previous['outcome'] != 'intent':
                raise ServiceError(409, 'cost:attempt_reserved')
            if previous is not None and (previous['source_id'] != source.source_id
                    or previous['request_digest'] != digest(build_request(source, item))
                    or previous['config_digest'] != digest(locked_projection(source))):
                self.coordinator._pause(context, source.source_id, 'cost:invalid_receipt')
                raise ServiceError(409, 'cost:receipt_changed')
            row = {'schema_version': 2 if unknown else 1, 'source_id': source.source_id, 'item_id': item_id, 'attempt': attempt,
                   'cycle_id': cycle['id'], 'operation_key': digest(uuid4().hex),
                   'request_digest': digest(build_request(source, item)), 'config_digest': digest(locked_projection(source)),
                   'instance_id': instance_id, 'contract_digest': contract_digest, 'estimated_anlas': estimated_anlas,
                   'policy': source.cost_policy, 'outcome': 'intent', 'evidence_digest': None}
            try:
                _write(project, row, previous)  # fsync before mark_sent and any remote dispatch.
            except ServiceError:
                self.coordinator._pause(context, source.source_id, 'cost:receipt_write_failed')
                raise
            def commit(db):
                current, actual, active = inspect(db)
                if (current.cost_policy != source.cost_policy or active != cycle or actual.attempts != item.attempts
                        or _read(project, item_id, attempt) != row
                        or digest(build_request(current, actual)) != row['request_digest']
                        or digest(locked_projection(current)) != row['config_digest']):
                    raise ServiceError(409, 'cost:cycle_expired')
                return commands.mark_sent(db, item_id).value
            try:
                submit_write(project, commit).result()
            except Exception:
                self.coordinator._pause(context, source.source_id, 'cost:dispatch_commit_unknown')
                raise ServiceError(503, 'cost:dispatch_commit_unknown', outcome='unknown') from None
            self.coordinator._emit(context, source.source_id)
            return deepcopy(row)

    def observe(self, context, item_id, attempt, evidence):
        """Persist reviewed adapter evidence; no retry, generation, or billing API."""
        project = context.handle
        with self.coordinator.lock, project_operation(project):
            row = _read(project, item_id, attempt)
            if (row is None or not isinstance(evidence, CostEvidence) or evidence.outcome not in ('submitted', 'rejected')
                    or not _hash(evidence.evidence_digest)
                    or any(getattr(evidence, k) != row[k] for k in ('operation_key', 'instance_id', 'contract_digest'))):
                raise ServiceError(409, 'cost:evidence_mismatch')
            with read_snapshot(project) as db:
                item = sources.get_item(db, item_id)
                source = self.coordinator._source(db, item.source_id)
            if (source.source_id != row['source_id'] or digest(locked_projection(source)) != row['config_digest']
                    or digest(build_request(source, item)) != row['request_digest'] or attempt > item.attempts
                    or (attempt == item.attempts and item.status == 'pending')):
                raise ServiceError(409, 'cost:evidence_mismatch')
            updated = {**row, 'outcome': evidence.outcome, 'evidence_digest': evidence.evidence_digest}
            if row['outcome'] in ('submitted', 'rejected') and row != updated and not (
                    row['outcome'] == 'submitted' and evidence.outcome == 'rejected'):
                raise ServiceError(409, 'cost:outcome_conflict')
            if evidence.outcome == 'rejected' and attempt == item.attempts and item.status == 'done':
                raise ServiceError(409, 'cost:outcome_conflict')
            _write(project, updated, row)
            # Outcome evidence is accounting only. The runner must settle the item
            # and stop/pause using the same proof; this method never fabricates it.
            self.coordinator._emit(context, source.source_id)

    def summary(self, context, source_id):
        project = context.handle
        with self.coordinator.lock, project_operation(project):
            with read_snapshot(project) as db:
                source = self.coordinator._source(db, source_id)
                items = [sources.get_item(db, row[0]) for row in db.execute('SELECT item_id FROM source_items WHERE source_id=? ORDER BY item_id', (source_id,))]
            counts = {k: 0 for k in ('submitted', 'unknown', 'rejected', 'missing', 'invalid', 'unscanned')}
            amounts = {k: 0 for k in ('submitted', 'unknown', 'rejected')}
            scanned = 0
            unpriced = 0
            unpriced_outcomes = set()
            for item in items:
                total = max(0, item.attempts - (1 if item.status == 'pending' and item.attempts else 0))
                for attempt in range(1, min(total, max(0, MAX_ATTEMPTS-scanned)) + 1):
                    scanned += 1
                    try:
                        row = _read(project, item.item_id, attempt)
                        if row is None:
                            counts['missing'] += 1
                            continue
                        if (row['source_id'] != source_id or row['config_digest'] != digest(locked_projection(source))
                                or row['request_digest'] != digest(build_request(source, item))):
                            raise ValueError()
                        outcome = row['outcome'] if row['outcome'] in ('submitted', 'rejected') else 'unknown'
                        counts[outcome] += 1
                        if row['estimated_anlas'] is None:
                            unpriced += 1
                            unpriced_outcomes.add(outcome)
                        else:
                            amounts[outcome] += row['estimated_anlas']
                    except Exception:
                        counts['invalid'] += 1
            # Count expected attempts independently so a bounded scan cannot claim completeness.
            expected = sum(max(0, i.attempts - (1 if i.status == 'pending' and i.attempts else 0)) for i in items)
            counts['unscanned'] = expected - scanned
            complete = not (counts['missing'] or counts['invalid'] or counts['unscanned'] or unpriced)
            observed = amounts['submitted'] + amounts['unknown']
            overflow = any(v > MAX_ESTIMATE for v in (*amounts.values(), observed))
            return {'source': 'local_estimate', 'estimate_scope': 'persisted_attempts', 'source_id': source_id,
                    'balance': None, 'subscription': None, 'stamina': None, 'actual_charged_anlas': None,
                    'estimated_anlas': observed if complete and not overflow else None,
                    'observed_estimated_anlas': observed if not overflow else None,
                    'estimate_complete': complete and not overflow, 'amount_overflow': overflow,
                    'submitted_estimated_anlas': amounts['submitted'] if not overflow and 'submitted' not in unpriced_outcomes else None,
                    'unknown_estimated_anlas': amounts['unknown'] if not overflow and 'unknown' not in unpriced_outcomes else None,
                    'rejected_estimated_anlas': amounts['rejected'] if not overflow and 'rejected' not in unpriced_outcomes else None,
                    'counts': counts, 'attempts': expected, 'unpriced_attempts': unpriced, 'zero_estimate_is_free': False}
