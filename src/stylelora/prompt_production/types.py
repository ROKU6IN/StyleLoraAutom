from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..tagging.plain import validate_plain_tag

Rating = Literal['general', 'sensitive', 'questionable', 'explicit']
Aspect = Literal['landscape', 'portrait', 'square']
Identifier = Annotated[str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


def tag(value):
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError('Invalid tag')
    try:
        return ' '.join(validate_plain_tag(value).split())
    except Exception:
        raise ValueError('Plain content tag required') from None


def dimension(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid dimension')
    return value.strip()


class Feature(Strict):
    tag: str
    rating: Rating | None = None
    _tag = field_validator('tag')(tag)


class Entry(Strict):
    tags: list[str] = Field(min_length=1, max_length=100)
    natural_language: str | None = Field(default=None, max_length=4096)
    aspect: Aspect | None = None
    rating: Rating | None = None

    @field_validator('tags')
    @classmethod
    def tags_valid(cls, values):
        return list(dict.fromkeys(tag(value) for value in values))

    @field_validator('natural_language')
    @classmethod
    def nl_valid(cls, value):
        if value is not None and any(ord(c) < 32 and c not in '\n\r\t' for c in value):
            raise ValueError('Invalid text')
        return value.strip() or None if value is not None else None


class DictionaryInput(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    kind: Literal['features', 'prompts']
    name: str = Field(min_length=1, max_length=200)
    dimensions: dict[str, list[Feature]] | None = None
    entries: list[Entry] | None = Field(default=None, max_length=10000)

    @field_validator('name')
    @classmethod
    def name_valid(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid name')
        return value.strip()

    @model_validator(mode='after')
    def payload(self):
        if self.kind == 'features':
            if self.entries is not None or self.dimensions is None or not 1 <= len(self.dimensions) <= 20:
                raise ValueError('Feature dimensions required')
            if not 1 <= sum(len(v) for v in self.dimensions.values()) <= 10000:
                raise ValueError('Feature count out of bounds')
            if any(not rows for rows in self.dimensions.values()):
                raise ValueError('Empty dimension')
            names = [dimension(key) for key in self.dimensions]
            if len(set(names)) != len(names):
                raise ValueError('Duplicate dimension')
            self.dimensions = dict(zip(names, self.dimensions.values()))
        elif self.dimensions is not None or self.entries is None:
            raise ValueError('Prompt entries required')
        return self


class DictionaryCreate(DictionaryInput):
    dictionary_id: Identifier


class DictionaryFile(DictionaryCreate):
    revision: int = Field(ge=1)


class ImportDocument(DictionaryFile):
    """Validate the envelope first; diagnose individual entries separately."""
    dimensions: dict[str, list[object]] | None = None
    entries: list[object] | None = Field(default=None, max_length=10000)


class Constraint(Strict):
    dimension: str
    tag: str
    minimum_ratio: float | None = Field(default=None, ge=0, le=1)
    maximum_ratio: float | None = Field(default=None, ge=0, le=1)
    minimum_count: int | None = Field(default=None, ge=0, le=10000)
    maximum_count: int | None = Field(default=None, ge=0, le=10000)
    _tag = field_validator('tag')(tag)
    _dimension = field_validator('dimension')(dimension)

    @field_validator('minimum_ratio', 'maximum_ratio', mode='before')
    @classmethod
    def ratio_valid(cls, value):
        if isinstance(value, bool):
            raise ValueError('Boolean is not a ratio')
        return value


class Predicate(Strict):
    all_tags: list[str] = Field(default_factory=list, max_length=100)
    none_tags: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('all_tags', 'none_tags')
    @classmethod
    def normalize(cls, values):
        return sorted(set(map(tag, values)))


class Plan(Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    algorithm_version: Literal['coverage-v1'] = 'coverage-v1'
    primary_dimensions: list[str] = Field(default_factory=lambda: ['scene', 'lighting'], min_length=2, max_length=3)
    cell_values: dict[str, list[str]] = Field(default_factory=lambda: {
        'scene': ['forest', 'beach', 'city street'], 'lighting': ['sunlight', 'night']})
    cell_weights: dict[str, int] = Field(default_factory=dict)
    exclude: list[Predicate] = Field(default_factory=list, max_length=100)
    # Explicit template rule, never inferred from a user's dimension names.
    no_humans_excludes: list[str] = Field(default_factory=list, max_length=20)
    target_count: int = Field(default=100, ge=1, le=10000)
    random_seed: int | None = Field(default=None, ge=0, le=2**64-1)
    constraints: list[Constraint] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def fields_valid(self):
        names = [dimension(v) for v in self.primary_dimensions]
        if len(set(names)) != len(names) or set(self.cell_values) != set(names):
            raise ValueError('Primary dimensions must match cell values')
        self.primary_dimensions = names
        self.no_humans_excludes = list(dict.fromkeys(dimension(v) for v in self.no_humans_excludes))
        if set(names) & set(self.no_humans_excludes):
            raise ValueError('Cannot suppress primary dimensions')
        count = 1
        for key, values in self.cell_values.items():
            normalized = list(dict.fromkeys(tag(v) for v in values))
            if not normalized or len(normalized) != len(values):
                raise ValueError('Empty or duplicate cell value')
            self.cell_values[key] = normalized
            count *= len(values)
        if count > 10000 or len(self.cell_weights) > 10000 or any(type(v) is not int or not 1 <= v <= 100000 for v in self.cell_weights.values()):
            raise ValueError('Matrix too large or invalid weight')
        return self


class Fence(Strict):
    enabled: bool = True
    threshold: Rating = 'sensitive'
    nsfwfallback: bool = False
    use_rating_model: bool = False


class Options(Strict):
    nl_enabled: bool = True
    composition_mode: Literal['normal', 'scenery_first'] = 'normal'
    tag_range: list[int] = Field(default_factory=lambda: [15, 40], min_length=2, max_length=2)
    aspect_hint: Aspect | None = None

    @field_validator('tag_range')
    @classmethod
    def range_valid(cls, value):
        if not 1 <= value[0] <= value[1] <= 100:
            raise ValueError('Invalid tag range')
        return value


class SamplingRequest(Strict):
    plan: Plan = Field(default_factory=Plan)
    coverage_enabled: bool = True
    prompt_dictionary_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    feature_dictionary_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    live_features: bool = False
    launcher_selection: dict | None = None
    source_weights: dict[str, int] = Field(default_factory=lambda: {'dictionary_pool': 1, 'feature_pool': 1})
    optimize: bool = False
    options: Options = Field(default_factory=Options)
    fence: Fence = Field(default_factory=Fence)
    append_target_id: Identifier | None = None

    @model_validator(mode='after')
    def sources_valid(self):
        if not self.prompt_dictionary_ids and not self.feature_dictionary_ids and not self.live_features:
            raise ValueError('Choose a source')
        if any(len(ids) != len(set(ids)) for ids in (self.prompt_dictionary_ids, self.feature_dictionary_ids)):
            raise ValueError('Duplicate source')
        if set(self.source_weights) != {'dictionary_pool', 'feature_pool'} or any(type(v) is not int or not 1 <= v <= 1000 for v in self.source_weights.values()):
            raise ValueError('Invalid source weights')
        return self


class TaskCreate(Strict):
    task_id: Identifier
    request: SamplingRequest
    preview_digest: Digest


class Control(Strict):
    action: Literal['pause', 'resume', 'cancel']
    expected_revision: int = Field(ge=1)


class Freeze(Strict):
    expected_revision: int = Field(ge=1)


class CleanupTask(Freeze):
    task_id: Identifier


class Cleanup(Strict):
    tasks: list[CleanupTask] = Field(min_length=1, max_length=200)

    @field_validator('tasks')
    @classmethod
    def unique_tasks(cls, rows):
        if len({row.task_id for row in rows}) != len(rows):
            raise ValueError('Duplicate task')
        return sorted(rows, key=lambda row: row.task_id)


class ResultsPreview(Freeze):
    format: Literal['json', 'csv', 'txt'] = 'json'


class ResultsExport(Strict):
    preview_id: Identifier
    digest: Digest
    acknowledge_lossy: bool = False


class ImportCommit(Strict):
    preview_id: Identifier
    digest: Digest


class ImportSelection(ImportCommit):
    rows: list[Annotated[int, Field(strict=True, ge=1, le=10001)]] = Field(min_length=1, max_length=10000)

    @field_validator('rows')
    @classmethod
    def unique_rows(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Duplicate import row')
        return sorted(values)


class AppendSelection(Strict):
    target_id: Identifier
    indices: list[int] = Field(min_length=1, max_length=10000)

    @field_validator('indices')
    @classmethod
    def unique_indices(cls, value):
        if len(value) != len(set(value)) or any(v < 0 or v >= 10000 for v in value):
            raise ValueError('Invalid selection')
        return sorted(value)


class AppendCommit(AppendSelection):
    expected_revision: int = Field(ge=1)
    target_version: Digest
