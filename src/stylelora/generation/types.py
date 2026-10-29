from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from ..api.contracts import StrictModel, Digest, Identifier
from ..project_db.validation import project_data


class CostPolicyPatch(StrictModel):
    cost_policy: Literal['deny', 'ask', 'allow']
    confirm_allow: bool = False


class CostConfirmation(StrictModel):
    cycle_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class ManualStopConfirmation(StrictModel):
    confirm_stop_source: bool = Field(strict=True)
    acknowledge_unknown_charge: bool = Field(strict=True)

    @model_validator(mode='after')
    def confirmed(self):
        if not self.confirm_stop_source or not self.acknowledge_unknown_charge:
            raise ValueError('Both confirmations are required')
        return self


class RecoveryAction(StrictModel):
    item_id: int = Field(ge=1, strict=True)
    action: Literal['reconcile', 'restore']
    confirm_restore: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def confirmation(self):
        if self.confirm_restore != (self.action == 'restore'):
            raise ValueError('Only restore requires explicit confirmation')
        return self


class Pixels(StrictModel):
    width: int = Field(ge=64, le=16384, multiple_of=64)
    height: int = Field(ge=64, le=16384, multiple_of=64)


def validate_sizes(sizes):
    if set(sizes) != {'landscape', 'portrait', 'square'}:
        raise ValueError('All three concrete sizes are required')
    for name, size in sizes.items():
        if (name == 'landscape' and size.width <= size.height or name == 'portrait' and size.width >= size.height
                or name == 'square' and size.width != size.height):
            raise ValueError('Size orientation differs from aspect')
    return sizes


class Profile(StrictModel):
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=100)
    sampler: str = Field(min_length=1, max_length=100)
    steps: int = Field(ge=1, le=1000)
    scale: float = Field(gt=0, le=1000)
    cfg_rescale: float = Field(ge=0, le=1)
    noise_schedule: str = Field(min_length=1, max_length=100)
    uc_preset: None = None
    quality_toggle: Literal[False] = False
    smea: bool
    smea_dyn: bool
    variety_plus: bool
    decrisp: bool

    @field_validator('quality_toggle', mode='before')
    @classmethod
    def fixed_quality(cls, value):
        if value is not False:
            raise ValueError('quality_toggle must be false')
        return value

    @model_validator(mode='after')
    def nonblank(self):
        if any(not getattr(self, k).strip() for k in ('model', 'effort', 'sampler', 'noise_schedule')):
            raise ValueError('Required profile value is empty')
        return self


class Configuration(StrictModel):
    schema_version: Literal[1] = 1
    backend: Literal['nai_launcher', 'nai_direct'] = 'nai_launcher'
    profile: Profile
    sizes: dict[str, Pixels]
    default_aspect: Literal['landscape', 'portrait', 'square']

    @field_validator('schema_version', mode='before')
    @classmethod
    def version_one(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('Unsupported version')
        return value

    @model_validator(mode='after')
    def sizes_valid(self):
        validate_sizes(self.sizes)
        try:
            project_data(self.model_dump())
        except Exception:
            raise ValueError('Configuration contains external locations or credentials') from None
        return self


class Draft(StrictModel):
    name: str = Field(min_length=1, max_length=512)
    config: Configuration
    style_prompt: str = Field(default='', max_length=16384)
    negative_prompt: str = Field(default='', max_length=16384)
    target_count: int = Field(ge=1, le=10000)
    cost_policy: Literal['deny', 'ask', 'allow'] = 'ask'

    @model_validator(mode='after')
    def name_valid(self):
        if not self.name.strip():
            raise ValueError('Name is empty')
        return self


class HandoffPreview(StrictModel):
    task_id: Identifier
    expected_task_revision: int = Field(ge=1)


class Projection(StrictModel):
    source_digest: Digest
    seeds_digest: Digest
    count: int = Field(ge=1, le=10000)


class HandoffStart(Projection):
    preview_id: Identifier
    digest: Digest


class GenerationDriver(Protocol):
    """Internal extension point, never supplied through HTTP.

    A production implementation owns capability validation, F01 cost checks,
    attempt receipts and artifact registration. Shared-setting backends additionally
    require settings leases. DirectDriver uses independent request-scoped state;
    SerialDriver remains injection-only for shared-setting adapters.
    dispatch only enqueues bounded work and must
    acquire its own project lease; every write observes the source lifecycle.
    """
    def capabilities(self) -> dict: ...
    def is_active(self, context, source_id: int) -> bool: ...
    def dispatch(self, context, source_id: int) -> None: ...
    def stop(self, context, source_id: int) -> None: ...
