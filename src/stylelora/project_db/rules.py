"""Transactional rule snapshots and manual edits; rule matching belongs to F09."""

from dataclasses import replace

from .errors import BatchTooLarge, InvalidChangeReference, PreviewStale, UndoConflict, ValidationError
from .merge import evidence_view
from .types import (
    AnnotationInput, CurrentRef, ExportSchemeInput, RuleApplyResult, RulePlan, Scope, Tag, WriteResult,
)
from .validation import LAYERS, digest, encode, normalize_tag, object_json, project_data, tags, utc_now
from .repositories import annotations, images, jobs, operations

MAX_RULE_IMAGES = 5000
MAX_RULE_BYTES = 16 * 1024 * 1024
READ_TARGETS = tuple((layer, "tags") for layer in LAYERS) + tuple(
    (layer, "nl") for layer in ("original", "llm", "manual")
)


def capture_read_set(db, image_ids):
    return tuple(annotations.current_ref(db, image_id, layer, kind)
                 for image_id in image_ids for layer, kind in READ_TARGETS)


def validate_read_set(db, refs, required_ids):
    refs = tuple(refs)
    keys = [(ref.image_id, ref.source, ref.type) for ref in refs]
    if len(set(keys)) != len(keys):
        raise ValidationError("Duplicate annotation read references")
    required = {(image_id, layer, kind) for image_id in required_ids for layer, kind in READ_TARGETS}
    if not required.issubset(keys):
        raise PreviewStale("A complete annotation read set is required")
    for ref in refs:
        if annotations.current_ref(db, ref.image_id, ref.source, ref.type) != ref:
            raise PreviewStale("Annotations changed after preview")


def _plan_digest(plan):
    return digest({"rule": plan.rule, "scope": jobs.scope_json(plan.scope),
                   "changes": plan.changes, "read_set": plan.read_set})


def make_rule_plan(db, *, rule, scope, changes):
    """Capture a preview after F09 has supplied the desired full tag snapshots."""
    object_json(rule, versioned=True)
    project_data(rule)
    ids = jobs.resolve_scope(db, scope)
    plan = RulePlan(rule, Scope(selected_ids=ids), tuple(changes), capture_read_set(db, ids), "")
    return replace(plan, preview_digest=_plan_digest(plan))


def _canonical_tags(values):
    tags(values)
    result, seen = [], set()
    for value in values:
        key = normalize_tag(value.tag)
        if key not in seen:
            result.append(Tag(key, value.conf))
            seen.add(key)
    return tuple(result)


def _build_params(db, image_id, layer, desired, next_id):
    config = dict(ExportSchemeInput("editing").caption_config_json)
    config["wd_null_confidence"] = "include"
    order, evidence = evidence_view(db, image_id, config)
    desired_keys = {normalize_tag(value.tag) for value in desired}
    old = annotations.get_current(db, image_id, layer, "tags")
    old_meta = old.params_json.get("merge_meta") if old else None
    removals = {value["tag"]: dict(value) for value in old_meta["removals"]} if old_meta else {}
    for key in set(order) - desired_keys:
        removals[key] = {"tag": key, "cutoff_annotation_id": next_id, "removed_at": utc_now()}
    origins = {}
    for value in desired:
        key = normalize_tag(value.tag)
        origins[key] = evidence.get(key) or [{"layer": layer, "annotation_id": next_id, "conf": value.conf}]
    meta = {"schema_version": 1, "tag_origins": origins, "removals": [removals[key] for key in sorted(removals)]}
    _validate_meta(db, image_id, layer, desired, meta, next_id)
    return {"schema_version": 1, "merge_meta": meta}


def _validate_meta(db, image_id, layer, content, meta, next_id):
    keys = {normalize_tag(value.tag) for value in content}
    if set(meta["tag_origins"]) != keys:
        raise InvalidChangeReference("Every snapshot tag requires provenance")
    for key, origins in meta["tag_origins"].items():
        if not origins:
            raise InvalidChangeReference("Missing tag origins")
        for origin in origins:
            ref = origin["annotation_id"]
            if ref == next_id:
                if origin["layer"] != layer:
                    raise InvalidChangeReference("Invalid self origin")
                continue
            if ref > next_id:
                raise InvalidChangeReference("Future origin")
            row = annotations.get_annotation(db, ref)
            if row.image_id != image_id or row.source != origin["layer"] or row.type != "tags":
                raise InvalidChangeReference("Origin refers to a different image or layer")
            if not any(normalize_tag(tag.tag) == key and tag.conf == origin["conf"] for tag in row.content):
                raise InvalidChangeReference("Origin does not contain this tag evidence")
    for removal in meta["removals"]:
        ref = removal["cutoff_annotation_id"]
        if ref == next_id:
            continue
        row = annotations.get_annotation(db, ref)
        if ref > next_id or row.image_id != image_id or row.source not in ("manual", "rule"):
            raise InvalidChangeReference("Invalid removal boundary")


def _snapshot(db, image_id, layer, desired):
    images.get_image(db, image_id)
    desired = _canonical_tags(desired)
    old = annotations.get_current(db, image_id, layer, "tags")
    next_id = db.execute("SELECT COALESCE(MAX(annotation_id),0)+1 FROM annotations").fetchone()[0]
    params = _build_params(db, image_id, layer, desired, next_id)
    with db.permit(layer):
        return annotations.append_version(
            db, AnnotationInput(image_id, layer, "tags", desired, params=params),
            expected=CurrentRef(image_id, layer, "tags", old.annotation_id if old else None), policy="content",
        )


def edit_manual_tags(db, image_id, *, tags, read_set):
    validate_read_set(db, read_set, (image_id,))
    return _snapshot(db, image_id, "manual", tags)


def edit_manual_nl(db, image_id, *, content, expected):
    with db.permit("manual"):
        return annotations.append_version(
            db, AnnotationInput(image_id, "manual", "nl", content, params={"schema_version": 1}),
            expected=expected, policy="content",
        )


def apply_rule(db, plan):
    object_json(plan.rule, versioned=True)
    project_data(plan.rule)
    if len(plan.changes) > MAX_RULE_IMAGES or len(encode(plan).encode("utf-8")) > MAX_RULE_BYTES:
        raise BatchTooLarge("Rule must be split explicitly before preview")
    if _plan_digest(plan) != plan.preview_digest:
        raise PreviewStale("Rule preview digest does not match")
    ids = jobs.resolve_scope(db, plan.scope)
    if len(ids) > MAX_RULE_IMAGES:
        raise BatchTooLarge("Rule scope exceeds the transaction budget")
    changed_ids = [change.image_id for change in plan.changes]
    if len(set(changed_ids)) != len(changed_ids) or not set(changed_ids).issubset(ids):
        raise ValidationError("Rule changes must be unique members of the preview scope")
    validate_read_set(db, plan.read_set, ids)
    changes = []
    for change in plan.changes:
        old = annotations.get_current(db, change.image_id, "rule", "tags")
        if (old.annotation_id if old else None) != change.previous_rule_id:
            raise PreviewStale("Rule layer changed")
        if change.merge_meta:
            raise ValidationError("Merge metadata is built by the service, not accepted from the caller")
        result = _snapshot(db, change.image_id, "rule", change.tags)
        if result.changed:
            changes.append({"image_id": change.image_id, "new_annotation_id": result.value.annotation_id,
                            "previous_annotation_id": change.previous_rule_id})
        db.project._fault("rule_after_annotation")
    if not changes:
        return RuleApplyResult(None, 0)
    if len(encode(changes).encode("utf-8")) > MAX_RULE_BYTES:
        raise BatchTooLarge("Rule log exceeds the transaction budget")
    rule = {"schema_version": 1, **plan.rule}
    cursor = db.execute(
        "INSERT INTO operation_log(rule_json,scope_json,affected_count,changes_json,created_at) VALUES(?,?,?,?,?)",
        (encode(rule), encode(jobs.scope_json(plan.scope)), len(changes), encode(changes), utc_now()),
    )
    db.project._fault("rule_after_log")
    return RuleApplyResult(cursor.lastrowid, len(changes))


def undo_rule(db, op_id):
    operation = operations.get_operation(db, op_id)
    if operation.status == "undone":
        return WriteResult(operation, False)
    latest = operations.latest_applied(db)
    if not latest or latest.op_id != op_id:
        raise UndoConflict("Only the latest applied operation may be undone", details={"latest_op_id": latest.op_id if latest else None})
    undone_ids = set()
    from .validation import decode
    for row in db.execute("SELECT changes_json FROM operation_log WHERE status='undone'"):
        undone_ids.update(change["new_annotation_id"] for change in decode(row[0]))
    for change in operation.changes_json:
        new = annotations.get_annotation(db, change["new_annotation_id"])
        if (new.image_id, new.source, new.type, new.is_current) != (change["image_id"], "rule", "tags", True):
            raise UndoConflict("The rule snapshot is no longer current", details={"image_id":change["image_id"],"source":"rule","annotation_id":new.annotation_id})
        previous_id = change["previous_annotation_id"]
        if previous_id is not None:
            previous = annotations.get_annotation(db, previous_id)
            if (previous.image_id, previous.source, previous.type, previous.is_current) != (
                change["image_id"], "rule", "tags", False
            ):
                raise UndoConflict("Previous rule snapshot is invalid", details={"image_id":change["image_id"],"source":"rule","annotation_id":previous_id})
        later = db.execute("SELECT annotation_id,source FROM annotations WHERE image_id=? AND annotation_id>?",
                           (change["image_id"], new.annotation_id)).fetchall()
        conflict = next((row for row in later if row["source"] != "rule" or row["annotation_id"] not in undone_ids),None)
        if conflict:
            raise UndoConflict("Later annotation changes depend on this operation", details={"image_id":change["image_id"],"source":conflict["source"],"annotation_id":conflict["annotation_id"]})
    for change in operation.changes_json:
        db.execute("UPDATE annotations SET is_current=0 WHERE annotation_id=?", (change["new_annotation_id"],))
        if change["previous_annotation_id"] is not None:
            db.execute("UPDATE annotations SET is_current=1 WHERE annotation_id=?", (change["previous_annotation_id"],))
        db.project._fault("undo_after_annotation")
    db.execute("UPDATE operation_log SET status='undone',undone_at=? WHERE op_id=?", (utc_now(), op_id))
    return WriteResult(operations.get_operation(db, op_id), True)
