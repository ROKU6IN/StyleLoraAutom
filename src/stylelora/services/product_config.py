"""Versioned non-secret defaults shared by UI, CLI and service requests."""
from copy import deepcopy
from pathlib import Path
import threading
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..api.contracts import StrictModel
from ..llm.secrets import safe_path
from ..project_db.storage import atomic_json
from ..project_db.validation import digest
from ..runtime.contracts import strict_json
from ..runtime.errors import ServiceError

MIB = 1024**2
MAX_THRESHOLD = 1024**5
Threshold = Annotated[int, Field(ge=MIB, le=MAX_THRESHOLD, multiple_of=MIB)]
DEFAULTS = {'project_bytes': 20*1024**3, 'total_bytes': 100*1024**3}


class Thresholds(StrictModel):
    project_bytes: Threshold
    total_bytes: Threshold


class ThresholdPatch(StrictModel):
    project_bytes: Threshold | None = None
    total_bytes: Threshold | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('At least one threshold is required; null is not supported')
        return self


class ProductPatch(StrictModel):
    storage_thresholds: ThresholdPatch | None = None
    cost_policy: Literal['deny', 'ask', 'allow'] | None = None
    confirm_allow: bool = False
    llm_limits: 'LlmLimitsPatch | None' = None
    wd_enabled: bool | None = None
    mcp: 'McpSettings | None' = None

    @model_validator(mode='after')
    def valid_patch(self):
        fields = self.model_fields_set - {'confirm_allow'}
        if not fields or any(getattr(self, key) is None for key in fields):
            raise ValueError('A non-null setting is required')
        if self.cost_policy == 'allow' and not self.confirm_allow:
            raise ValueError('Allowing cost requires explicit confirmation')
        if self.confirm_allow and self.cost_policy != 'allow':
            raise ValueError('Confirmation is only valid with allow')
        return self


class LlmLimits(StrictModel):
    concurrency: Annotated[int, Field(ge=1, le=8)] = 2
    timeout_s: Annotated[int, Field(ge=5, le=600)] = 300


class LlmLimitsPatch(StrictModel):
    concurrency: Annotated[int, Field(ge=1, le=8)] | None = None
    timeout_s: Annotated[int, Field(ge=5, le=600)] | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('At least one non-null limit is required')
        return self


class McpSettings(StrictModel):
    discovery_mode: Literal['default', 'configured'] = 'default'


class Settings(StrictModel):
    schema_version: Literal[2] = 2
    revision: Annotated[int, Field(ge=0)] = 0
    storage_thresholds: Thresholds
    cost_policy: Literal['deny', 'ask', 'allow'] = 'ask'
    llm_limits: LlmLimits = Field(default_factory=LlmLimits)
    wd_enabled: bool = False
    mcp: McpSettings = Field(default_factory=McpSettings)


ProductPatch.model_rebuild()


class ProductConfig:
    def __init__(self, runtime, directory=None):
        self.runtime, self.lock = runtime, threading.RLock()
        self.path = Path(directory)/'product-config.json' if directory is not None else None
        self.memory = self.defaults()

    @staticmethod
    def defaults():
        return Settings(storage_thresholds=Thresholds(**DEFAULTS)).model_dump()

    def _load(self):
        if self.path is None:
            return deepcopy(self.memory)
        try:
            path = safe_path(self.path)
            if not path.exists():
                return self.defaults()
            if path.stat().st_size > 16384:
                raise ValueError()
            value = strict_json(path.read_bytes())
            if set(value) != {'content', 'digest'} or digest(value['content']) != value['digest']:
                raise ValueError()
            content = value['content']
            if type(content.get('schema_version')) is not int:
                raise ValueError()
            if content['schema_version'] == 1:
                if set(content) != {'schema_version', 'revision', 'storage_thresholds'}:
                    raise ValueError()
                # Read-only migration. The next explicit changed PATCH commits
                # v2; merely reading or a no-op does not rewrite a v1 file.
                content = {**content, 'schema_version': 2}
                return Settings.model_validate(content).model_dump()
            parsed = Settings.model_validate(content).model_dump()
            if parsed != content:
                raise ValueError()
            return parsed
        except OSError:
            raise ServiceError(503, 'config:unreadable') from None
        except Exception:
            raise ServiceError(503, 'config:invalid_storage') from None

    def view(self, value):
        return {**deepcopy(value), 'version': digest(value),
                'capabilities': {'storage_thresholds': True, 'product_defaults': True},
                'limits': {'minimum_bytes': MIB, 'maximum_bytes': MAX_THRESHOLD, 'multiple_bytes': MIB}}

    def get(self):
        with self.lock:
            return self.view(self._load())

    def _commit(self, value):
        if self.path is None:
            self.memory = deepcopy(value)
            return
        try:
            path = safe_path(self.path)
            safe_path(path.parent).mkdir(parents=True, exist_ok=True)
            atomic_json(path, {'content': value, 'digest': digest(value)})
        except Exception:
            try:
                if self._load() == value:
                    return
            except ServiceError:
                pass
            raise ServiceError(503, 'config:write_unknown', outcome='unknown') from None

    def update(self, patch, expected):
        body = ProductPatch.model_validate(patch).model_dump(exclude_unset=True, exclude={'confirm_allow'})
        with self.lock:
            value = self._load()
            if expected is None:
                raise ServiceError(428, 'concurrency:precondition_required')
            if expected != digest(value):
                raise ServiceError(409, 'config:revision_conflict')
            updated = deepcopy(value)
            for key, item in body.items():
                if isinstance(item, dict):
                    updated[key].update(item)
                else:
                    updated[key] = item
            if updated == value:
                return self.view(value)
            value = updated
            value['revision'] += 1
            self._commit(value)
            result = self.view(value)
        self.runtime.emit('config.changed', {'revision': value['revision']})
        return result
