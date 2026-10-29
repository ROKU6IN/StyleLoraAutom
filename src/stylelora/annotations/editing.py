from dataclasses import dataclass

from ..exchange.errors import ExchangeError
from ..project_db import commands
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import BatchTooLarge, PreviewStale, ValidationError
from ..project_db.merge import merge_caption
from ..project_db.repositories import annotations, images
from ..project_db.rules import capture_read_set, edit_manual_nl, validate_read_set
from ..project_db.types import CurrentRef, ExportSchemeInput, Tag
from ..project_db.validation import digest, text
from ..tagging.plain import validate_plain_tag


@dataclass(frozen=True)
class AnnotationView:
    image_id: int
    filename: str
    review_status: str
    file_missing: bool
    tags: tuple[str, ...]
    natural_language: str | None
    refs: tuple[CurrentRef, ...]
    revision: str
    missing_original: bool
    layers: dict
    wd_overlap: dict[str, bool]


@dataclass(frozen=True)
class ManualResult:
    image_id: int
    changed: bool
    annotation_ids: tuple[int, ...]


def _view(db, image_id):
    image = images.get_image(db, image_id)
    config = dict(ExportSchemeInput("editing").caption_config_json)
    config["wd_null_confidence"] = "include"
    config["include_nl"] = True
    merged = merge_caption(db, image_id, config=config)
    refs = capture_read_set(db, (image_id,))
    layers = {f"{ref.source}/{ref.type}": annotations.get_current(db, image_id, ref.source, ref.type) for ref in refs}
    original, wd = layers["original/tags"], layers["wd/tags"]
    from ..project_db.validation import normalize_tag
    original_keys = {normalize_tag(tag.tag) for tag in original.content} if original else None
    overlap = {normalize_tag(tag.tag): original_keys is None or normalize_tag(tag.tag) in original_keys
               for tag in wd.content} if wd else {}
    return AnnotationView(image_id, image.filename, image.review_status, image.file_missing, merged.tags,
                          merged.natural_language, refs, digest(refs), original is None, layers, overlap)


def get_annotation_view(project, image_id):
    with read_snapshot(project) as db:
        return _view(db, image_id)


def _edit(db, image_id, desired, natural_language, write_nl, refs):
    validate_read_set(db, refs, (image_id,))
    changed, ids = False, []
    if desired is not None:
        normalized = tuple(Tag(validate_plain_tag(value.tag), value.conf) for value in desired)
        result = commands.edit_manual_tags(db, image_id, tags=normalized, read_set=refs)
        changed, ids = result.changed, [result.value.annotation_id]
    db.project._fault("edit_after_tags")
    if write_nl:
        content = "" if natural_language is None else natural_language
        text(content, "natural language")
        if "\0" in content:
            raise ExchangeError("caption:nul_character")
        expected = next(ref for ref in refs if ref.image_id == image_id and ref.source == "manual" and ref.type == "nl")
        nl = edit_manual_nl(db, image_id, content=content, expected=expected)
        changed = changed or nl.changed
        ids.append(nl.value.annotation_id)
    return ManualResult(image_id, changed, tuple(ids))


def edit_manual(project, image_id, *, tags=None, natural_language=None, write_nl=False, expected_revision=None, read_set=None):
    if type(write_nl) is not bool:
        raise ValidationError("write_nl must be boolean")
    if tags is None and not write_nl:
        raise ValidationError("No annotation change was requested")
    view = get_annotation_view(project, image_id)
    if expected_revision is not None and expected_revision != view.revision:
        raise PreviewStale("Annotation revision changed")
    refs = tuple(read_set) if read_set is not None else view.refs
    desired = None if tags is None else tuple(
        value if isinstance(value, Tag) else Tag(value) for value in tags)
    return submit_write(project, lambda db: _edit(db, image_id, desired, natural_language, write_nl, refs)).result()


def bulk_edit_manual(project, image_ids, *, add=(), remove=(), expected=None):
    ids = tuple(dict.fromkeys(image_ids))
    if not ids or len(ids) > 5000:
        raise BatchTooLarge("Select between 1 and 5000 images")
    additions = tuple(dict.fromkeys(validate_plain_tag(value) for value in add))
    removals = {validate_plain_tag(value) for value in remove}
    if set(additions) & removals:
        raise ValidationError("Add and remove sets must be disjoint")
    with read_snapshot(project) as db:
        views = tuple(_view(db, image_id) for image_id in ids)
    if expected is not None and (set(expected) != set(ids) or any(expected[v.image_id] != v.revision for v in views)):
        raise PreviewStale("Batch annotation revisions changed")
    def apply(db):
        for view in views:
            validate_read_set(db, view.refs, (view.image_id,))
        results = []
        for view in views:
            target = [tag for tag in view.tags if tag not in removals]
            target.extend(tag for tag in additions if tag not in target)
            results.append(_edit(db, view.image_id, tuple(Tag(tag) for tag in target), None, False, view.refs))
        return tuple(results)
    return submit_write(project, apply).result()


def review_images(project, image_ids, target, *, expected=None):
    ids = tuple(dict.fromkeys(image_ids))
    if not ids or len(ids) > 5000:
        raise BatchTooLarge("Select between 1 and 5000 images")
    with read_snapshot(project) as db:
        current = {image_id: images.get_image(db, image_id).review_status for image_id in ids}
    if expected is not None and current != expected:
        raise PreviewStale("Review states changed")
    return submit_write(project, lambda db: commands.bulk_review(db, ids, expected=current, target=target)).result()
