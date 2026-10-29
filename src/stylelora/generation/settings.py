"""Product-wide settings lease and durable write fence; no production adapter."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import re
import threading
from typing import Protocol
from uuid import uuid4

from ..llm.secrets import safe_path
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, encode, project_data
from ..runtime.contracts import strict_json, uuid_string
from ..runtime.errors import ServiceError

KEYS = frozenset(('model', 'effort', 'sampler', 'steps', 'scale', 'cfg_rescale', 'noise_schedule',
                  'quality_toggle', 'uc_preset', 'smea', 'smea_dyn', 'variety_plus', 'decrisp'))
PHASES = frozenset(('applying', 'applied', 'needs_restore', 'restoring', 'conflict'))
MAX_BYTES = 65536


def _same(left, right):
    return type(left) is type(right) and left == right


@dataclass(frozen=True)
class SettingsIdentity:
    instance_id: str  # Adapter-proven stable instance fingerprint, not a PID alone.
    contract_digest: str
    writable_keys: tuple[str, ...]


class SettingsWriteConflict(Exception):
    """Reviewed adapter proved atomic precondition rejection, with no settings written."""


class SettingsAdapter(Protocol):
    identity: SettingsIdentity

    async def read_settings(self) -> dict: ...
    # Compare expected at the mutation boundary, not as a separate preflight GET.
    # An adapter lacking this guarantee must not enable the production writer.
    async def update_settings(self, values: dict, operation_id: str, *, expected: dict) -> None: ...
    # True must prove this exact operation has settled, including late effects.
    # Reading unchanged values, a timeout or a missing recent entry is not proof.
    async def settings_write_settled(self, operation_id: str) -> bool: ...
    async def generation_settled(self, operation_key: str) -> bool: ...


def _fingerprint(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _scalar(value):
    if value is None or type(value) is bool:
        return True
    if type(value) in (int, float):
        return math.isfinite(value) and abs(value) <= 2**53
    return isinstance(value, str) and len(value) <= 512 and not any(ord(c) < 32 for c in value)


def _values(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys) or not all(_scalar(v) for v in value.values()):
        raise ValueError()
    project_data(value)


def _identity(adapter):
    try:
        value = adapter.identity
        if (not isinstance(value, SettingsIdentity) or not _fingerprint(value.instance_id)
                or not _fingerprint(value.contract_digest) or not isinstance(value.writable_keys, tuple)
                or not value.writable_keys or len(set(value.writable_keys)) != len(value.writable_keys)
                or not set(value.writable_keys) <= KEYS):
            raise ValueError()
        return {'instance_id': value.instance_id, 'contract_digest': value.contract_digest,
                'writable_keys': sorted(value.writable_keys)}
    except Exception:
        raise ServiceError(422, 'profile:invalid_contract') from None


def _validate_changes(changes, writable):
    try:
        if not changes or not set(changes) <= set(writable):
            raise ValueError()
        _values(changes, changes.keys())
        for key, value in changes.items():
            if key in ('model', 'effort', 'sampler', 'noise_schedule'):
                valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 200
            elif key == 'steps':
                valid = type(value) is int and 1 <= value <= 1000
            elif key in ('scale', 'cfg_rescale'):
                valid = type(value) in (int, float) and (0 < value <= 1000 if key == 'scale' else 0 <= value <= 1)
            elif key == 'uc_preset':
                valid = value is None
            elif key == 'quality_toggle':
                valid = value is False
            else:
                valid = type(value) is bool
            if not valid:
                raise ValueError()
    except Exception:
        raise ServiceError(422, 'profile:invalid_settings') from None


class SettingsJournal:
    def __init__(self, directory=None):
        self.path = Path(directory)/'settings-lease.json' if directory is not None else None
        self.memory = {'schema_version': 1, 'revision': 0, 'record': None}
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            try:
                if self.path is None:
                    return deepcopy(self.memory)
                path = safe_path(self.path)
                if not path.exists():
                    return {'schema_version': 1, 'revision': 0, 'record': None}
                if path.stat().st_size > MAX_BYTES:
                    raise ValueError()
                with path.open('rb') as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError()
                envelope = strict_json(raw)
                if set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(envelope['content']):
                    raise ValueError()
                state = envelope['content']
                if (set(state) != {'schema_version', 'revision', 'record'} or type(state['schema_version']) is not int
                        or state['schema_version'] != 1 or type(state['revision']) is not int or state['revision'] < 0):
                    raise ValueError()
                row = state['record']
                if row is not None:
                    if (not isinstance(row, dict) or set(row) != {'lease_id', 'project_id', 'source_id', 'cycle_id',
                            'identity', 'original', 'desired', 'phase', 'write_id', 'inflight'} or state['revision'] == 0):
                        raise ValueError()
                    for key in ('lease_id', 'project_id', 'cycle_id'):
                        uuid_string(row[key])
                    if type(row['source_id']) is not int or row['source_id'] < 1 or row['phase'] not in PHASES:
                        raise ValueError()
                    identity = row['identity']
                    if set(identity) != {'instance_id', 'contract_digest', 'writable_keys'}:
                        raise ValueError()
                    if (not _fingerprint(identity['instance_id']) or not _fingerprint(identity['contract_digest'])
                            or not isinstance(identity['writable_keys'], list) or not identity['writable_keys']
                            or identity['writable_keys'] != sorted(set(identity['writable_keys']))
                            or not set(identity['writable_keys']) <= KEYS):
                        raise ValueError()
                    _values(row['original'], identity['writable_keys'])
                    _values(row['desired'], identity['writable_keys'])
                    if row['phase'] in ('applying', 'restoring'):
                        uuid_string(row['write_id'])
                    elif row['write_id'] is not None:
                        raise ValueError()
                    if row['inflight'] is not None and (not _fingerprint(row['inflight']) or row['phase'] != 'applied'):
                        raise ValueError()
                return state
            except OSError:
                raise ServiceError(503, 'profile:journal_unavailable') from None
            except Exception:
                raise ServiceError(503, 'profile:invalid_journal') from None

    def save(self, previous, record):
        with self.lock:
            if digest(self.load()) != digest(previous):
                raise ServiceError(409, 'profile:journal_changed')
            value = {'schema_version': 1, 'revision': previous['revision'] + 1, 'record': deepcopy(record)}
            envelope = {'content': value, 'digest': digest(value)}
            if len(encode(envelope).encode('utf-8')) + 1 > MAX_BYTES:
                raise ServiceError(413, 'profile:journal_limit')
            if self.path is None:
                self.memory = value
            else:
                try:
                    path = safe_path(self.path)
                    safe_path(path.parent).mkdir(parents=True, exist_ok=True)
                    atomic_json(path, envelope)
                except Exception:
                    try:
                        if digest(self.load()) == digest(value):
                            return value
                    except ServiceError:
                        pass
                    raise ServiceError(503, 'profile:journal_write_failed', outcome='unknown') from None
            return deepcopy(value)


class SettingsLeases:
    """One runtime-owned manager for the single Launcher; nonblocking cross-loop exclusion."""
    def __init__(self, directory=None, *, notify=lambda: None):
        self.journal = SettingsJournal(directory)
        self.gate = threading.Lock()
        self.notify = notify
        self.live_lease = None

    @contextmanager
    def _operation(self):
        if not self.gate.acquire(blocking=False):
            raise ServiceError(409, 'profile:lease_busy')
        try:
            yield
        finally:
            self.gate.release()

    def _load(self):
        state = self.journal.load()
        if self.live_lease and (state['record'] is None or state['record']['lease_id'] != self.live_lease):
            raise ServiceError(409, 'profile:journal_changed')
        return state

    def _save(self, state, record):
        result = self.journal.save(state, record)
        self.live_lease = record['lease_id'] if record else None
        try:
            self.notify()
        except Exception:
            pass
        return result

    def status(self):
        try:
            state = self._load()
            row = state['record']
            phase = row['phase'] if row else 'checking' if self.gate.locked() else 'idle'
            reasons = {'applying': 'profile:apply_unknown', 'restoring': 'profile:restore_unknown',
                       'conflict': 'profile:restore_conflict', 'needs_restore': 'profile:restore_pending',
                       'applied': 'profile:lease_active', 'checking': 'profile:lease_busy'}
            return {'blocked': phase != 'idle', 'phase': phase, 'reason': reasons.get(phase),
                    'inflight': bool(row and row['inflight']), 'revision': state['revision'], 'version': digest(state),
                    'source_id': row['source_id'] if row else None,
                    'changed_keys': sorted(k for k in row['original'] if not _same(row['original'][k], row['desired'][k])) if row else [],
                    'recovery_adapter_available': False}
        except ServiceError as error:
            return {'blocked': True, 'phase': 'invalid', 'reason': error.code, 'inflight': None,
                    'revision': None, 'version': None, 'source_id': None, 'changed_keys': [], 'recovery_adapter_available': False}

    def _record(self, lease_id, adapter=None):
        state = self._load()
        row = state['record']
        if row is None or row['lease_id'] != lease_id:
            raise ServiceError(409, 'profile:stale_lease')
        if adapter is not None and _identity(adapter) != row['identity']:
            raise ServiceError(409, 'profile:backend_changed')
        return state, row

    async def _read(self, adapter, identity):
        try:
            raw = await adapter.read_settings()
            if _identity(adapter) != identity:
                raise ServiceError(409, 'profile:backend_changed')
            values = {k: deepcopy(raw[k]) for k in identity['writable_keys']}
            _values(values, identity['writable_keys'])
            return values
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, 'profile:read_failed') from None

    async def acquire(self, adapter, *, project_id, source_id, cycle_id, changes):
        with self._operation():
            uuid_string(project_id); uuid_string(cycle_id)
            if type(source_id) is not int or source_id < 1:
                raise ServiceError(422, 'profile:invalid_owner')
            state = self._load()
            if state['record']:
                raise ServiceError(409, 'profile:restore_pending')
            identity = _identity(adapter)
            changes = deepcopy(changes)
            _validate_changes(changes, identity['writable_keys'])
            original = await self._read(adapter, identity)
            desired = {**original, **deepcopy(changes)}
            patch = {k: desired[k] for k in desired if not _same(desired[k], original[k])}
            row = {'lease_id': str(uuid4()), 'project_id': project_id, 'source_id': source_id, 'cycle_id': cycle_id,
                   'identity': identity, 'original': original, 'desired': desired, 'phase': 'applying' if patch else 'applied',
                   'write_id': str(uuid4()) if patch else None, 'inflight': None}
            state = self._save(state, row)  # Durable BEFORE any external mutation.
            if patch:
                try:
                    result = await adapter.update_settings(deepcopy(patch), row['write_id'],
                        expected={k: deepcopy(original[k]) for k in patch})
                    if result is not None:
                        raise ValueError()
                except SettingsWriteConflict:
                    self._save(state, {**row, 'phase': 'conflict', 'write_id': None})
                    raise ServiceError(409, 'profile:restore_conflict') from None
                except Exception:
                    raise ServiceError(503, 'profile:apply_unknown', outcome='unknown') from None
                self._record(row['lease_id'], adapter)
                row = {**row, 'phase': 'applied', 'write_id': None}
                state = self._save(state, row)
            actual = await self._read(adapter, identity)
            if any(not _same(actual[k], desired[k]) for k in desired):
                self._save(state, {**row, 'phase': 'conflict'})
                raise ServiceError(409, 'profile:drift')
            return row['lease_id']

    async def verify_effective(self, adapter, lease_id, effective, *, preparation_key=None):
        with self._operation():
            state, row = self._record(lease_id, adapter)
            if row['phase'] != 'applied' or (row['inflight'] is not None and row['inflight'] != preparation_key):
                raise ServiceError(409, 'profile:restore_pending')
            if not isinstance(effective, dict) or any(k not in effective or not _same(effective[k], v)
                                                     for k, v in row['desired'].items()):
                self._save(state, {**row, 'phase': 'conflict'})
                raise ServiceError(409, 'profile:drift')

    def mark_inflight(self, lease_id, operation_key):
        with self._operation():
            state, row = self._record(lease_id)
            if not _fingerprint(operation_key) or row['phase'] != 'applied' or row['inflight']:
                raise ServiceError(409, 'profile:inflight_conflict')
            self._save(state, {**row, 'inflight': operation_key})

    async def settle_inflight(self, adapter, lease_id):
        with self._operation():
            state, row = self._record(lease_id, adapter)
            if row['inflight'] is None:
                return
            if not await self._settled(adapter, 'generation_settled', row['inflight'], row['identity']):
                raise ServiceError(409, 'profile:generation_unresolved')
            self._record(lease_id, adapter)
            self._save(state, {**row, 'inflight': None})

    async def reconcile(self, adapter, lease_id):
        """Read-only remote reconciliation; never repeats an update call."""
        with self._operation():
            state, row = self._record(lease_id, adapter)
            if row['inflight']:
                raise ServiceError(409, 'profile:generation_unresolved')
            if row['phase'] in ('applying', 'restoring'):
                if not await self._settled(adapter, 'settings_write_settled', row['write_id'], row['identity']):
                    raise ServiceError(409, 'profile:write_unresolved')
                self._record(lease_id, adapter)
                row = {**row, 'phase': 'needs_restore', 'write_id': None}
                state = self._save(state, row)
            current = await self._read(adapter, row['identity'])
            patch = self._restore_patch(state, row, current)
            self._save(state, {**row, 'phase': 'needs_restore', 'write_id': None} if patch else None)

    async def _settled(self, adapter, method, key, identity):
        try:
            result = await getattr(adapter, method)(key)
            if _identity(adapter) != identity:
                raise ServiceError(409, 'profile:backend_changed')
            return result is True
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, 'profile:read_failed') from None

    def _restore_patch(self, state, row, current):
        changed = [k for k in row['original'] if not _same(row['original'][k], row['desired'][k])]
        if any(not _same(current[k], row['original'][k]) and not _same(current[k], row['desired'][k]) for k in changed):
            self._save(state, {**row, 'phase': 'conflict', 'write_id': None})
            raise ServiceError(409, 'profile:restore_conflict')
        return {k: row['original'][k] for k in changed if not _same(current[k], row['original'][k])}

    async def restore(self, adapter, lease_id):
        with self._operation():
            state, row = self._record(lease_id, adapter)
            if row['inflight']:
                raise ServiceError(409, 'profile:generation_unresolved')
            if row['phase'] in ('applying', 'restoring'):
                raise ServiceError(409, 'profile:write_unresolved')
            current = await self._read(adapter, row['identity'])
            patch = self._restore_patch(state, row, current)
            if not patch:
                self._save(state, None)
                return
            row = {**row, 'phase': 'restoring', 'write_id': str(uuid4())}
            state = self._save(state, row)
            try:
                result = await adapter.update_settings(deepcopy(patch), row['write_id'],
                    expected={k: deepcopy(current[k]) for k in patch})
                if result is not None:
                    raise ValueError()
            except SettingsWriteConflict:
                self._save(state, {**row, 'phase': 'conflict', 'write_id': None})
                raise ServiceError(409, 'profile:restore_conflict') from None
            except Exception:
                raise ServiceError(503, 'profile:restore_unknown', outcome='unknown') from None
            self._record(lease_id, adapter)
            row = {**row, 'phase': 'needs_restore', 'write_id': None}
            state = self._save(state, row)
            current = await self._read(adapter, row['identity'])
            if self._restore_patch(state, row, current):
                raise ServiceError(409, 'profile:restore_unverified')
            self._save(state, None)
