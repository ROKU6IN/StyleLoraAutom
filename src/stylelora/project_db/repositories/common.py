from dataclasses import fields

from ..errors import NotFound, ValidationError
from ..types import Page, Tag
from ..validation import decode, integer

TABLE_KEYS = {
    "sources": "source_id", "source_items": "item_id", "images": "image_id",
    "annotations": "annotation_id", "jobs": "job_id", "operation_log": "op_id",
    "export_schemes": "scheme_id", "import_schemes": "scheme_id",
}
BOOLS = {"locked", "file_missing", "rating_verified", "is_current", "is_default"}


def row_dto(row, cls):
    if row is None:
        raise NotFound("Record not found")
    values = dict(row)
    for key, value in tuple(values.items()):
        if key in BOOLS:
            values[key] = bool(value)
        elif key.endswith("_json") and value is not None:
            values[key] = decode(value)
            if key in ("changes_json", "pipeline_json"):
                values[key] = tuple(values[key])
    if cls.__name__ == "AnnotationRow" and values["type"] == "tags":
        values["content"] = tuple(Tag(**tag) for tag in decode(values["content"]))
    return cls(**{field.name: values[field.name] for field in fields(cls)})


def get(db, table, key, cls):
    integer(key, "record ID", 1)
    return row_dto(db.execute(f"SELECT * FROM {table} WHERE {TABLE_KEYS[table]}=?", (key,)).fetchone(), cls)


def paged(db, table, cls, page, *, where="1=1", params=()):
    integer(page.after_id, "after_id")
    integer(page.limit, "limit", 1)
    if page.limit > 1000:
        raise ValidationError("Page limit exceeds 1000")
    pk = TABLE_KEYS[table]
    upper = page.upper_id
    if upper is None:
        upper = db.execute(f"SELECT COALESCE(MAX({pk}),0) FROM {table}").fetchone()[0]
    integer(upper, "upper_id")
    rows = db.execute(
        f"SELECT * FROM {table} WHERE ({where}) AND {pk}>? AND {pk}<=? ORDER BY {pk} LIMIT ?",
        (*params, page.after_id, upper, page.limit + 1),
    ).fetchall()
    items = tuple(row_dto(row, cls) for row in rows[:page.limit])
    return Page(items, getattr(items[-1], pk) if len(rows) > page.limit else None, upper)


def in_json(column, values):
    """A bound JSON array uses one SQL variable even for a large selection."""
    from ..validation import encode
    return f"{column} IN (SELECT value FROM json_each(?))", encode(values)
