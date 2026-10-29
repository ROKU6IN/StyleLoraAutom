from ..errors import FileMissing, StateConflict, ValidationError
from ..types import ImageRow, RatingValue, WriteResult
from ..validation import REVIEWS, choice, file_hash, integer, relative_path, sha256, timestamp, text
from .common import get, in_json, paged, row_dto


def get_image(db, image_id):
    return get(db, "images", image_id, ImageRow)


def image_where(query):
    clauses, params = [], []
    if type(query.include_missing) is not bool:
        raise ValidationError("include_missing must be boolean")
    if not query.include_missing:
        clauses.append("file_missing=0")
    for key, values in (("source_id", query.source_ids), ("review_status", query.review_statuses)):
        if values is not None:
            for value in values:
                integer(value, "source ID", 1) if key == "source_id" else choice(value, REVIEWS, "review status")
            clause, encoded = in_json(key, values)
            clauses.append(clause)
            params.append(encoded)
    if query.rating is not None:
        for rating in query.rating:
            choice(rating, ('general', 'sensitive', 'questionable', 'explicit'), 'rating')
    if query.file_missing is not None and type(query.file_missing) is not bool:
        raise ValidationError('file_missing must be boolean or null')
    if query.q is not None:
        text(query.q, 'tag search')
        if len(query.q) > 4096:
            raise ValidationError('Tag search is too long')
    extra, arguments = workbench_where({'rating': query.rating, 'file_missing': query.file_missing, 'q': query.q})
    if extra != '1=1':
        clauses.append(extra)
        params.extend(arguments)
    return " AND ".join(clauses) or "1=1", tuple(params)


def workbench_where(query):
    """Shared SQL semantics for the workbench and frozen executor filters."""
    clauses, args = [], []
    for key, values in (("source_id", query.get("source_ids")), ("review_status", query.get("review_statuses")), ("rating", query.get("rating"))):
        if values:
            clauses.append(key + " IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    if query.get("file_missing") is not None:
        clauses.append("file_missing=?")
        args.append(int(query["file_missing"]))
    if query.get("q"):
        clauses.append("EXISTS (SELECT 1 FROM annotations a,json_each(a.content) t WHERE a.image_id=images.image_id AND a.is_current=1 AND a.type='tags' AND instr(lower(json_extract(t.value,'$.tag')),?)>0)")
        args.append(query["q"].lower())
    return " AND ".join(clauses) or "1=1", args


def list_images(db, query, page):
    where, params = image_where(query)
    return paged(db, "images", ImageRow, page, where=where, params=params)


def find_by_sha256(db, value):
    sha256(value)
    return tuple(row_dto(row, ImageRow) for row in db.execute(
        "SELECT * FROM images WHERE sha256=? ORDER BY image_id", (value,),
    ))


def set_review(db, image_id, *, expected, target):
    choice(expected, REVIEWS, "expected review")
    choice(target, REVIEWS, "target review")
    image = get_image(db, image_id)
    if image.file_missing and target != "reject":
        raise FileMissing("A missing image cannot be kept or returned to unreviewed")
    if image.review_status == target:
        return WriteResult(image, False)
    if image.review_status != expected:
        raise StateConflict("Image review changed")
    db.execute("UPDATE images SET review_status=? WHERE image_id=? AND review_status=?", (target, image_id, expected))
    return WriteResult(get_image(db, image_id), True)


def set_file_missing(db, image_id, *, missing, verified_sha256=None):
    if type(missing) is not bool:
        raise ValidationError("missing must be boolean")
    image = get_image(db, image_id)
    if not missing:
        if verified_sha256 != image.sha256 or db.project is None:
            raise FileMissing("Restored file must be verified before clearing missing")
        path = relative_path(db.project.root, "images/" + image.filename)
        if not path.is_file() or path.stat().st_size != image.file_size or file_hash(path) != image.sha256:
            raise FileMissing("Restored file does not match the stored image")
    if image.file_missing == missing:
        return WriteResult(image, False)
    db.execute("UPDATE images SET file_missing=? WHERE image_id=?", (int(missing), image_id))
    return WriteResult(get_image(db, image_id), True)


def rating_value(image):
    if image.rating is None:
        return None
    return RatingValue(image.rating, image.rating_source, image.rating_verified, image.rated_at)


def validate_rating(value):
    if value is None:
        return
    choice(value.rating, ("general", "sensitive", "questionable", "explicit"), "rating")
    choice(value.source, ("keyword", "model", "fallback", "refusal", "manual"), "rating source")
    if type(value.verified) is not bool:
        raise ValidationError("rating verified must be boolean")
    timestamp(value.rated_at)
    if value.source == "fallback" and value.verified:
        raise ValidationError("Fallback ratings are unverified")
    if value.source in ("manual", "refusal") and not value.verified:
        raise ValidationError("Manual/refusal ratings must be verified")
    if value.source == "refusal" and value.rating != "questionable":
        raise ValidationError("Refusal ratings must be questionable")


def set_rating(db, image_id, *, expected, value):
    validate_rating(value)
    image = get_image(db, image_id)
    old = rating_value(image)
    if old == value:
        return WriteResult(image, False)
    if old != expected:
        raise StateConflict("Rating changed while the result was being prepared")
    values = (value.rating, value.source, int(value.verified), value.rated_at) if value else (None, None, 0, None)
    db.execute("UPDATE images SET rating=?,rating_source=?,rating_verified=?,rated_at=? WHERE image_id=?",
               (*values, image_id))
    return WriteResult(get_image(db, image_id), True)


def clear_recomputable_ratings(db, image_ids):
    ids = tuple(dict.fromkeys(image_ids))
    for image_id in ids:
        integer(image_id, "image ID", 1)
    count = 0
    for chunk in db.chunks(ids):
        slots = ",".join("?" for _ in chunk)
        count += db.execute(
            f"UPDATE images SET rating=NULL,rating_source=NULL,rating_verified=0,rated_at=NULL "
            f"WHERE image_id IN ({slots}) AND rating_source IN ('keyword','model','fallback')", chunk,
        ).rowcount
    return count
