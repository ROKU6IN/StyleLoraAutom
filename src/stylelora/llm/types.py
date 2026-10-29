import math
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..runtime.errors import ServiceError

Protocol = Literal['openai_chat', 'openai_responses', 'anthropic_messages']
Role = Literal['prompt', 'prompt_nsfw', 'vision', 'vision_nsfw', 'rating', 'clean', 'clean_nsfw']
ROLES = ('prompt', 'prompt_nsfw', 'vision', 'vision_nsfw', 'rating', 'clean', 'clean_nsfw')
Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r'^[^\x00-\x20\x7f]+$')]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ModelConfig(Strict):
    name: Name
    supports_images: bool = False
    capability_source: Literal['manual', 'models', 'tested'] = 'manual'
    tested_at: str | None = None
    response_aliases: list[Name] = Field(default_factory=list, max_length=20)


class EndpointInput(Strict):
    base_url: str = Field(min_length=1, max_length=2048)
    protocol: Protocol
    models: list[ModelConfig] = Field(default_factory=list, max_length=1000)
    supports_json_schema: bool = False

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or '?' in value or '#' in value or '\\' in value
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError('Invalid endpoint URL')
        _ = parsed.port  # Also reject malformed ports.
        return value.rstrip('/')

    @model_validator(mode='after')
    def unique(self):
        if len({m.name for m in self.models}) != len(self.models):
            raise ValueError('Duplicate model')
        return self


class BindingInput(Strict):
    endpoint_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    model: Name


class RolesInput(Strict):
    roles: dict[Role, BindingInput | None]


class SecretInput(Strict):
    value: str = Field(min_length=1, max_length=8192, repr=False, pattern=r'^[\x21-\x7e]+$')


class ImageTestInput(Strict):
    model: Name


class LlmFailure(ServiceError):
    def __init__(self, code, *, retryable=False, outcome='rejected'):
        super().__init__(503, 'llm:' + code, outcome=outcome)
        self.retryable = retryable
        self.safe_message = self.message


@dataclass(frozen=True)
class RoleBinding:
    binding_id: str
    role: str
    model: str
    protocol: str
    revision: int


@dataclass
class RepairBudget:
    limit: int = 1
    used: int = 0
    _lock: object = field(default_factory=threading.Lock, repr=False)

    def take(self):
        with self._lock:
            if self.used >= min(self.limit, 1):
                return False
            self.used += 1
            return True


@dataclass(frozen=True)
class LlmRequest:
    request_id: str
    binding: RoleBinding
    system_text: str
    payload: object
    response_schema: dict | None
    timeout_s: float = 300
    transport_retry_limit: int = 2
    images: tuple = ()
    cancel: object = None
    timeout_policy: str = 'fail_unknown'
    tiers: tuple = ()
    image_input: bytes | None = field(default=None, repr=False)
    sampling: object = field(default_factory=lambda: MappingProxyType({}))
    repair_budget: RepairBudget | None = None
    initial_tier: int = 0

    def __post_init__(self):
        if (not self.request_id or len(self.request_id) > 128 or not math.isfinite(self.timeout_s)
                or not 0 < self.timeout_s <= 300 or not 0 <= self.transport_retry_limit <= 5
                or len(self.images) > 1 or (self.images and self.image_input is not None)
                or type(self.initial_tier) is not int or self.initial_tier not in (0, 1, 2)
                or self.timeout_policy not in ('fail_unknown', 'vision_downgrade')
                or (self.timeout_policy == 'vision_downgrade' and self.image_input is None)):
            raise LlmFailure('invalid_request', outcome='not_sent')
        if self.tiers and self.tiers != (0, 1, 2):
            raise LlmFailure('invalid_tiers', outcome='not_sent')
        if set(self.sampling) - {'temperature', 'top_p', 'max_tokens'}:
            raise LlmFailure('unsupported_sampling', outcome='not_sent')
        for key, value in self.sampling.items():
            limit = (1, 32768) if key == 'max_tokens' else (0, 2 if key == 'temperature' else 1)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or not limit[0] <= value <= limit[1] or (key == 'max_tokens' and not isinstance(value, int))):
                raise LlmFailure('unsupported_sampling', outcome='not_sent')
        object.__setattr__(self, 'sampling', MappingProxyType(dict(self.sampling)))


@dataclass(frozen=True)
class LlmReply:
    text: str
    actual_model: str
    request_id: str
    usage: dict | None = None
    actual_tier: int | None = None
    attempts: int = 1


@dataclass(frozen=True)
class JsonReply:
    value: object
    reply: LlmReply
    repair_used: bool
