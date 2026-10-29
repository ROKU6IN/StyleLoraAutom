import copy
from pathlib import Path
import threading
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..llm.secrets import safe_path
from ..llm.types import Strict
from ..project_db.storage import atomic_json
from ..project_db.validation import digest, normalize_tag
from ..runtime.contracts import strict_json, Versions
from ..runtime.errors import ServiceError

RATINGS = ('general', 'sensitive', 'questionable', 'explicit')


class Keyword(Strict):
    tag: str = Field(min_length=1, max_length=200)
    rating: Literal['general', 'sensitive', 'questionable', 'explicit']

    @field_validator('tag')
    @classmethod
    def normalized(cls, value):
        return normalize_tag(value)


class PolicyInput(Strict):
    keywords: list[Keyword] = Field(default_factory=list, max_length=10000)
    blocked_terms: list[str] = Field(default_factory=lambda: [
        'watercolor', 'oil painting', 'brush strokes', 'brushstroke', 'art style', 'artist style',
        'masterpiece', 'best quality', '水彩', '油画', '笔触', '画风'], max_length=10000)

    @model_validator(mode='after')
    def unique(self):
        if len({entry.tag for entry in self.keywords}) != len(self.keywords):
            raise ValueError('Duplicate keyword')
        self.blocked_terms = sorted({normalize_tag(term) for term in self.blocked_terms})
        if any(len(term) > 200 for term in self.blocked_terms):
            raise ValueError('Term too long')
        self.keywords.sort(key=lambda entry: entry.tag)
        return self


class VisionConfig(Strict):
    dataset_mode_policy: Literal['style'] = 'style'
    use_rating_model: bool = False
    fallback: Literal['normal', 'nsfwfallback'] = 'normal'
    max_tokens: int = Field(default=2048, ge=128, le=8192)
    timeout_s: int = Field(default=300, ge=1, le=300)
    transport_retry_limit: int = Field(default=2, ge=0, le=2)


class PolicyStore:
    def __init__(self, runtime, directory=None):
        self.runtime, self.lock = runtime, threading.RLock()
        self.directory = safe_path(Path(directory)/'vision-policy') if directory is not None else None
        self.values = {}
        value = PolicyInput().model_dump()
        if self.directory and (self.directory/'current.json').exists():
            try:
                path = safe_path(self.directory/'current.json')
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                value = PolicyInput.model_validate(strict_json(path.read_bytes())).model_dump()
            except Exception:
                raise ServiceError(503, 'vision:policy_unavailable') from None
        self.current = value
        self.values[digest(value)] = value

    def view(self):
        with self.lock:
            return {**copy.deepcopy(self.current), 'hash': digest(self.current),
                    'version': self.runtime.versions.get('vision:policy', self.current)}

    def save(self, value, expected):
        with self.lock:
            if self.runtime.draining:
                raise ServiceError(503, 'process:draining')
            Versions.require(expected, self.view()['version'])
            value = value.model_dump()
            if value != self.current:
                if self.directory:
                    safe_path(self.directory).mkdir(parents=True, exist_ok=True)
                    atomic_json(safe_path(self.directory/(digest(self.current)+'.json')), self.current)
                    atomic_json(safe_path(self.directory/(digest(value)+'.json')), value)
                    atomic_json(safe_path(self.directory/'current.json'), value)
                self.values[digest(value)] = value
                self.current = value
                self.runtime.emit('llm.changed', {'resource': 'vision_policy'})
            return self.view()

    def load(self, checksum):
        with self.lock:
            if checksum in self.values:
                return copy.deepcopy(self.values[checksum])
            if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
                raise ServiceError(503, 'vision:policy_unavailable')
            try:
                path = safe_path(self.directory/(checksum+'.json'))
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                value = PolicyInput.model_validate(strict_json(path.read_bytes())).model_dump()
                if digest(value) != checksum:
                    raise ValueError()
                self.values[checksum] = value
                return copy.deepcopy(value)
            except Exception:
                raise ServiceError(503, 'vision:policy_unavailable') from None


def keyword_rating(tags, policy):
    table = {item['tag']: item['rating'] for item in policy['keywords']}
    matches = [table[normalize_tag(tag)] for tag in tags if normalize_tag(tag) in table]
    return max(matches, key=RATINGS.index) if matches else None
