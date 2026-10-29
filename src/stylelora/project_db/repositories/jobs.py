from dataclasses import asdict

from ..errors import SnapshotMismatch, StateConflict, ValidationError
from ..types import JobRow, WriteResult
from ..validation import JOB_STATES, choice, encode, integer, project_data, text, utc_now
from .common import get, paged, row_dto
from .images import get_image, image_where


def get_job(db, job_id):
    return get(db, "jobs", job_id, JobRow)


def list_jobs(db, *, status=None, batch_id=None, page):
    return _list_jobs(db, status=status, batch_id=batch_id, page=page)


def list_jobs_for_executor(db, executor, *, status=None, batch_id=None, page):
    choice(executor, ('wd', 'llm_vision'), 'executor')
    return _list_jobs(db, executor=executor, status=status, batch_id=batch_id, page=page)


def list_batch_heads(db, *, page):
    return paged(db, 'jobs', JobRow, page, where='job_id IN (SELECT MIN(job_id) FROM jobs GROUP BY batch_id)')


def wait_reason(db, job_id, reason):
    """Persist a pending stage's hold/diagnosis without inventing a job state."""
    job = get_job(db, job_id)
    if job.status != 'pending':
        raise StateConflict('Only pending stages have a wait reason')
    if reason is not None:
        text(reason, 'wait reason', nonempty=True)
    if job.error == reason:
        return WriteResult(job, False)
    db.execute('UPDATE jobs SET error=?,updated_at=? WHERE job_id=?', (reason, utc_now(job.updated_at), job_id))
    return WriteResult(get_job(db, job_id), True)


def _list_jobs(db, *, status=None, batch_id=None, executor=None, page):
    where, params = [], []
    if executor is not None:
        where.append('executor=?')
        params.append(executor)
    if status is not None:
        choice(status, JOB_STATES, "job status")
        where.append("status=?")
        params.append(status)
    if batch_id is not None:
        text(batch_id, "batch ID", nonempty=True)
        where.append("batch_id=?")
        params.append(batch_id)
    return paged(db, "jobs", JobRow, page, where=" AND ".join(where) or "1=1", params=params)


def scope_json(scope):
    if (scope.selected_ids is None) == (scope.filter is None):
        raise ValidationError("Exactly one scope selector is required")
    if scope.selected_ids is not None:
        for image_id in scope.selected_ids:
            integer(image_id, "image ID", 1)
        if len(scope.selected_ids) != len(set(scope.selected_ids)):
            raise ValidationError("Scope IDs must be unique")
    else:
        image_where(scope.filter)
    from ..validation import primitive
    value = {"schema_version": 1, **primitive(scope)}
    if value['filter'] is not None:
        # Default additions must not change stored 0.15 and earlier request identities.
        for name in ('rating', 'file_missing', 'q'):
            if value['filter'][name] is None:
                value['filter'].pop(name)
    return value


def resolve_scope(db, scope):
    scope_json(scope)
    if scope.selected_ids is not None:
        for image_id in scope.selected_ids:
            get_image(db, image_id)
        return tuple(sorted(scope.selected_ids))
    where, params = image_where(scope.filter)
    if scope.filter.review_statuses is None:
        where += " AND review_status<>'reject'"
    return tuple(row[0] for row in db.execute(f"SELECT image_id FROM images WHERE {where} ORDER BY image_id", params))


def create_batch(db, batch_id, requests):
    text(batch_id, "batch ID", nonempty=True)
    requests = tuple(requests)
    if not requests or len(requests) > 2 or len({r.executor for r in requests}) != len(requests):
        raise ValidationError("A batch contains at most one job per supported executor")
    requests = tuple(sorted(requests, key=lambda r: 0 if r.executor == "wd" else 1))
    desired = []
    for request in requests:
        choice(request.executor, ("wd", "llm_vision"), "executor")
        choice(request.mode, ("selected", "uncovered", "all"), "mode")
        # Mode controls cache policy; either IDs or a server-resolved filter may select the range.
        desired.append((request.executor, request.mode, scope_json(request.scope)))
    existing = tuple(row_dto(row, JobRow) for row in db.execute(
        "SELECT * FROM jobs WHERE batch_id=? ORDER BY job_id", (batch_id,),
    ))
    if existing:
        if [(row.executor, row.mode, row.scope_json) for row in existing] != desired:
            raise SnapshotMismatch("Batch ID was already used with different requests")
        return existing
    now = utc_now()
    ids = []
    for executor, mode, scope in desired:
        cursor = db.execute(
            "INSERT INTO jobs(executor,batch_id,mode,scope_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (executor, batch_id, mode, encode(scope), now, now),
        )
        ids.append(cursor.lastrowid)
    return tuple(get_job(db, job_id) for job_id in ids)


def freeze_job(db, job_id, snapshot, *, image_ids, start=True):
    job = get_job(db, job_id)
    integer(snapshot.schema_version, "snapshot schema", 1)
    if snapshot.schema_version != 1:
        raise ValidationError("Unsupported snapshot schema")
    text(snapshot.model, "model", nonempty=True)
    text(snapshot.protocol, "protocol", nonempty=True)
    text(snapshot.skill_hash, "skill hash", nonempty=True)
    integer(snapshot.concurrency, "concurrency", 1)
    integer(snapshot.timeout, "timeout", 1)
    integer(snapshot.retries, "retries")
    targets = snapshot.target_types
    if not targets or len(set(targets)) != len(targets) or any(t not in ("tags", "nl") for t in targets):
        raise ValidationError("Invalid job target types")
    if job.executor == "wd" and targets != ("tags",):
        raise ValidationError("WD jobs only produce tags")
    config = {"schema_version": 1, **asdict(snapshot)}
    # Normalize tuple fields into the persisted JSON form.
    from ..validation import decode
    config = decode(encode(config))
    project_data(config)
    image_ids = tuple(image_ids)
    if tuple(sorted(set(image_ids))) != image_ids:
        raise ValidationError("Frozen IDs must be sorted and unique")
    for image_id in image_ids:
        get_image(db, image_id)
    if job.status != "pending" or job.config_snapshot_json:
        if job.config_snapshot_json == config and tuple(job.progress_json["image_ids"]) == image_ids:
            if start and job.status == 'pending':
                from ..commands import transition_job
                return transition_job(db, job_id, expected='pending', target='running')
            return WriteResult(job, False)
        raise SnapshotMismatch("Job snapshot is already frozen")
    from ..types import ImageFilter, Scope
    scope = job.scope_json
    selector = Scope(tuple(scope["selected_ids"]) if scope["selected_ids"] is not None else None,
                     ImageFilter(**scope["filter"]) if scope["filter"] is not None else None)
    allowed = resolve_scope(db, selector)
    if job.mode != "uncovered" and image_ids != allowed:
        raise SnapshotMismatch("Frozen range does not match the requested scope")
    if job.mode == "uncovered" and not set(image_ids).issubset(allowed):
        raise SnapshotMismatch("Frozen range contains images outside the requested scope")
    if start and db.execute("SELECT 1 FROM jobs WHERE batch_id=? AND job_id<? AND status<>'done'",
                  (job.batch_id, job_id)).fetchone():
        raise StateConflict("Earlier jobs in this batch must finish first")
    now = utc_now(job.updated_at)
    db.execute(
        "UPDATE jobs SET config_snapshot_json=?,progress_json=?,total=?,status=?,started_at=?,updated_at=? "
        "WHERE job_id=? AND status='pending'",
        (encode(config), encode({"schema_version": 1, "image_ids": image_ids, "outcomes": {}}),
         len(image_ids), 'running' if start else 'pending', now if start else None, now, job_id),
    )
    if db.project and start:
        db.after_commit.append(lambda: db.project._job_generations.add(job_id))
    return WriteResult(get_job(db, job_id), True)
