"""Direct HTTP attempt journal and serial runner, independent of settings leases.

A durable response hash binds bytes to one HTTP exchange. A filename alone is
never ownership evidence. An intent without that receipt stays sent/unknown.
Recovery only consumes local receipts and NEVER repeats an upstream request.
"""
import asyncio
import hashlib
import secrets
import time

from ..backends.nai_direct.profile import CONTRACT, INSTANCE, capabilities, payload
from ..project_db.connection import read_snapshot
from ..project_db.repositories import sources, images
from ..project_db.storage import atomic_bytes, atomic_json
from ..project_db.validation import digest, relative_path, primitive
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError
from .artifacts import inspect_and_prepare, register_artifact
from .costs import CostEvidence, _hash, _read as read_cost
from .profile import build_request, locked_projection, validate_capabilities
from .runner import SerialDriver


def journal_path(project, item_id, attempt):
    if any(type(v) is not int or v < 1 for v in (item_id, attempt)):
        raise ServiceError(422, 'nai:invalid_receipt')
    return relative_path(project.root, f'images/.nai-direct/{item_id}/{attempt}.json')


def read_receipt(project, item_id, attempt):
    try:
        path = journal_path(project, item_id, attempt)
        if not path.exists():
            return None
        with path.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        envelope = strict_json(raw)
        row = envelope['content']
        if (set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(row)
                or set(row) != {'schema_version', 'source_id', 'item_id', 'attempt', 'operation_key',
                               'request_digest', 'config_digest', 'contract_digest', 'instance_id',
                               'seed', 'payload_digest', 'phase', 'sha256'}
                or type(row['schema_version']) is not int or row['schema_version'] != 1
                or type(row['source_id']) is not int or row['source_id'] < 1
                or type(row['item_id']) is not int or row['item_id'] != item_id
                or type(row['attempt']) is not int or row['attempt'] != attempt
                or type(row['seed']) is not int or not 0 <= row['seed'] < 2**32
                or row['phase'] not in ('intent', 'response', 'closed')
                or not all(_hash(row[k]) for k in ('operation_key', 'request_digest', 'config_digest',
                                                 'contract_digest', 'instance_id', 'payload_digest'))
                or (row['sha256'] is not None if row['phase'] == 'intent' else not _hash(row['sha256']))):
            raise ValueError()
        return row
    except Exception:
        raise ServiceError(503, 'nai:invalid_receipt') from None


def write_receipt(project, row, previous):
    try:
        if read_receipt(project, row['item_id'], row['attempt']) != previous:
            raise ValueError()
        path = journal_path(project, row['item_id'], row['attempt'])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {'content': row, 'digest': digest(row)})
        if read_receipt(project, row['item_id'], row['attempt']) != row:
            raise ValueError()
    except Exception:
        raise ServiceError(503, 'nai:receipt_write_unknown', outcome='unknown') from None


def bound_receipt(project, source, item):
    row = read_receipt(project, item.item_id, max(1, item.attempts))
    if row is None:
        return None, None
    grant = read_cost(project, item.item_id, row['attempt'])
    if (grant is None or any(row[k] != grant[k] for k in ('source_id', 'item_id', 'attempt', 'operation_key',
                'request_digest', 'config_digest', 'contract_digest', 'instance_id'))
            or row['source_id'] != source.source_id or row['attempt'] != item.attempts
            or row['contract_digest'] != CONTRACT or row['instance_id'] != INSTANCE
            or row['config_digest'] != digest(locked_projection(source))
            or row['request_digest'] != digest(build_request(source, item))
            or row['payload_digest'] != digest(payload(source.config_json, build_request(source, item), row['seed']))):
        raise ServiceError(409, 'nai:receipt_mismatch')
    return row, grant


class DirectDriver(SerialDriver):
    def __init__(self, runtime, service):
        super().__init__(runtime, None, timeout=185, interval=1)
        self.service = service

    def capabilities(self):
        if not self.service.available():
            raise ServiceError(503, 'nai:token_missing')
        return capabilities()

    def recovery_identity(self):
        return {'instance_id': INSTANCE, 'contract_digest': CONTRACT}

    async def _cycle(self, context, source_id, stop):
        source, cycle, item = self._snapshot(context, source_id)
        if self._stopped(context, stop) or source.status != 'running' or not cycle:
            return
        validate_capabilities(source.config_json, source.negative_prompt, self.capabilities())
        while not self._stopped(context, stop):
            source, actual_cycle, item = self._snapshot(context, source_id)
            if source.status != 'running' or not actual_cycle or actual_cycle['id'] != cycle['id'] or item is None:
                break
            if item.status != 'pending':
                raise ServiceError(409, 'generation:reconcile_required')
            attempt = max(1, item.attempts)
            path = relative_path(context.handle.root, f'images/.pending/{item.item_id}/{attempt}/out.png')
            if read_receipt(context.handle, item.item_id, attempt) is not None or path.exists():
                raise ServiceError(409, 'nai:receipt_mismatch')
            request = build_request(source, item)
            seed = secrets.randbits(32)
            body = payload(source.config_json, request, seed)
            token = self.service.token()
            if self.last_submit is not None:
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_submit)))
            with self.runtime.generation_sources.lock:
                current, current_cycle, _ = self._snapshot(context, source_id)
                if (self._stopped(context, stop) or current.status != 'running'
                        or not current_cycle or current_cycle['id'] != cycle['id']):
                    break
                grant = self.runtime.generation_costs.reserve_dispatch(context, item.item_id,
                    estimated_anlas=None, unknown_cost=True, expected_cycle_id=cycle['id'],
                    instance_id=INSTANCE, contract_digest=CONTRACT)
            if grant is None:
                break
            row = {k: grant[k] for k in ('source_id', 'item_id', 'attempt', 'operation_key',
                                       'request_digest', 'config_digest', 'contract_digest', 'instance_id')}
            row.update(schema_version=1, seed=seed, payload_digest=digest(body), phase='intent', sha256=None)
            # A grant commits sent before any HTTP POST. Failures from here onward
            # stay sent. Reopening or resuming cannot obtain another grant.
            write_receipt(context.handle, row, None)
            self.last_submit = time.monotonic()
            png = await self.service.backend.generate(token, body)
            context.handle._fault('nai_after_response')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise ServiceError(409, 'artifact:save_path_exists')
            atomic_bytes(path, png)
            context.handle._fault('nai_after_save')
            response = {**row, 'phase': 'response', 'sha256': hashlib.sha256(png).hexdigest()}
            write_receipt(context.handle, response, row)
            context.handle._fault('nai_after_receipt')
            self._accept_local(context, item.item_id)
        with self.runtime.generation_sources.lock:
            current, _, remaining = self._snapshot(context, source_id)
            if current.status == 'running' and remaining is not None:
                self.runtime.generation_sources._pause(context, source_id, 'generation:cycle_changed')
        self._finish(context, source_id)

    def _accept_local(self, context, item_id):
        with read_snapshot(context.handle) as db:
            item = sources.get_item(db, item_id)
            source = sources.get_source(db, item.source_id)
        row, grant = bound_receipt(context.handle, source, item)
        if row is None or row['phase'] not in ('response', 'closed'):
            raise ServiceError(409, 'nai:outcome_unknown')
        if item.status == 'sent':
            from ..tagging.catalog import TagCatalog
            snapshot = self.runtime.catalog.snapshot()
            catalog = TagCatalog(snapshot.revision, dict(snapshot.entries), dict(snapshot.categories), snapshot.complete)
            path = relative_path(context.handle.root, f"images/.pending/{item_id}/{row['attempt']}/out.png")
            prepared = inspect_and_prepare(context.handle, item_id=item_id, attempt=row['attempt'],
                saved_path=str(path), catalog=catalog, owned_sha256=row['sha256'])
            register_artifact(context.handle, prepared)
        elif item.status == 'done':
            with read_snapshot(context.handle) as db:
                image = images.get_image(db, item.image_id)
            if image.sha256 != row['sha256']:
                raise ServiceError(409, 'artifact:ownership_conflict')
        else:
            raise ServiceError(409, 'nai:receipt_mismatch')
        self.runtime.generation_costs.observe(context, item_id, row['attempt'], CostEvidence(
            grant['operation_key'], INSTANCE, CONTRACT, 'submitted', digest({'sha256': row['sha256'], 'operation_key': row['operation_key']})))
        if row['phase'] != 'closed':
            write_receipt(context.handle, {**row, 'phase': 'closed'}, row)
        self.runtime.generation_sources._emit(context, source.source_id)

    async def _recover(self, context, item_id, *, restore):
        if restore:
            raise ServiceError(409, 'recovery:action_unavailable')
        self._accept_local(context, item_id)
        with read_snapshot(context.handle) as db:
            source_id = sources.get_item(db, item_id).source_id
        self._finish(context, source_id)

    def recovery_view(self, context, source, item, *, pending=False):
        row, grant = None, None
        phase, reason, reconcile = 'untracked', None, False
        busy = pending or self.recovery_busy()
        try:
            row, grant = bound_receipt(context.handle, source, item)
            if row and row['phase'] == 'closed':
                phase = 'settled'
            elif row and row['phase'] == 'response' and item.status in ('sent', 'done'):
                phase, reconcile = 'needs_reconcile', True
                if source.status == 'running':
                    reason, reconcile = 'recovery:pause_required', False
                elif busy:
                    reason, reconcile = 'recovery:busy', False
            elif item.status == 'sent' or row:
                phase, reason = 'blocked', 'nai:outcome_unknown'
        except ServiceError as error:
            phase, reason = 'blocked', error.code
        version = self.runtime.versions.get(f'{context.project_id}:generation-recovery:{source.source_id}:{item.item_id}',
            {'source': primitive(source), 'item': primitive(item), 'receipt': row, 'cost': grant, 'reason': reason if phase == 'blocked' else None})
        return {'source_id': source.source_id, 'item_id': item.item_id, 'seq': item.seq, 'attempt': item.attempts,
                'status': item.status, 'phase': phase, 'reason': reason, 'lease_phase': None,
                'busy': busy, 'can_reconcile': reconcile, 'can_restore': False, 'version': version}
