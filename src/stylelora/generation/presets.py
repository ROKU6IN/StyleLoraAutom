"""Product-wide pixel presets. Sources only receive copied concrete dimensions."""
from copy import deepcopy
from pathlib import Path
import threading
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import Field, field_validator

from ..api.contracts import StrictModel, Identifier
from ..llm.secrets import safe_path
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, encode
from ..runtime.contracts import Versions, strict_json, uuid_string
from ..runtime.errors import ServiceError
from .profile import SIZES
from .types import Pixels, validate_sizes

MAX_PRESETS = 2048
MAX_BYTES = 8 * 1024 * 1024
BUILTIN_IDS = {key: str(uuid5(NAMESPACE_URL, 'stylelora:generation-presets:' + key + ':v1')) for key in SIZES}


class PresetInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    sizes: dict[str, Pixels]

    @field_validator('name')
    @classmethod
    def name_valid(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid preset name')
        return value.strip()

    _sizes = field_validator('sizes')(validate_sizes)


class StoredPreset(PresetInput):
    preset_id: Identifier
    revision: int = Field(ge=1)


def view(row, *, builtin_key=None):
    value = {**deepcopy(row), 'builtin': builtin_key is not None, 'builtin_key': builtin_key}
    return {**value, 'version': digest(value)}


def builtins():
    return [view({'preset_id': BUILTIN_IDS[key], 'name': '小' if key == 'small' else '大',
                  'sizes': sizes, 'revision': 1}, builtin_key=key) for key, sizes in SIZES.items()]


class PresetStore:
    def __init__(self, runtime, directory=None):
        self.runtime, self.lock = runtime, threading.RLock()
        self.path = Path(directory) / 'generation_presets.json' if directory is not None else None
        self.memory = {'schema_version': 1, 'revision': 0, 'presets': {}}

    def _load(self):
        if self.path is None:
            return deepcopy(self.memory)
        try:
            path = safe_path(self.path)
            if not path.exists():
                return {'schema_version': 1, 'revision': 0, 'presets': {}}
            if path.stat().st_size > MAX_BYTES:
                raise ValueError()
            envelope = strict_json(path.read_bytes())
            if set(envelope) != {'content', 'digest'} or envelope['digest'] != digest(envelope['content']):
                raise ValueError()
            value = envelope['content']
            if (set(value) != {'schema_version', 'revision', 'presets'} or type(value['schema_version']) is not int
                    or value['schema_version'] != 1 or type(value['revision']) is not int or value['revision'] < 0
                    or not isinstance(value['presets'], dict) or len(value['presets']) > MAX_PRESETS):
                raise ValueError()
            for identifier, row in value['presets'].items():
                uuid_string(identifier)
                if (identifier in BUILTIN_IDS.values() or StoredPreset.model_validate(row).model_dump() != row
                        or row['preset_id'] != identifier or row['revision'] > value['revision']):
                    raise ValueError()
            return value
        except OSError:
            raise ServiceError(503, 'preset:io_error') from None
        except Exception:
            raise ServiceError(503, 'preset:invalid_storage') from None

    def _commit(self, value):
        envelope = {'content': value, 'digest': digest(value)}
        if len(encode(envelope).encode('utf-8')) > MAX_BYTES:
            raise ServiceError(413, 'preset:storage_limit')
        if self.path is None:
            self.memory = deepcopy(value)
        else:
            try:
                path = safe_path(self.path)
                safe_path(path.parent).mkdir(parents=True, exist_ok=True)
                atomic_json(path, envelope)
            except Exception:
                # A failure after replace can still be proven committed by the complete envelope.
                try:
                    if self._load() == value:
                        self.runtime.emit('presets.changed', {'revision': value['revision']})
                        return
                except ServiceError:
                    pass
                raise ServiceError(503, 'preset:write_failed', outcome='unknown') from None
        self.runtime.emit('presets.changed', {'revision': value['revision']})

    def _get(self, value, identifier):
        uuid_string(identifier)
        for builtin in builtins():
            if builtin['preset_id'] == identifier:
                return builtin
        if identifier not in value['presets']:
            raise ServiceError(404, 'preset:not_found')
        return view(value['presets'][identifier])

    def get(self, identifier):
        with self.lock:
            return self._get(self._load(), identifier)

    def list(self, *, limit=50, cursor=None):
        with self.lock:
            value = self._load()
            rows = builtins() + [view(value['presets'][key]) for key in sorted(value['presets'])]
            version = digest(rows)
            position = self.runtime.cursors.decode(cursor, 'generation-presets') if cursor else None
            if position and position['version'] != version:
                raise ServiceError(409, 'page:stale')
            start = position['offset'] if position else 0
            more = start + limit < len(rows)
            return {'items': rows[start:start + limit], 'total': len(rows), 'limit': limit, 'version': version,
                    'has_more': more, 'default_preset_id': BUILTIN_IDS['small'],
                    'next_cursor': self.runtime.cursors.encode({'identity': 'generation-presets', 'version': version,
                        'offset': start + limit}) if more else None}

    def create(self, body):
        body = PresetInput.model_validate(body).model_dump()
        with self.lock:
            value = self._load()
            if len(value['presets']) >= MAX_PRESETS:
                raise ServiceError(413, 'preset:storage_limit')
            identifier = str(uuid4())
            value['presets'][identifier] = {'preset_id': identifier, **body, 'revision': 1}
            value['revision'] += 1
            self._commit(value)
            return view(value['presets'][identifier])

    def update(self, identifier, body, expected):
        body = PresetInput.model_validate(body).model_dump()
        with self.lock:
            value = self._load()
            current = self._get(value, identifier)
            if current['builtin']:
                raise ServiceError(409, 'preset:builtin_locked')
            Versions.require(expected, current['version'])
            if all(current[key] == body[key] for key in ('name', 'sizes')):
                return current
            value['presets'][identifier] = {'preset_id': identifier, **body, 'revision': current['revision'] + 1}
            value['revision'] += 1
            self._commit(value)
            return view(value['presets'][identifier])

    def delete(self, identifier, expected):
        with self.lock:
            value = self._load()
            current = self._get(value, identifier)
            if current['builtin']:
                raise ServiceError(409, 'preset:builtin_locked')
            Versions.require(expected, current['version'])
            del value['presets'][identifier]
            value['revision'] += 1
            self._commit(value)
