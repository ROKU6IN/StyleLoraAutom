"""Atomic cross-table commands. Call through submit_write; never commit here."""

from dataclasses import replace
from uuid import uuid4

from .errors import (
    ArtifactConflict, FileMissing, OutcomeConflict, SeedConflict, StateConflict, ValidationError,
)
from .types import AnnotationInput, CurrentRef, SourcePatch, WriteResult
from .validation import (
    ASPECTS, JOB_STATES, SOURCE_STATES, choice, digest, encode, integer, object_json,
    relative_path, text, utc_now,
)
from .repositories import annotations, images, jobs, sources

SOURCE_EDGES = {
    ("running", "paused"), ("paused", "running"), ("running", "done"), ("paused", "done"),
    ("running", "canceled"), ("paused", "canceled"), ("draft", "canceled"),
}
JOB_EDGES = {
    ("pending", "running"),
    ("running", "paused"), ("paused", "running"), ("running", "done"), ("running", "failed"),
    ("running", "canceled"), ("pending", "canceled"), ("paused", "canceled"),
    ("pending", "failed"), ("paused", "done"), ("paused", "failed"),
}


def _cycle_on_commit(db, source_id, running=False, acknowledged=False, pending_cost=False):
    if db.project:
        value = {"id": uuid4().hex, "ack": acknowledged, "pending_cost": pending_cost, "confirmed_from": None}
        db.after_commit.append(lambda: db.project._cycles.__setitem__(source_id, value))


def _validate_start(db, source, items):
    config = source.config_json
    object_json(config, versioned=True)
    if source.type == "nai":
        if source.target_count != len(items):
            raise ValidationError("NAI target_count must match the full seed list")
        # F02 owns model-specific capability checks; DAL enforces its stable snapshot boundary.
        profile = config.get("profile", config)
        text(profile.get("model"), "model", nonempty=True)
        text(profile.get("sampler"), "sampler", nonempty=True)
        integer(profile.get("steps"), "steps", 1)
        scale = profile.get("scale")
        if type(scale) not in (int, float) or not 0 < scale < float("inf"):
            raise ValidationError("Scale must be positive and finite")
        if profile.get("quality_toggle") is not False or "uc_preset" not in profile or profile["uc_preset"] is not None:
            raise ValidationError("quality_toggle=false and uc_preset=null are required")
        choice(config.get("default_aspect"), ASPECTS, "default aspect")
        dimensions = config.get("sizes")
        if not isinstance(dimensions, dict) or set(dimensions) != set(ASPECTS):
            raise ValidationError("All three concrete image sizes are required")
        for aspect in ASPECTS:
            size = dimensions[aspect]
            if not isinstance(size, dict) or set(size) != {"width", "height"}:
                raise ValidationError("Invalid image size")
            for value in size.values():
                integer(value, "dimension", 64)
                if value % 64:
                    raise ValidationError("Image dimensions must be multiples of 64")
    else:
        if config.get("reader") != "txt_sidecar":
            raise ValidationError("Import reader must be txt_sidecar")
        object_json(config.get("params"), versioned=True)
        manifest = config.get("import_manifest")
        if not isinstance(manifest, list) or len(manifest) != len(items):
            raise ValidationError("A complete relative import manifest is required")
        for seed, entry in zip(items, manifest):
            if not isinstance(entry, dict) or entry.get("seq") != seed.seq or not isinstance(entry.get("path"), str):
                raise ValidationError("Invalid import manifest entry")
            path = relative_path(db.project.root, entry["path"])
            if path.name != seed.prompt:
                raise ValidationError("Import prompt must be the original filename")


def start_source(db, source_id, items):
    source = sources.get_source(db, source_id)
    items = tuple(items)
    _validate_start(db, source, items)
    with db.permit("start_source"):
        sources.seed_items(db, source_id, items)
    if source.locked:
        return source
    if source.status != "draft":
        raise StateConflict("Only draft sources can be started")
    now = utc_now()
    db.execute("UPDATE sources SET status='running',locked=1,started_at=?,finished_at=NULL WHERE source_id=?",
               (now, source_id))
    _cycle_on_commit(db, source_id, True)
    return sources.get_source(db, source_id)


def transition_source(db, source_id, *, expected, target, reason):
    choice(expected, SOURCE_STATES, "expected source status")
    choice(target, SOURCE_STATES, "target source status")
    text(reason, "reason")
    source = sources.get_source(db, source_id)
    if source.status == target:
        return WriteResult(source, False)
    if source.status != expected or (expected, target) not in SOURCE_EDGES:
        raise StateConflict("Illegal source transition")
    if target == "running" and sources.count_item_states(db, source_id)["sent"]:
        raise StateConflict("Reconcile sent items before resuming")
    if target == "done":
        counts = sources.count_item_states(db, source_id)
        if counts["pending"] or counts["sent"]:
            raise StateConflict("Source still has unresolved items")
    finished = utc_now() if target in ("done", "canceled") else None
    db.execute("UPDATE sources SET status=?,finished_at=? WHERE source_id=? AND status=?",
               (target, finished, source_id, expected))
    _cycle_on_commit(db, source_id, target == "running")
    return WriteResult(sources.get_source(db, source_id), True)


def mark_sent(db, item_id):
    item = sources.get_item(db, item_id)
    if item.status == "sent":
        return WriteResult(item, False)
    source = sources.get_source_state(db, item.source_id)
    if item.status != "pending" or source.status != "running":
        raise StateConflict("Only a pending item in a running source can be sent")
    db.execute("UPDATE source_items SET status='sent',attempts=?,error=NULL,updated_at=? WHERE item_id=?",
               (max(1, item.attempts), utc_now(item.updated_at), item_id))
    return WriteResult(sources.get_item(db, item_id), True)


def skip_duplicate(db, item_id, *, sha256):
    item = sources.get_item(db, item_id)
    source = sources.get_source_state(db, item.source_id)
    if source.type != "import" or not images.find_by_sha256(db, sha256):
        raise StateConflict("Only a proven import duplicate can be skipped")
    if item.status == "skipped":
        return WriteResult(item, False)
    if item.status not in ("pending", "sent"):
        raise StateConflict("This item cannot be skipped")
    if item.status == "pending" and source.status != "running":
        raise StateConflict("Pending imports require a running source")
    db.execute(
        "UPDATE source_items SET status='skipped',image_id=NULL,error='duplicate_sha256',updated_at=? WHERE item_id=?",
        (utc_now(item.updated_at), item_id),
    )
    return WriteResult(sources.get_item(db, item_id), True)


def set_cost_policy(db, source_id, *, policy):
    return sources.update_source(db, source_id, SourcePatch(cost_policy=policy))


def fail_item(db, item_id, *, attempt, error):
    integer(attempt, "attempt", 1)
    text(error, "error", nonempty=True)
    # This is a safe domain reason/code, not arbitrary upstream error text.
    if len(error) > 512 or "\n" in error or "://" in error or "Bearer " in error:
        raise ValidationError("Use a short sanitized error reason")
    item = sources.get_item(db, item_id)
    if item.attempts != attempt:
        raise StateConflict("Attempt changed")
    if item.status == "failed" and item.error == error:
        return WriteResult(item, False)
    if item.status != "sent":
        raise StateConflict("Only a sent item can fail")
    db.execute("UPDATE source_items SET status='failed',error=?,updated_at=? WHERE item_id=?",
               (error, utc_now(item.updated_at), item_id))
    return WriteResult(sources.get_item(db, item_id), True)


def retry_failed_item(db, item_id, *, expected_attempt):
    integer(expected_attempt, "attempt", 1)
    item = sources.get_item(db, item_id)
    source = sources.get_source_state(db, item.source_id)
    if item.status == "pending" and item.attempts == expected_attempt + 1 and source.status != "canceled":
        return WriteResult(item, False)
    if item.status != "failed" or item.attempts != expected_attempt or source.status not in ("running", "paused", "done"):
        raise StateConflict("Item cannot be retried with these preconditions")
    if source.status == "done":
        db.execute("UPDATE sources SET status='paused',finished_at=NULL WHERE source_id=?", (source.source_id,))
        _cycle_on_commit(db, source.source_id)
    db.execute("UPDATE source_items SET status='pending',attempts=attempts+1,error=NULL,updated_at=? WHERE item_id=?",
               (utc_now(item.updated_at), item_id))
    return WriteResult(sources.get_item(db, item_id), True)


def complete_item(db, artifact, originals=()):
    from .artifacts import check_archive, cleanup_receipt, finalize_file, load_receipt, originals_from_receipt, validate_artifact
    validate_artifact(db.project.root, artifact)
    check_archive(db.project.root, artifact)
    item = sources.get_item(db, artifact.item_id)
    if item.attempts != artifact.attempt:
        raise ArtifactConflict("Artifact belongs to a different attempt")
    if item.status == "done":
        image = images.get_image(db, item.image_id)
        if (image.sha256, image.file_size, image.source_id, image.width, image.height, image.phash,
            image.png_meta_json, image.filename.rsplit(".", 1)[-1]) != (
            artifact.sha256, artifact.file_size, item.source_id, artifact.width, artifact.height, artifact.phash,
            artifact.png_meta, artifact.extension[1:]
        ):
            raise ArtifactConflict("Completed item has different image evidence")
        supplied = {value.type: value for value in originals}
        if len(supplied) != len(originals):
            raise ArtifactConflict("Duplicate original layers in replay")
        for kind in ("tags", "nl"):
            stored = annotations.get_current(db, image.image_id, "original", kind)
            value = supplied.get(kind)
            if (stored is None) != (value is None) or (stored and (
                stored.content, stored.confidence, stored.params_json, stored.cache_key
            ) != (value.content, value.confidence, value.params, value.cache_key)):
                raise ArtifactConflict("Completed item has different original annotations")
        db.after_commit.append(lambda: cleanup_receipt(db.project.root, item.item_id, item.attempts))
        return WriteResult(image, False)
    if item.status != "sent":
        raise StateConflict("Only sent items can be registered")
    receipt = load_receipt(db.project.root, artifact)
    if encode(originals) != encode(originals_from_receipt(receipt)):
        raise ArtifactConflict("Original annotations do not match the receipt")
    image_id, filename = finalize_file(db, artifact, receipt)
    db.execute(
        "INSERT INTO images(image_id,filename,width,height,phash,sha256,file_size,source_id,png_meta_json,created_at,review_status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (image_id, filename, artifact.width, artifact.height, artifact.phash, artifact.sha256, artifact.file_size,
         item.source_id, encode(artifact.png_meta) if artifact.png_meta is not None else None, utc_now(),
         artifact.registration_check['review_status'] if artifact.registration_check else 'unreviewed'),
    )
    with db.permit("original"):
        for value in originals:
            annotations.append_version(
                db, AnnotationInput(image_id, "original", value.type, value.content, value.confidence,
                                    params=value.params, cache_key=value.cache_key),
                expected=CurrentRef(image_id, "original", value.type, None), policy="content",
            )
    db.project._fault("artifact_before_done")
    db.execute("UPDATE source_items SET status='done',image_id=?,error=NULL,updated_at=? WHERE item_id=?",
               (image_id, utc_now(item.updated_at), item.item_id))
    db.after_commit.append(lambda: db.project._fault("artifact_after_commit"))
    db.after_commit.append(lambda: cleanup_receipt(db.project.root, item.item_id, item.attempts))
    return WriteResult(images.get_image(db, image_id), True)


def transition_job(db, job_id, *, expected, target, error=None):
    choice(expected, JOB_STATES, "expected job status")
    choice(target, JOB_STATES, "target job status")
    job = jobs.get_job(db, job_id)
    if job.status == target and (target != "failed" or job.error == error):
        return WriteResult(job, False)
    if job.status != expected or (expected, target) not in JOB_EDGES:
        raise StateConflict("Illegal job transition")
    if target == 'running' and not job.config_snapshot_json:
        raise StateConflict('A job must freeze its scope and configuration before starting')
    if target == "done" and job.done_count + job.failed_count + job.skipped_count != job.total:
        raise StateConflict("Job has unresolved images")
    if target == "failed":
        text(error, "batch failure reason", nonempty=True)
    if target == "running" and db.execute(
        "SELECT 1 FROM jobs WHERE batch_id=? AND job_id<? AND status<>'done'", (job.batch_id, job_id),
    ).fetchone():
        raise StateConflict("Earlier batch job is unfinished")
    now = utc_now(job.updated_at)
    finished = now if target in ("done", "failed", "canceled") else None
    db.execute("UPDATE jobs SET status=?,error=?,finished_at=?,updated_at=?,started_at=COALESCE(started_at,?) WHERE job_id=? AND status=?",
               (target, error, finished, now, now if target == 'running' else None, job_id, expected))
    if db.project:
        if target == "running":
            db.after_commit.append(lambda: db.project._job_generations.add(job_id))
        elif target in ("done", "failed", "canceled"):
            db.after_commit.append(lambda: db.project._job_generations.discard(job_id))
    return WriteResult(jobs.get_job(db, job_id), True)


def commit_job_outcome(db, job_id, outcome):
    job = jobs.get_job(db, job_id)
    choice(outcome.kind, ("done", "failed", "skipped"), "outcome")
    integer(outcome.image_id, "image ID", 1)
    progress = job.progress_json
    if outcome.image_id not in progress["image_ids"]:
        raise StateConflict("Image is outside the frozen job scope")
    checksum = digest(outcome)
    previous = progress["outcomes"].get(str(outcome.image_id))
    if previous:
        if previous["digest"] != checksum:
            raise OutcomeConflict("This job already recorded a different outcome for this image")
        return WriteResult(job, False)
    if job.status == "canceled":
        return WriteResult(job, False)
    if job.status not in ("running", "paused"):
        raise StateConflict("Job cannot accept new outcomes")
    if db.project and job_id not in db.project._job_generations:
        raise StateConflict("Job must be resumed after process recovery before accepting new results")
    image = images.get_image(db, outcome.image_id)
    if image.file_missing and (outcome.kind != "skipped" or outcome.reason != "file_missing"):
        raise FileMissing("Missing images must be recorded as skipped/file_missing")
    ids, keys = [], []
    if outcome.kind == "done":
        wanted = set(job.config_snapshot_json["target_types"])
        if len(outcome.annotations) != len(wanted) or {a.type for a in outcome.annotations} != wanted:
            raise ValidationError("The entire target-layer result must be committed together")
        if outcome.reason is not None or outcome.annotation_ids:
            raise ValidationError("Successful model outcomes contain new annotation payloads only")
        layer = "wd" if job.executor == "wd" else "llm"
        vision = job.executor == 'llm_vision' and job.config_snapshot_json.get('params', {}).get('pipeline_version') == 'vision-runner-v1'
        if vision:
            first = outcome.annotations[0]
            if any(a.model_name != first.model_name or a.params != first.params for a in outcome.annotations):
                raise ValidationError('Vision targets must belong to the same response')
            actual = first.params.get('actual', {})
            role = actual.get('role')
            binding = job.config_snapshot_json['params'].get('roles', {}).get(role)
            if (role not in ('vision', 'vision_nsfw') or not binding
                    or first.model_name not in (binding['model'], *binding.get('response_aliases', []))
                    or actual.get('protocol') != binding['protocol'] or actual.get('model') != first.model_name):
                raise ValidationError('Vision response does not match the frozen role')
        with db.permit("job_outcome"):
            for value in outcome.annotations:
                if (value.image_id, value.job_id, value.source, value.model_name) != (
                    outcome.image_id, job_id, layer, value.model_name if vision else job.config_snapshot_json["model"]
                ):
                    raise ValidationError("Result does not match the frozen job")
                result = annotations.append_version(
                    db, value, expected=annotations.current_ref(db, value.image_id, value.source, value.type),
                    # A freshly evaluated visual response is one indivisible pair.
                    # Per-layer cache reuse could retain an old tag row while
                    # appending its missing caption, mixing two different outcomes.
                    policy="paired_job" if vision else "force_new_job" if job.mode == "all" else "cache",
                )
                ids.append(result.value.annotation_id)
                keys.append(result.value.cache_key)
                db.project._fault("job_after_annotation")
    else:
        if outcome.annotations:
            raise ValidationError("Failed/skipped outcomes cannot create annotations")
        text(outcome.reason, "outcome reason", nonempty=True)
        if outcome.reason == "cache_hit":
            if outcome.kind != "skipped" or job.mode == "all":
                raise StateConflict("New all-mode jobs cannot skip historical cache entries")
            for annotation_id in outcome.annotation_ids:
                row = annotations.get_annotation(db, annotation_id)
                if row.image_id != outcome.image_id or not row.is_current:
                    raise StateConflict("Cache evidence is stale")
                ids.append(annotation_id)
                keys.append(row.cache_key)
            target_layer = "wd" if job.executor == "wd" else "llm"
            target_rows = [annotations.get_annotation(db, annotation_id) for annotation_id in ids]
            if len(target_rows) != len(job.config_snapshot_json["target_types"]) or {
                (row.source, row.type) for row in target_rows
            } != {(target_layer, kind) for kind in job.config_snapshot_json["target_types"]}:
                raise ValidationError("Cache skip requires all target layers")
            if job.executor == 'llm_vision' and len(target_rows) > 1:
                first = target_rows[0]
                if any((row.job_id, row.model_name, row.params_json) != (first.job_id, first.model_name, first.params_json) for row in target_rows):
                    raise ValidationError('Cache skip requires one coherent visual response')
        elif outcome.annotation_ids:
            raise ValidationError("Only cache skips can reference existing annotations")
    progress["outcomes"][str(outcome.image_id)] = {
        "kind": outcome.kind, "reason": outcome.reason, "annotation_ids": ids, "cache_keys": keys, "digest": checksum,
    }
    counts = [sum(value["kind"] == kind for value in progress["outcomes"].values()) for kind in ("done", "failed", "skipped")]
    db.execute("UPDATE jobs SET progress_json=?,done_count=?,failed_count=?,skipped_count=?,updated_at=? WHERE job_id=?",
               (encode(progress), *counts, utc_now(job.updated_at), job_id))
    return WriteResult(jobs.get_job(db, job_id), True)


def bulk_review(db, image_ids, *, expected, target):
    ids = tuple(dict.fromkeys(image_ids))
    if len(ids) > 5000:
        from .errors import BatchTooLarge
        raise BatchTooLarge("Review batch exceeds 5000 images")
    if set(expected) != set(ids):
        raise ValidationError("Every image requires a review precondition")
    return tuple(images.set_review(db, image_id, expected=expected[image_id], target=target).value for image_id in ids)


def evaluate_cost(project, source_id, *, estimated_anlas, unknown_cost=False):
    from .connection import submit_write
    if not (unknown_cost is True and estimated_anlas is None):
        integer(estimated_anlas, "estimated_anlas")

    def evaluate(db):
        source = sources.get_source(db, source_id)
        if source.status != "running":
            raise StateConflict("Cost can only be evaluated for a running source")
        cycle = project._cycles.get(source_id)
        if estimated_anlas == 0 or source.cost_policy == "allow" or (
            source.cost_policy == "ask" and cycle and cycle["ack"]
        ):
            return "submit"
        transition_source(db, source_id, expected="running", target="paused", reason="cost:" + source.cost_policy)
        _cycle_on_commit(db, source_id, pending_cost=source.cost_policy == "ask")
        return "paused"

    return submit_write(project, evaluate).result()


def cost_cycle_id(project, source_id):
    """Opaque in-memory token for presenting the cost confirmation action."""
    from .connection import submit_write
    return submit_write(project, lambda db: project._cycles.get(source_id, {}).get("id")).result()


def confirm_cost_and_resume(project, source_id, *, expected_cycle_id, validate=None):
    from .connection import submit_write

    def confirm(db):
        if validate is not None:
            validate(db)  # Optional service CAS runs in this same writer transaction.
        source = sources.get_source(db, source_id)
        cycle = project._cycles.get(source_id)
        if source.status == "running" and cycle and cycle["confirmed_from"] == expected_cycle_id:
            return source
        if source.cost_policy != "ask" or not cycle or cycle["id"] != expected_cycle_id or not cycle["pending_cost"]:
            raise StateConflict("Cost confirmation has expired or is not applicable")
        result = transition_source(db, source_id, expected="paused", target="running", reason="cost:confirmed")
        new = {"id": uuid4().hex, "ack": True, "pending_cost": False, "confirmed_from": expected_cycle_id}
        db.after_commit.append(lambda: project._cycles.__setitem__(source_id, new))
        return result.value

    return submit_write(project, confirm).result()


# Rules share the same transaction/session contract but live in a separate module.
def apply_rule(db, plan):
    from .rules import apply_rule as execute
    return execute(db, plan)


def undo_rule(db, op_id):
    from .rules import undo_rule as execute
    return execute(db, op_id)


def edit_manual_tags(db, image_id, *, tags, read_set):
    from .rules import edit_manual_tags as execute
    return execute(db, image_id, tags=tags, read_set=read_set)
