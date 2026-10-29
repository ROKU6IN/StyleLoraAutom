"""F02–F05 serial coordinator for an explicitly injected, reviewed adapter.

No production adapter is registered. These are internal contracts, NOT proposed
Launcher MCP fields. An adapter must implement bounded async calls, durable exact
operation association, authoritative settlement (including prepare/cancel and all
late effects), and atomic settings guards before it can satisfy this interface.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import threading
import time
from typing import Protocol
from uuid import UUID, uuid4

from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.repositories import sources
from ..project_db.validation import digest, relative_path
from ..runtime.errors import ServiceError
from .artifacts import inspect_and_prepare, register_artifact
from .costs import CostEvidence, _hash, _read as read_cost
from .operations import read_operation, write_operation
from .profile import build_request, locked_projection, validate_capabilities
from .settings import SettingsAdapter


@dataclass(frozen=True)
class Preparation:
    estimated_anlas: int
    effective: dict


@dataclass(frozen=True)
class Outcome:
    operation_key: str
    instance_id: str
    contract_digest: str
    request_digest: str
    config_digest: str
    cost_operation_key: str | None
    kind: str  # complete / rejected (no charge, no artifact) / canceled / unknown
    evidence_digest: str
    artifact_sha256: str | None = None
    metadata_stripped: bool | None = None


class CycleAdapter(SettingsAdapter, Protocol):
    def capabilities(self) -> dict: ...
    async def prepare(self, operation: dict, request: dict, save_path: str) -> Preparation: ...
    async def submit(self, operation: dict, grant: dict) -> Outcome: ...
    async def cancel_preparation(self, operation: dict) -> Outcome: ...
    async def probe(self, operation: dict, grant: dict | None) -> Outcome: ...


class SerialDriver:
    """One global slot; dispatch retains the project before returning to HTTP.

    stop only closes admission. It never cancels an already authorized submit.
    Unknown writes are never retried, even after process or project reopening.
    """
    def __init__(self, runtime, adapter, *, timeout=660, interval=1.0):
        self.runtime, self.adapter = runtime, adapter
        self.timeout, self.interval = timeout, max(1.0, interval)
        self.lock = threading.Lock()
        self.active = None
        self.last_submit = None

    def capabilities(self):
        return deepcopy(self.adapter.capabilities())

    def recovery_identity(self):
        """Local adapter identity only; never performs discovery or network I/O."""
        return {k: getattr(self.adapter.identity, k) for k in ('instance_id', 'contract_digest')}

    def recovery_busy(self):
        with self.lock:
            return self.active is not None

    def is_active(self, context, source_id):
        with self.lock:
            return bool(self.active and self.active[:2] == (context.project_id, source_id))

    def stop(self, context, source_id):
        with self.lock:
            if self.active and self.active[:2] == (context.project_id, source_id):
                self.active[2].set()

    def stop_project(self, project_id):
        with self.lock:
            if self.active and self.active[0] == project_id:
                self.active[2].set()

    def dispatch(self, context, source_id):
        self._launch(context, source_id, 'cycle')

    def reconcile(self, context, item_id):
        """Internal read-only remote probe; never prepare/submit/cancel/restore."""
        with read_snapshot(context.handle) as db:
            item = sources.get_item(db, item_id)
        self._launch(context, item.source_id, 'reconcile', item_id)

    def restore(self, context, item_id):
        """Separate, explicit internal recovery action after read-only reconciliation."""
        with read_snapshot(context.handle) as db:
            item = sources.get_item(db, item_id)
        self._launch(context, item.source_id, 'restore', item_id)

    def _launch(self, context, source_id, mode, item_id=None):
        stop, finished = self._reserve(context, source_id)
        thread = threading.Thread(target=self._work, args=(context, source_id, stop, mode, item_id, finished),
                                  name='stylelora-generation', daemon=True)
        try:
            thread.start()
        except BaseException:
            self._release(context, finished)
            raise

    def _reserve(self, context, source_id):
        with self.lock:
            if self.active:
                raise ServiceError(409, 'generation:driver_busy')
            self.runtime.retain(context)
            stop, finished = threading.Event(), threading.Event()
            self.active = (context.project_id, source_id, stop, finished)
            return stop, finished

    def _release(self, context, finished):
        with self.lock:
            self.runtime.release(context)
            self.active = None
            finished.set()

    def wait(self, timeout=10):
        with self.lock:
            finished = self.active[3] if self.active else None
        if finished and not finished.wait(timeout):
            raise TimeoutError('Generation driver is still draining')

    def _work(self, context, source_id, stop, mode, item_id, finished):
        try:
            if mode == 'cycle':
                asyncio.run(self._cycle(context, source_id, stop))
            else:
                asyncio.run(self._recover(context, item_id, restore=mode == 'restore'))
        except BaseException as error:
            # Never expose arbitrary adapter exceptions (paths, prompts, tokens).
            code = error.code if isinstance(error, ServiceError) and error.code in SAFE_REASONS else 'generation:outcome_unknown'
            with self.runtime.generation_sources.lock:
                self.runtime.generation_sources._pause(context, source_id, code)
                self.runtime.generation_sources._emit(context, source_id)
        finally:
            self._release(context, finished)

    def recover_blocking(self, context, source_id, item_id, action, validate):
        """Supervisor worker entry: validate and reserve atomically before remote reads/writes."""
        if action not in ('reconcile', 'restore'):
            raise ServiceError(422, 'recovery:invalid_action')
        with self.runtime.generation_sources.lock:
            validate()
            _, finished = self._reserve(context, source_id)
        try:
            asyncio.run(self._recover(context, item_id, restore=action == 'restore'))
        except BaseException as error:
            code = error.code if isinstance(error, ServiceError) and error.code in SAFE_REASONS else 'generation:outcome_unknown'
            with self.runtime.generation_sources.lock:
                self.runtime.generation_sources._pause(context, source_id, code)
                self.runtime.generation_sources._emit(context, source_id)
            raise ServiceError(409 if isinstance(error, ServiceError) else 503, code,
                               outcome='unknown' if action == 'restore' else 'rejected') from None
        finally:
            self._release(context, finished)

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable, self.timeout)

    def _snapshot(self, context, source_id):
        def read(db):
            source = sources.get_source(db, source_id)
            cycle = deepcopy(context.handle._cycles.get(source_id))
            row = db.execute("SELECT item_id FROM source_items WHERE source_id=? AND status IN ('pending','sent') ORDER BY seq LIMIT 1",
                             (source_id,)).fetchone()
            return source, cycle, sources.get_item(db, row[0]) if row else None
        return submit_write(context.handle, read).result()

    def _stopped(self, context, stop):
        return stop.is_set() or context.closing or self.runtime.draining

    def _identity(self, row):
        if any(getattr(self.adapter.identity, k) != row[k] for k in ('instance_id', 'contract_digest')):
            raise ServiceError(409, 'profile:backend_changed')

    def _binding(self, context, item, row):
        with read_snapshot(context.handle) as db:
            source = sources.get_source(db, item.source_id)
        if (row['source_id'] != source.source_id or row['request_digest'] != digest(build_request(source, item))
                or row['config_digest'] != digest(locked_projection(source)) or row['attempt'] != max(1, item.attempts)):
            raise ServiceError(409, 'generation:evidence_mismatch')
        self._identity(row)

    def _finish(self, context, source_id):
        with self.runtime.generation_sources.lock:
            def write(db):
                source = sources.get_source(db, source_id)
                if source.status in ('running', 'paused') and not db.execute(
                    "SELECT 1 FROM source_items WHERE source_id=? AND status IN ('pending','sent')", (source_id,)).fetchone():
                    commands.transition_source(db, source_id, expected=source.status, target='done', reason='generation:complete')
            submit_write(context.handle, write).result()
            self.runtime.generation_sources._emit(context, source_id)

    async def _cycle(self, context, source_id, stop):
        leases = self.runtime.settings_leases
        source, cycle, item = self._snapshot(context, source_id)
        if self._stopped(context, stop) or source.status != 'running' or not cycle:
            return
        if item and item.status == 'sent':
            raise ServiceError(409, 'generation:reconcile_required')
        capabilities = self.capabilities()
        validate_capabilities(source.config_json, source.negative_prompt, capabilities)
        identity = self.adapter.identity
        if identity.contract_digest != capabilities['contract_digest']:
            raise ServiceError(409, 'profile:backend_changed')
        changes = {k: source.config_json['profile'][k] for k in identity.writable_keys}
        lease = await self._call(leases.acquire(self.adapter, project_id=context.project_id, source_id=source_id,
            cycle_id=str(UUID(cycle['id'])), changes=changes))
        unresolved = False
        try:
            while not self._stopped(context, stop):
                current, active_cycle, item = self._snapshot(context, source_id)
                if current.status != 'running' or not active_cycle or active_cycle['id'] != cycle['id'] or item is None:
                    break
                if item.status != 'pending':
                    raise ServiceError(409, 'generation:reconcile_required')
                attempt = max(1, item.attempts)
                previous = read_operation(context.handle, item.item_id, attempt)
                if previous and not previous['closed']:
                    raise ServiceError(409, 'generation:reconcile_required')
                save_path = relative_path(context.handle.root, f'images/.pending/{item.item_id}/{attempt}/out.png')
                if save_path.exists():
                    raise ServiceError(409, 'artifact:save_path_exists')
                request = build_request(current, item)
                row = {'schema_version': 1, 'source_id': source_id, 'item_id': item.item_id, 'attempt': attempt,
                       'operation_key': digest(uuid4().hex), 'lease_id': lease, 'instance_id': identity.instance_id,
                       'contract_digest': identity.contract_digest, 'request_digest': digest(request),
                       'config_digest': digest(locked_projection(current)), 'closed': False}
                write_operation(context.handle, row, previous)
                leases.mark_inflight(lease, row['operation_key'])  # Also fences unknown PREPARE and cancel.
                unresolved = True
                self._identity(row)
                preparation = await self._call(self.adapter.prepare(deepcopy(row), request, str(save_path)))
                if not isinstance(preparation, Preparation):
                    raise ServiceError(503, 'generation:invalid_preparation')
                await leases.verify_effective(self.adapter, lease, preparation.effective, preparation_key=row['operation_key'])
                # Rate limit before mark_sent. Pauses during the wait cannot buy a new dispatch.
                if self.last_submit is not None:
                    await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_submit)))
                with self.runtime.generation_sources.lock:
                    now, actual_cycle, _ = self._snapshot(context, source_id)
                    allowed = (not self._stopped(context, stop) and now.status == 'running'
                               and actual_cycle and actual_cycle['id'] == cycle['id'])
                    self._identity(row)
                    grant = self.runtime.generation_costs.reserve_dispatch(context, item.item_id,
                        estimated_anlas=preparation.estimated_anlas, expected_cycle_id=cycle['id'],
                        instance_id=identity.instance_id, contract_digest=identity.contract_digest) if allowed else None
                if grant is None:
                    outcome = await self._call(self.adapter.cancel_preparation(deepcopy(row)))
                    await self._accept(context, item, row, None, outcome)
                    unresolved = False
                    break
                # No awaits between committed grant and submit except the submit itself.
                # Even a pause/cancel here must allow this one authorized attempt to settle.
                self.last_submit = time.monotonic()
                outcome = await self._call(self.adapter.submit(deepcopy(row), deepcopy(grant)))
                await self._accept(context, item, row, grant, outcome)
                unresolved = False
                if outcome.kind == 'rejected':
                    raise ServiceError(409, 'generation:rejected')
        finally:
            if not unresolved:
                await self._call(leases.restore(self.adapter, lease))
        with self.runtime.generation_sources.lock:
            current, _, remaining = self._snapshot(context, source_id)
            if current.status == 'running' and remaining is not None:
                self.runtime.generation_sources._pause(context, source_id, 'generation:cycle_changed')
        self._finish(context, source_id)

    async def _accept(self, context, item, row, grant, outcome):
        self._binding(context, item, row)
        if grant and any(grant[k] != row[k] for k in ('source_id', 'item_id', 'attempt', 'instance_id',
                                                     'contract_digest', 'request_digest', 'config_digest')):
            raise ServiceError(409, 'generation:evidence_mismatch')
        if (not isinstance(outcome, Outcome) or outcome.kind not in ('complete', 'rejected', 'canceled')
                or not _hash(outcome.evidence_digest)
                or any(getattr(outcome, k) != row[k] for k in ('operation_key', 'instance_id', 'contract_digest', 'request_digest', 'config_digest'))
                or outcome.cost_operation_key != (grant['operation_key'] if grant else None)
                or (grant is None) != (outcome.kind == 'canceled')):
            raise ServiceError(409, 'generation:evidence_mismatch')
        # Both matching terminal evidence and authoritative no-late-effects proof required.
        if await self._call(self.adapter.generation_settled(row['operation_key'])) is not True:
            raise ServiceError(409, 'generation:reconcile_required')
        self._identity(row)
        if grant:
            with read_snapshot(context.handle) as db:
                actual = sources.get_item(db, item.item_id)
            if actual.status == 'sent':
                if outcome.kind == 'complete':
                    from ..tagging.catalog import TagCatalog
                    snapshot = self.runtime.catalog.snapshot()
                    catalog = TagCatalog(snapshot.revision, dict(snapshot.entries), dict(snapshot.categories), snapshot.complete)
                    path = relative_path(context.handle.root, f"images/.pending/{item.item_id}/{row['attempt']}/out.png")
                    if not _hash(outcome.artifact_sha256) or (outcome.metadata_stripped is not None and type(outcome.metadata_stripped) is not bool):
                        raise ServiceError(409, 'artifact:ownership_conflict')
                    # F05 validates/decompresses first with resource bounds; compare its
                    # verified digest with adapter-owned bytes before registration.
                    prepared = inspect_and_prepare(context.handle, item_id=item.item_id, attempt=row['attempt'],
                        saved_path=str(path), metadata_stripped=outcome.metadata_stripped, catalog=catalog,
                        owned_sha256=outcome.artifact_sha256)
                    register_artifact(context.handle, prepared)
                else:
                    if relative_path(context.handle.root, f"images/.pending/{item.item_id}/{row['attempt']}/out.png").exists():
                        raise ServiceError(409, 'artifact:ownership_conflict')
                    submit_write(context.handle, lambda db: commands.fail_item(db, item.item_id,
                        attempt=row['attempt'], error='generation:rejected')).result()
            elif actual.status not in ('done', 'failed') or actual.attempts != row['attempt']:
                raise ServiceError(409, 'generation:evidence_mismatch')
            elif (actual.status == 'done') != (outcome.kind == 'complete'):
                raise ServiceError(409, 'generation:evidence_mismatch')
            elif actual.status == 'done':
                from ..project_db.repositories import images
                with read_snapshot(context.handle) as db:
                    image = images.get_image(db, actual.image_id)
                if image.sha256 != outcome.artifact_sha256:
                    raise ServiceError(409, 'artifact:ownership_conflict')
            self.runtime.generation_costs.observe(context, item.item_id, row['attempt'], CostEvidence(
                grant['operation_key'], row['instance_id'], row['contract_digest'],
                'submitted' if outcome.kind == 'complete' else 'rejected', outcome.evidence_digest))
        await self._call(self.runtime.settings_leases.settle_inflight(self.adapter, row['lease_id']))
        write_operation(context.handle, {**row, 'closed': True}, row)
        self.runtime.generation_sources._emit(context, item.source_id)

    async def _recover(self, context, item_id, *, restore):
        with read_snapshot(context.handle) as db:
            item = sources.get_item(db, item_id)
        row = read_operation(context.handle, item_id, max(1, item.attempts))
        if row is None:
            raise ServiceError(409, 'generation:reconcile_required')
        self._binding(context, item, row)
        if restore:
            if not row['closed']:
                raise ServiceError(409, 'generation:reconcile_required')
            if self.runtime.settings_leases.status()['blocked']:
                await self._call(self.runtime.settings_leases.restore(self.adapter, row['lease_id']))
            self._finish(context, item.source_id)
            return
        if not row['closed']:
            grant = read_cost(context.handle, item_id, row['attempt']) if item.status != 'pending' else None
            if item.status != 'pending' and grant is None:
                raise ServiceError(409, 'generation:reconcile_required')
            outcome = await self._call(self.adapter.probe(deepcopy(row), deepcopy(grant)))
            await self._accept(context, item, row, grant, outcome)
        if self.runtime.settings_leases.status()['blocked']:
            await self._call(self.runtime.settings_leases.reconcile(self.adapter, row['lease_id']))


SAFE_REASONS = frozenset({
    'nai:token_missing', 'nai:secret_store_unavailable', 'nai:request_unknown', 'nai:outcome_unknown',
    'nai:invalid_receipt', 'nai:receipt_mismatch', 'nai:receipt_write_unknown', 'nai:invalid_image_response',
    'nai:invalid_response', 'nai:response_too_large', 'nai:unauthorized', 'nai:forbidden', 'nai:rate_limited', 'nai:http_error',
    'generation:reconcile_required', 'generation:evidence_mismatch', 'generation:rejected',
    'generation:invalid_operation', 'generation:operation_write_unknown', 'generation:invalid_preparation',
    'generation:unsupported_configuration', 'generation:invalid_capabilities', 'generation:negative_locked',
    'profile:backend_changed', 'profile:restore_pending', 'profile:drift', 'profile:restore_conflict',
    'profile:apply_unknown', 'profile:restore_unknown', 'profile:restore_unverified',
    'cost:invalid_estimate', 'cost:cycle_expired', 'cost:dispatch_commit_unknown', 'cost:receipt_write_failed',
    'artifact:ownership_conflict', 'artifact:save_path_exists', 'artifact:invalid_png', 'artifact:size_mismatch',
})
