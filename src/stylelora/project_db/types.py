"""Immutable, detached DTOs for the F01 public API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Literal, TypeAlias, TypeVar

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
SourceStatus: TypeAlias = Literal["draft", "running", "paused", "done", "canceled"]
ItemStatus: TypeAlias = Literal["pending", "sent", "done", "failed", "skipped"]
JobStatus: TypeAlias = Literal["pending", "running", "paused", "done", "failed", "canceled"]
ReviewStatus: TypeAlias = Literal["unreviewed", "keep", "reject"]
Layer: TypeAlias = Literal["original", "wd", "llm", "manual", "rule"]
AnnotationType: TypeAlias = Literal["tags", "nl"]
Aspect: TypeAlias = Literal["landscape", "portrait", "square"]
T = TypeVar("T")


class _Unset(Enum):
    VALUE = "UNSET"


UNSET = _Unset.VALUE


@dataclass(frozen=True, slots=True)
class ProjectMetadata:
    format_version: int
    name: str
    trigger_word: str
    created_at: str


@dataclass(frozen=True, slots=True)
class WriteResult(Generic[T]):
    value: T
    changed: bool


@dataclass(frozen=True, slots=True)
class PageRequest:
    after_id: int = 0
    limit: int = 200
    upper_id: int | None = None


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_after_id: int | None
    upper_id: int


@dataclass(frozen=True, slots=True)
class SourceDraft:
    type: Literal["import", "nai"]
    name: str
    config: JsonObject = field(default_factory=dict)
    cost_policy: Literal["deny", "ask", "allow"] = "ask"
    style_prompt: str = ""
    negative_prompt: str = ""
    target_count: int | None = None


@dataclass(frozen=True, slots=True)
class SourcePatch:
    name: str | None = None
    cost_policy: str | None = None
    type: str | _Unset = UNSET
    config: JsonObject | _Unset = UNSET
    style_prompt: str | _Unset = UNSET
    negative_prompt: str | _Unset = UNSET
    target_count: int | None | _Unset = UNSET


@dataclass(frozen=True, slots=True)
class SourceRow:
    source_id: int
    type: str
    name: str
    config_json: JsonObject
    cost_policy: str
    style_prompt: str
    negative_prompt: str
    target_count: int | None
    status: SourceStatus
    locked: bool
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ItemSeed:
    seq: int
    prompt: str
    aspect: Aspect | None = None


@dataclass(frozen=True, slots=True)
class SourceItemRow:
    item_id: int
    source_id: int
    seq: int
    prompt: str
    aspect: Aspect | None
    status: ItemStatus
    image_id: int | None
    attempts: int
    error: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class SourceFilter:
    statuses: tuple[SourceStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemFilter:
    source_id: int
    statuses: tuple[ItemStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageFilter:
    source_ids: tuple[int, ...] | None = None
    review_statuses: tuple[ReviewStatus, ...] | None = None
    include_missing: bool = False
    rating: tuple[str, ...] | None = None
    file_missing: bool | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class ImageRow:
    image_id: int
    filename: str
    width: int
    height: int
    phash: str | None
    sha256: str
    file_size: int
    source_id: int
    file_missing: bool
    review_status: ReviewStatus
    rating: str | None
    rating_source: str | None
    rating_verified: bool
    rated_at: str | None
    png_meta_json: JsonObject | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    item_id: int
    attempt: int
    relative_staging_path: str
    extension: str
    width: int
    height: int
    phash: str | None
    sha256: str
    file_size: int
    png_meta: JsonObject | None
    receipt_digest: str
    registration_check: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class Tag:
    tag: str
    conf: float | None = None


@dataclass(frozen=True, slots=True)
class AnnotationInput:
    image_id: int
    source: Layer
    type: AnnotationType
    content: tuple[Tag, ...] | str
    confidence: float | None = None
    model_name: str | None = None
    params: JsonObject = field(default_factory=dict)
    job_id: int | None = None
    cache_key: str | None = None


@dataclass(frozen=True, slots=True)
class OriginalInput:
    type: AnnotationType
    content: tuple[Tag, ...] | str
    confidence: float | None = None
    params: JsonObject = field(default_factory=lambda: {"schema_version": 1})
    cache_key: str | None = None


@dataclass(frozen=True, slots=True)
class AnnotationRow:
    annotation_id: int
    image_id: int
    source: Layer
    type: AnnotationType
    content: tuple[Tag, ...] | str
    confidence: float | None
    model_name: str | None
    params_json: JsonObject
    job_id: int | None
    cache_key: str | None
    created_at: str
    is_current: bool


@dataclass(frozen=True, slots=True)
class CurrentRef:
    image_id: int
    source: Layer
    type: AnnotationType
    annotation_id: int | None


@dataclass(frozen=True, slots=True)
class RatingValue:
    rating: str
    source: str
    verified: bool
    rated_at: str


@dataclass(frozen=True, slots=True)
class Scope:
    selected_ids: tuple[int, ...] | None = None
    filter: ImageFilter | None = None


@dataclass(frozen=True, slots=True)
class JobRequest:
    executor: Literal["wd", "llm_vision"]
    mode: Literal["selected", "uncovered", "all"]
    scope: Scope


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    model: str
    target_types: tuple[AnnotationType, ...] = ("tags",)
    role: str = "vision"
    protocol: str = "local"
    skill_hash: str = "none:wd-v1"
    params: JsonObject = field(default_factory=dict)
    routing: JsonObject = field(default_factory=dict)
    concurrency: int = 1
    timeout: int = 120
    retries: int = 0
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class JobRow:
    job_id: int
    executor: str
    batch_id: str | None
    mode: str
    scope_json: JsonObject
    config_snapshot_json: JsonObject
    progress_json: JsonObject
    status: JobStatus
    total: int
    done_count: int
    failed_count: int
    skipped_count: int
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobOutcome:
    image_id: int
    kind: Literal["done", "failed", "skipped"]
    annotations: tuple[AnnotationInput, ...] = ()
    reason: str | None = None
    annotation_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleImageChange:
    image_id: int
    tags: tuple[Tag, ...]
    previous_rule_id: int | None
    merge_meta: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RulePlan:
    rule: JsonObject
    scope: Scope
    changes: tuple[RuleImageChange, ...]
    read_set: tuple[CurrentRef, ...]
    preview_digest: str


@dataclass(frozen=True, slots=True)
class RuleApplyResult:
    op_id: int | None
    affected_count: int


@dataclass(frozen=True, slots=True)
class OperationRow:
    op_id: int
    rule_json: JsonObject
    scope_json: JsonObject
    affected_count: int
    changes_json: tuple[JsonObject, ...]
    status: str
    created_at: str
    undone_at: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class ExportSchemeInput:
    name: str
    exporter: str = "image_txt"
    filter_json: JsonObject = field(default_factory=lambda: {"schema_version": 1, "review_statuses": ["keep"]})
    caption_config_json: JsonObject = field(default_factory=lambda: {
        "schema_version": 1, "tag_layers": ["original", "wd", "llm", "manual", "rule"],
        "layer_priority": ["manual", "rule", "llm", "wd", "original"],
        "wd_min_confidence": 0.0, "wd_null_confidence": "exclude", "tag_sort": "layer_first",
        "include_nl": False, "nl_priority": ["manual", "llm", "original"], "trigger_first": True,
    })
    output_config_json: JsonObject = field(default_factory=lambda: {
        "schema_version": 1, "separator": ", ", "new_timestamp_directory": True, "mode": "copy",
    })
    pipeline_json: tuple[JsonObject, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportSchemeInput:
    name: str
    reader: str = "txt_sidecar"
    params_json: JsonObject = field(default_factory=lambda: {
        "schema_version": 1, "encoding": "utf-8-sig", "tag_separator": ",", "paragraph_separator": "\n\n",
    })
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class ExportSchemeRow:
    scheme_id: int
    name: str
    exporter: str
    filter_json: JsonObject
    caption_config_json: JsonObject
    output_config_json: JsonObject
    pipeline_json: tuple[JsonObject, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ImportSchemeRow:
    scheme_id: int
    name: str
    reader: str
    params_json: JsonObject
    is_default: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    item_id: int
    attempt: int
    verdict: Literal["complete", "absent", "unknown"]
    artifact: ImageArtifact | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    paused_source_ids: tuple[int, ...] = ()
    paused_job_ids: tuple[int, ...] = ()
    recovered_item_ids: tuple[int, ...] = ()
    failed_item_ids: tuple[int, ...] = ()
    unresolved_item_ids: tuple[int, ...] = ()
    missing_image_ids: tuple[int, ...] = ()
    orphan_filenames: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationInfo:
    from_version: int
    to_version: int
    name: str
    checksum: str
