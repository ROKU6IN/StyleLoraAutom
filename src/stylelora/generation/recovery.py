"""Versioned, project-bound recovery admission. GET never contacts a backend."""
from uuid import uuid4

from ..project_db.connection import read_snapshot
from ..project_db.repositories import sources
from ..project_db.validation import digest, primitive
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from .costs import _read as read_cost
from .operations import read_operation
from .profile import build_request, locked_projection


class RecoveryService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pending = None

    @property
    def coordinator(self):
        return self.runtime.generation_sources

    def _driver(self, source=None):
        driver = self.coordinator.driver_for(source) if source else self.coordinator.driver
        return driver if driver and all(callable(getattr(driver, name, None)) for name in (
            'recover_blocking', 'recovery_identity', 'recovery_busy')) else None

    def _view(self, context, source_id, item_id, *, owner=None):
        with read_snapshot(context.handle) as db:
            source = self.coordinator._source(db, source_id)
            item = sources.get_item(db, item_id)
            if item.source_id != source_id:
                raise ServiceError(404, 'resource:not_found')
        if source.config_json.get('backend') == 'nai_direct':
            return self.runtime.nai_direct.driver.recovery_view(context, source, item,
                pending=self.pending is not None and self.pending != owner)
        attempt = max(1, item.attempts)
        driver = self._driver()
        identity = driver.recovery_identity() if driver else None
        busy = bool((self.pending is not None and self.pending != owner) or (driver and driver.recovery_busy()))
        row = journal = grant = None
        reason, phase = None, 'untracked'
        reconcile = restore = False
        lease_phase = None
        try:
            row = read_operation(context.handle, item_id, attempt)
            journal = self.runtime.settings_leases._load()
            held = journal['record']
            if row is None:
                if item.status == 'sent':
                    reason, phase = 'recovery:operation_missing', 'blocked'
            else:
                if (row['source_id'] != source_id or row['request_digest'] != digest(build_request(source, item))
                        or row['config_digest'] != digest(locked_projection(source))):
                    raise ServiceError(409, 'generation:evidence_mismatch')
                bound = bool(held and held['lease_id'] == row['lease_id'] and held['source_id'] == source_id
                             and all(held['identity'][k] == row[k] for k in ('instance_id', 'contract_digest')))
                lease_phase = held['phase'] if bound else None
                if not row['closed'] and item.status != 'pending':
                    grant = read_cost(context.handle, item_id, attempt)
                    if grant is None or any(grant[k] != row[k] for k in (
                        'source_id', 'item_id', 'attempt', 'instance_id', 'contract_digest', 'request_digest', 'config_digest')):
                        raise ServiceError(409, 'recovery:cost_evidence_missing')
                if not bound:
                    phase, reason = ('settled', None) if row['closed'] else ('blocked', 'recovery:lease_unbound')
                elif held['inflight'] not in (None, row['operation_key']) or (row['closed'] and held['inflight'] is not None):
                    phase, reason = 'blocked', 'recovery:other_inflight'
                elif not row['closed'] or held['phase'] in ('applying', 'restoring'):
                    phase, reconcile = 'needs_reconcile', True
                else:
                    phase, restore = 'needs_restore', True
                if reconcile or restore:
                    if source.status == 'running':
                        reason = 'recovery:pause_required'
                    elif driver is None:
                        reason = 'generation:adapter_unavailable'
                    elif identity != {k: row[k] for k in ('instance_id', 'contract_digest')}:
                        reason = 'profile:backend_changed'
                    elif busy:
                        reason = 'recovery:busy'
                    if reason:
                        reconcile = restore = False
        except ServiceError as error:
            phase, reason = 'blocked', error.code
            reconcile = restore = False
        # Private evidence only contributes to an opaque session HMAC. No file paths,
        # prompts, operation keys, settings values or backend fingerprints leave GET.
        fingerprint = {'source': primitive(source), 'item': primitive(item), 'operation': row,
                       'journal': journal, 'cost': grant, 'adapter': identity, 'reason': reason if phase == 'blocked' else None}
        version = self.runtime.versions.get(f'{context.project_id}:generation-recovery:{source_id}:{item_id}', fingerprint)
        return {'source_id': source_id, 'item_id': item_id, 'seq': item.seq, 'attempt': item.attempts,
                'status': item.status, 'phase': phase, 'reason': reason, 'lease_phase': lease_phase,
                'busy': busy, 'can_reconcile': reconcile, 'can_restore': restore, 'version': version}

    def view(self, context, source_id, item_id):
        with self.coordinator.lock:
            return self._view(context, source_id, item_id)

    def list(self, context, source_id, *, limit=50, cursor=None, status=None):
        if not 1 <= limit <= 1000 or status not in (None, 'pending', 'sent', 'done', 'failed'):
            raise ServiceError(422, 'input:invalid')
        identity = digest([context.project_id, context.generation, 'generation-recovery', source_id, status])
        token = self.runtime.cursors.decode(cursor, identity) if cursor else None
        with self.coordinator.lock:
            with read_snapshot(context.handle) as db:
                self.coordinator._source(db, source_id)
                upper = token['upper'] if token else db.execute(
                    'SELECT COALESCE(MAX(item_id),0) FROM source_items WHERE source_id=?', (source_id,)).fetchone()[0]
                where = 'source_id=? AND item_id<=?'
                params = [source_id, upper]
                if status:
                    where += ' AND status=?'
                    params.append(status)
                total = db.execute('SELECT count(*) FROM source_items WHERE '+where, params).fetchone()[0]
                rows = db.execute('SELECT item_id FROM source_items WHERE '+where+' AND item_id>? ORDER BY item_id LIMIT ?',
                                  (*params, token['last'] if token else 0, limit+1)).fetchall()
            chosen = rows[:limit]
            items = [self._view(context, source_id, row[0]) for row in chosen]
            return {'items': items, 'limit': limit, 'total': total, 'upper_id': upper, 'has_more': len(rows)>limit,
                    'next_cursor': self.runtime.cursors.encode({'identity':identity, 'upper':upper, 'last':chosen[-1][0]})
                                   if len(rows)>limit else None}

    def start(self, context, source_id, body, expected):
        item_id, action = body['item_id'], body['action']
        if action == 'restore' and body.get('confirm_restore') is not True:
            raise ServiceError(422, 'recovery:confirmation_required')
        reservation = uuid4().hex
        def validate(owner=None):
            if context.closing or self.runtime.draining:
                raise ServiceError(409, 'project:closing')
            value = self._view(context, source_id, item_id, owner=owner)
            Versions.require(expected, value['version'])
            if not value['can_'+action]:
                raise ServiceError(409, value['reason'] or 'recovery:action_unavailable')
        with self.coordinator.lock:
            validate()
            with read_snapshot(context.handle) as db:
                source = self.coordinator._source(db, source_id)
            driver = self._driver(source)
            self.pending = reservation
            def work(task):
                # The driver checks again while acquiring its exclusive slot. A
                # changed file, source, lease or adapter invalidates a queued action.
                driver.recover_blocking(context, source_id, item_id, action, lambda: validate(reservation))
                return self.view(context, source_id, item_id)
            def release():
                with self.coordinator.lock:
                    if self.pending == reservation:
                        self.pending = None
                    self.coordinator._emit(context, source_id)
            try:
                return self.runtime.supervisor.submit('generation.'+action, work, context=context,
                                                      finalizer=release, cancelable=False)
            except BaseException:
                release()
                raise
