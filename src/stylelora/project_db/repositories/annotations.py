from ..errors import ConstraintViolation, CurrentConflict, StateConflict, ValidationError
from ..types import AnnotationRow, CurrentRef, WriteResult
from ..validation import LAYERS, choice, confidence, encode, integer, object_json, project_data, tags, text, utc_now
from .common import get, paged, row_dto
from .images import get_image


def get_annotation(db, annotation_id):
    return get(db, "annotations", annotation_id, AnnotationRow)


def get_current(db, image_id, source, type):
    integer(image_id, "image ID", 1)
    choice(source, LAYERS, "annotation source")
    choice(type, ("tags", "nl"), "annotation type")
    row = db.execute(
        "SELECT * FROM annotations WHERE image_id=? AND source=? AND type=? AND is_current=1",
        (image_id, source, type),
    ).fetchone()
    return row_dto(row, AnnotationRow) if row else None


def current_ref(db, image_id, source, type):
    value = get_current(db, image_id, source, type)
    return CurrentRef(image_id, source, type, value.annotation_id if value else None)


def list_history(db, image_id, source, type, page):
    get_image(db, image_id)
    choice(source, LAYERS, "annotation source")
    choice(type, ("tags", "nl"), "annotation type")
    return paged(db, "annotations", AnnotationRow, page,
                 where="image_id=? AND source=? AND type=?", params=(image_id, source, type))


def is_covered(db, image_id, keys):
    if not keys:
        return False
    return all((row := get_current(db, image_id, *target)) is not None and row.cache_key == key
               for target, key in keys.items())


def validate_input(db, value):
    get_image(db, value.image_id)
    choice(value.source, LAYERS, "annotation source")
    choice(value.type, ("tags", "nl"), "annotation type")
    confidence(value.confidence)
    object_json(value.params, versioned=True)
    project_data(value.params)
    if value.type == "tags":
        tags(value.content)
    else:
        text(value.content, "natural language")
    if value.source in ("wd", "rule") and value.type != "tags":
        raise ValidationError("This layer only supports tags")
    if value.source in ("original", "manual", "rule"):
        db.require(value.source)
        if value.model_name is not None or value.job_id is not None:
            raise ValidationError("Non-model layers cannot reference a model/job")
    else:
        db.require("job_outcome")
        text(value.model_name, "actual model", nonempty=True)
        text(value.cache_key, "cache key", nonempty=True)
        integer(value.job_id, "job ID", 1)
        if not isinstance(value.params.get("requested"), dict) or not isinstance(value.params.get("actual"), dict):
            raise ValidationError("Model annotations require requested and actual parameters")
        job = db.execute("SELECT executor,status FROM jobs WHERE job_id=?", (value.job_id,)).fetchone()
        expected_executor = "wd" if value.source == "wd" else "llm_vision"
        if not job or job["executor"] != expected_executor or job["status"] not in ("running", "paused"):
            raise StateConflict("Annotation is not from an active matching job")


def append_version(db, value, *, expected, policy):
    choice(policy, ("cache", "content", "force_new_job", "paired_job"), "append policy")
    validate_input(db, value)
    if policy == "paired_job":
        db.require("job_outcome")
        if value.source != "llm":
            raise ValidationError("Paired response policy requires the LLM layer")
    if (expected.image_id, expected.source, expected.type) != (value.image_id, value.source, value.type):
        raise CurrentConflict("Annotation precondition refers to a different layer")
    old = get_current(db, value.image_id, value.source, value.type)
    if old:
        same_cache = value.cache_key is not None and value.cache_key == old.cache_key
        same_content = (old.content == value.content and old.params_json == value.params and
                        old.confidence == value.confidence and old.model_name == value.model_name)
        if value.source == "original":
            if same_content:
                return WriteResult(old, False)
            raise ConstraintViolation("Original annotations are immutable")
        if policy == "cache" and same_cache:
            return WriteResult(old, False)
        if policy == "content" and same_content:
            return WriteResult(old, False)
        if policy in ("force_new_job", "paired_job") and old.job_id == value.job_id:
            if same_cache and same_content:
                return WriteResult(old, False)
            raise CurrentConflict("A job cannot replace its already committed layer")
    if (old.annotation_id if old else None) != expected.annotation_id:
        raise CurrentConflict("Current annotation changed")
    if policy == "force_new_job":
        job = db.execute("SELECT mode FROM jobs WHERE job_id=?", (value.job_id,)).fetchone()
        if not job or job["mode"] != "all":
            raise StateConflict("Only all-mode jobs may force a new cached version")
    if old:
        db.execute("UPDATE annotations SET is_current=0 WHERE annotation_id=? AND is_current=1", (old.annotation_id,))
    content = encode(value.content) if value.type == "tags" else value.content
    cursor = db.execute(
        "INSERT INTO annotations(image_id,source,type,content,confidence,model_name,params_json,job_id,"
        "cache_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (value.image_id, value.source, value.type, content, value.confidence, value.model_name,
         encode(value.params), value.job_id, value.cache_key, utc_now()),
    )
    return WriteResult(get_annotation(db, cursor.lastrowid), True)
