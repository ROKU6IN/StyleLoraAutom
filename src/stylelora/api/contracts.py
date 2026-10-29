from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..tagging.predictor import TaggerConfig
from ..vision.policy import VisionConfig

Positive = Annotated[int, Field(gt=0)]
Identifier = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Review = Literal["keep", "unreviewed", "reject"]
Layer = Literal["original", "wd", "llm", "manual", "rule"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Empty(StrictModel):
    pass


class ItemRetry(StrictModel):
    expected_attempt: int = Field(ge=1)


class TaggerInstall(StrictModel):
    manifest_id: Digest


class JobFilter(StrictModel):
    source_ids: list[Positive] | None = Field(default=None, max_length=1000)
    review_statuses: list[Review] | None = Field(default=None, max_length=3)
    include_missing: bool = True
    rating: list[Literal['general', 'sensitive', 'questionable', 'explicit']] | None = Field(default=None, max_length=4)
    file_missing: bool | None = None
    q: str | None = Field(default=None, max_length=4096)


class JobScope(StrictModel):
    selected_ids: list[Positive] | None = Field(default=None, max_length=5000)
    filter: JobFilter | None = None


class CombinedJobConfig(StrictModel):
    wd: TaggerConfig
    llm_vision: VisionConfig


class JobPreview(StrictModel):
    executors: list[Literal['wd', 'llm_vision']] = Field(min_length=1, max_length=2)
    mode: Literal['selected', 'uncovered', 'all']
    scope: JobScope
    config: TaggerConfig | VisionConfig | CombinedJobConfig

    @model_validator(mode='after')
    def matching_config(self):
        if len(set(self.executors)) != len(self.executors):
            raise ValueError('Duplicate executor')
        self.executors.sort(key=lambda name: 0 if name == 'wd' else 1)
        expected = CombinedJobConfig if len(self.executors) == 2 else TaggerConfig if self.executors == ['wd'] else VisionConfig
        if not isinstance(self.config, expected):
            raise ValueError('Executor and configuration must match')
        return self


class JobBatch(JobPreview):
    batch_id: Identifier
    preview_scope_digest: Digest | None = None


class Reclassify(StrictModel):
    scope: JobScope
    config: VisionConfig = Field(default_factory=VisionConfig)


class JobControl(StrictModel):
    action: Literal['pause', 'resume', 'cancel']


class JobRetry(StrictModel):
    batch_id: Identifier
    preview_digest: Digest


class ManualRating(StrictModel):
    rating: Literal['general', 'sensitive', 'questionable', 'explicit']


class Bootstrap(StrictModel):
    code: str = Field(min_length=1, max_length=256)


class Shutdown(StrictModel):
    grace_seconds: int = Field(default=30, ge=0, le=30)


class CreateProject(StrictModel):
    grant_id: Identifier
    name: str = Field(min_length=1, max_length=512)
    trigger_word: str = Field(default="", max_length=1024)


class OpenProject(StrictModel):
    grant_id: Identifier | None = None
    known_id: Identifier | None = None

    @model_validator(mode='after')
    def one_source(self):
        if (self.grant_id is None) == (self.known_id is None):
            raise ValueError('Choose one project reference')
        return self


class ProjectPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    trigger_word: str | None = Field(default=None, max_length=1024)


class PickerRequest(StrictModel):
    purpose: Literal["project_create", "project_open", "import_root"]
    project_id: Identifier | None = None


class TagInput(StrictModel):
    tag: str = Field(min_length=1, max_length=4096)
    conf: float | None = Field(default=None, ge=0, le=1)


class Ref(StrictModel):
    image_id: Positive
    source: Layer
    type: Literal["tags", "nl"]
    annotation_id: Positive | None


class ManualEdit(StrictModel):
    tags: list[TagInput] | None = Field(default=None, max_length=10000)
    natural_language: str | None = Field(default=None, max_length=1024*1024)
    write_nl: bool = False
    read_set: list[Ref] = Field(min_length=8, max_length=40000)


class BulkEdit(StrictModel):
    ids: list[Positive] = Field(min_length=1)
    add: list[str] = Field(default_factory=list, max_length=10000)
    remove: list[str] = Field(default_factory=list, max_length=10000)
    read_set: list[Ref] = Field(min_length=8, max_length=40000)
    expected_versions: dict[str, str]


class ImageQuery(StrictModel):
    source_ids: list[Positive] = Field(default_factory=list)
    review_statuses: list[Review] = Field(default_factory=list)
    rating: list[Literal["general", "sensitive", "questionable", "explicit"]] = Field(default_factory=list)
    file_missing: bool | None = None
    q: str | None = Field(default=None, max_length=4096)


class ReviewPreview(StrictModel):
    query: ImageQuery


class ReviewInput(StrictModel):
    ids: list[Positive] | None = Field(default=None, min_length=1)
    target: Review | None = None
    targets: dict[str, Review] | None = None
    expected_versions: dict[str, str] | None = None
    query: ImageQuery | None = None
    expected_total: int | None = Field(default=None, ge=0)
    preview_id: Identifier | None = None
    digest: Digest | None = None


class ImportOptionsInput(StrictModel):
    encoding: Literal["utf-8-sig", "utf-8", "gbk", "auto"] = "utf-8-sig"
    parser: Literal["literal-tags-v1", "catalog-comma-v1"] = "literal-tags-v1"
    recursive: bool = True
    max_image_bytes: int = Field(default=100*1024*1024, gt=0, le=100*1024*1024)
    max_pixels: int = Field(default=100_000_000, gt=0, le=100_000_000)
    max_sidecar_bytes: int = Field(default=1024*1024, gt=0, le=1024*1024)
    max_files: int = Field(default=10000, gt=0, le=10000)


class ImportPreview(StrictModel):
    root_handle: Identifier
    scheme_id: Positive | None = None
    options: ImportOptionsInput | None = None


class ImportStart(StrictModel):
    preview_id: Identifier
    digest: Digest
    name: str = Field(default="Local images", min_length=1, max_length=512)


class RootInput(StrictModel):
    root_handle: Identifier


class SourceControl(StrictModel):
    action: Literal["pause", "resume", "cancel"]


class ExportOptionsInput(StrictModel):
    include_nl: bool | None = None
    include_unreviewed: bool | None = None
    include_json: bool | None = None
    source_ids: list[Positive] | None = None
    blocked_tags: list[str] = Field(default_factory=list, max_length=10000)
    blocklist_mode: Literal["remove", "warn"] | None = None
    allow_existing_trigger: bool = False


class ExportPreview(StrictModel):
    scheme_id: Positive | None = None
    options: ExportOptionsInput = Field(default_factory=ExportOptionsInput)


class ExportStart(StrictModel):
    scheme_id: Positive | None = None
    preview_id: Identifier
    digest: Digest
    trigger_override_digest: Digest | None = None
    hardlink_ack_digest: Digest | None = None


class DefaultScheme(StrictModel):
    expected_versions: dict[str,str]


class TriggerValidation(StrictModel):
    trigger: str = Field(max_length=4096)
    project_id: Identifier | None = None
    style_prompts: list[str] | None = Field(default=None,max_length=1000)


class ScopeInput(StrictModel):
    selected_ids: list[Positive] | None = None
    filter: ImageQuery | None = None


class DuplicateScanInput(StrictModel):
    scope: ScopeInput
    threshold: int = Field(ge=0, le=64)


class RuleGroup(StrictModel):
    mode: Literal['all', 'any']
    tags: list[str] = Field(max_length=10000)


class RuleSpec(StrictModel):
    schema_version: Literal[1]
    condition: RuleGroup | None
    delete: RuleGroup | None
    write: RuleGroup | None


class RulePreviewInput(StrictModel):
    rule: RuleSpec
    scope: ScopeInput


class ApplyPreview(StrictModel):
    preview_id: Identifier
    digest: Digest


class BulkPreviewInput(StrictModel):
    scope: ScopeInput
    add: list[str] = Field(default_factory=list, max_length=10000)
    remove: list[str] = Field(default_factory=list, max_length=10000)


class StatsInput(StrictModel):
    scope: ScopeInput


class TranslationInput(StrictModel):
    tag: str = Field(min_length=1, max_length=4096)
    value: str = Field(max_length=4096)


class TranslationLookup(StrictModel):
    tags: list[str] = Field(max_length=1000)
    provider: Literal['local'] = 'local'


class SavedRule(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=512)
    rule: RuleSpec


class RuleLibraryInput(StrictModel):
    schema_version: Literal[1]
    kind: Literal['replacement_rules']
    rules: list[SavedRule] = Field(max_length=1000)
