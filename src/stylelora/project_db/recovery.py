"""Conservative recovery: pause, inspect evidence, reconcile; never submit work."""

from dataclasses import replace

from .artifacts import (
    artifact_from_receipt, cleanup_receipt, load_receipt, originals_from_receipt, receipt_path, verified_file,
)
from .connection import read_snapshot, submit_write
from .errors import ProjectError, StateConflict
from .types import RecoveryEvidence, RecoveryReport, SourceItemRow, WriteResult
from .validation import decode, relative_path, utc_now
from .repositories import images, sources
from .repositories.common import row_dto


def pause_interrupted(db):
    source_ids = tuple(row[0] for row in db.execute("SELECT source_id FROM sources WHERE status='running' ORDER BY source_id"))
    job_ids = tuple(row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status='running' ORDER BY job_id"))
    db.execute("UPDATE sources SET status='paused' WHERE status='running'")
    db.execute("UPDATE jobs SET status='paused',updated_at=? WHERE status='running'", (utc_now(),))
    if db.project:
        db.after_commit.extend((db.project._cycles.clear, db.project._job_generations.clear))
    return RecoveryReport(paused_source_ids=source_ids, paused_job_ids=job_ids)


def inspect_sent(project, item):
    """Absence of a receipt is UNKNOWN, not proof of an unexecuted request."""
    try:
        path = receipt_path(project.root, item.item_id, item.attempts)
        if not path.is_file():
            return RecoveryEvidence(item.item_id, item.attempts, "unknown", reason="recovery:no_complete_receipt")
        receipt = decode(path.read_text(encoding="utf-8"))
        artifact = artifact_from_receipt(receipt)
        if (artifact.item_id, artifact.attempt) != (item.item_id, item.attempts):
            raise StateConflict("Receipt identity mismatch")
        receipt = load_receipt(project.root, artifact)
        verified_file(project.root, artifact, receipt)
        return RecoveryEvidence(item.item_id, item.attempts, "complete", artifact, "recovery:complete_receipt")
    except (ProjectError, OSError, KeyError, TypeError, ValueError):
        return RecoveryEvidence(item.item_id, item.attempts, "unknown", reason="recovery:unverified_receipt")


def reconcile_item(db, evidence):
    from .commands import complete_item, fail_item
    item = sources.get_item(db, evidence.item_id)
    if item.attempts != evidence.attempt:
        raise StateConflict("Recovery evidence is for a different attempt")
    if evidence.verdict == "unknown":
        return WriteResult(item, False)
    if evidence.verdict == "absent":
        return fail_item(db, item.item_id, attempt=item.attempts, error="recovery:no_complete_artifact")
    if evidence.verdict != "complete" or evidence.artifact is None:
        raise StateConflict("Invalid recovery evidence")
    receipt = load_receipt(db.project.root, evidence.artifact)
    result = complete_item(db, evidence.artifact, originals_from_receipt(receipt))
    return WriteResult(sources.get_item(db, item.item_id), result.changed)


def recover_project(project):
    report = submit_write(project, pause_interrupted).result()
    with read_snapshot(project) as db:
        pending = tuple(row_dto(row, SourceItemRow) for row in db.execute(
            "SELECT * FROM source_items WHERE status='sent' ORDER BY item_id"
        ))
    recovered, unresolved = [], []
    for item in pending:
        evidence = inspect_sent(project, item)
        if evidence.verdict == "complete":
            try:
                submit_write(project, lambda db, e=evidence: reconcile_item(db, e)).result()
                recovered.append(item.item_id)
            except (ProjectError, OSError):
                unresolved.append(item.item_id)
        else:
            unresolved.append(item.item_id)
    with read_snapshot(project) as db:
        image_rows = tuple(db.execute("SELECT image_id,filename,file_missing FROM images"))
        done = tuple(row_dto(row, SourceItemRow) for row in db.execute(
            "SELECT * FROM source_items WHERE status='done' ORDER BY item_id"
        ))
    missing = []
    for row in image_rows:
        if not relative_path(project.root, "images/" + row["filename"]).is_file():
            if not row["file_missing"]:
                submit_write(project, lambda db, i=row["image_id"]: images.set_file_missing(db, i, missing=True)).result()
            missing.append(row["image_id"])
    for item in done:
        # A leftover receipt is removed only after it agrees with committed identity.
        path = receipt_path(project.root, item.item_id, item.attempts)
        if path.exists():
            try:
                artifact = artifact_from_receipt(decode(path.read_text(encoding="utf-8")))
                load_receipt(project.root, artifact)
                with read_snapshot(project) as db:
                    image = images.get_image(db, item.image_id)
                if (artifact.item_id, artifact.attempt, artifact.sha256, artifact.file_size) == (
                    item.item_id, item.attempts, image.sha256, image.file_size
                ):
                    cleanup_receipt(project.root, item.item_id, item.attempts)
            except (ProjectError, OSError, KeyError, TypeError, ValueError):
                pass
    registered = {row["filename"] for row in image_rows}
    orphans = tuple(sorted(path.name for path in (project.root / "images").iterdir()
                           if path.is_file() and path.name not in registered))
    return replace(report, recovered_item_ids=tuple(recovered), unresolved_item_ids=tuple(unresolved),
                   missing_image_ids=tuple(missing), orphan_filenames=orphans)
