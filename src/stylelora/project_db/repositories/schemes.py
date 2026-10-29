from dataclasses import asdict

from ..errors import StateConflict, UnsupportedScheme, ValidationError
from ..types import ExportSchemeInput, ExportSchemeRow, ImportSchemeInput, ImportSchemeRow, WriteResult
from ..validation import choice, encode, object_json, project_data, text, utc_now
from .common import get, paged, row_dto


def get_export_scheme(db, scheme_id):
    return get(db, "export_schemes", scheme_id, ExportSchemeRow)


def get_import_scheme(db, scheme_id):
    return get(db, "import_schemes", scheme_id, ImportSchemeRow)


def get_default_import_scheme(db):
    return row_dto(db.execute("SELECT * FROM import_schemes WHERE is_default=1").fetchone(), ImportSchemeRow)


def list_export_schemes(db, page):
    return paged(db, "export_schemes", ExportSchemeRow, page)


def list_import_schemes(db, page):
    return paged(db, "import_schemes", ImportSchemeRow, page)


def _validate(value, importing):
    text(value.name, "scheme name", nonempty=True)
    project_data(asdict(value))
    if importing:
        if value.reader != "txt_sidecar":
            raise UnsupportedScheme("Unsupported import reader")
        object_json(value.params_json, versioned=True)
        if type(value.is_default) is not bool:
            raise ValidationError("is_default must be boolean")
        encoding = value.params_json.get("encoding")
        if encoding not in ("utf-8-sig", "utf-8", "gbk", "auto"):
            raise UnsupportedScheme("Explicit supported text encoding is required")
    else:
        if value.exporter != "image_txt":
            raise UnsupportedScheme("Unsupported exporter")
        for obj in (value.filter_json, value.caption_config_json, value.output_config_json):
            object_json(obj, versioned=True)
        if not isinstance(value.pipeline_json, (tuple, list)):
            raise ValidationError("pipeline must be an array")
        encode(value.pipeline_json)
        choice(value.caption_config_json.get("tag_sort", "layer_first"),
               ("layer_first", "prompt_pyramid"), "tag sorter")


def create_export_scheme(db, value):
    _validate(value, False)
    now = utc_now()
    cursor = db.execute(
        "INSERT INTO export_schemes(name,exporter,filter_json,caption_config_json,output_config_json,"
        "pipeline_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (value.name, value.exporter, encode(value.filter_json), encode(value.caption_config_json),
         encode(value.output_config_json), encode(value.pipeline_json), now, now),
    )
    return get_export_scheme(db, cursor.lastrowid)


def create_import_scheme(db, value):
    _validate(value, True)
    count = db.execute("SELECT count(*) FROM import_schemes").fetchone()[0]
    now = utc_now()
    if value.is_default and count:
        previous = get_default_import_scheme(db)
        now = utc_now(previous.updated_at) if previous else now
        db.execute("UPDATE import_schemes SET is_default=0,updated_at=? WHERE is_default=1", (now,))
    cursor = db.execute(
        "INSERT INTO import_schemes(name,reader,params_json,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (value.name, value.reader, encode(value.params_json), int(value.is_default or not count), now, now),
    )
    return get_import_scheme(db, cursor.lastrowid)


def update_export_scheme(db, scheme_id, value, *, expected_updated_at):
    _validate(value, False)
    old = get_export_scheme(db, scheme_id)
    current = ExportSchemeInput(old.name, old.exporter, old.filter_json, old.caption_config_json,
                                old.output_config_json, old.pipeline_json)
    if current == value:
        return WriteResult(old, False)
    if old.updated_at != expected_updated_at:
        raise StateConflict("Export scheme changed")
    db.execute(
        "UPDATE export_schemes SET name=?,exporter=?,filter_json=?,caption_config_json=?,output_config_json=?,"
        "pipeline_json=?,updated_at=? WHERE scheme_id=?",
        (value.name, value.exporter, encode(value.filter_json), encode(value.caption_config_json),
         encode(value.output_config_json), encode(value.pipeline_json), utc_now(old.updated_at), scheme_id),
    )
    return WriteResult(get_export_scheme(db, scheme_id), True)


def update_import_scheme(db, scheme_id, value, *, expected_updated_at):
    _validate(value, True)
    old = get_import_scheme(db, scheme_id)
    if ImportSchemeInput(old.name, old.reader, old.params_json, old.is_default) == value:
        return WriteResult(old, False)
    if old.updated_at != expected_updated_at:
        raise StateConflict("Import scheme changed")
    if old.is_default and not value.is_default:
        raise StateConflict("Select a replacement default before clearing the current default")
    if value.is_default:
        set_default_import_scheme(db, scheme_id)
    db.execute("UPDATE import_schemes SET name=?,reader=?,params_json=?,updated_at=? WHERE scheme_id=?",
               (value.name, value.reader, encode(value.params_json), utc_now(old.updated_at), scheme_id))
    return WriteResult(get_import_scheme(db, scheme_id), True)


def set_default_import_scheme(db, scheme_id):
    old = get_import_scheme(db, scheme_id)
    if old.is_default:
        return WriteResult(old, False)
    previous = get_default_import_scheme(db)
    now = utc_now(max(old.updated_at, previous.updated_at))
    db.execute("UPDATE import_schemes SET is_default=0,updated_at=? WHERE is_default=1", (now,))
    db.execute("UPDATE import_schemes SET is_default=1,updated_at=? WHERE scheme_id=?", (now, scheme_id))
    return WriteResult(get_import_scheme(db, scheme_id), True)


def delete_import_scheme(db, scheme_id):
    old = get_import_scheme(db, scheme_id)
    if old.is_default:
        raise StateConflict("Switch the default import scheme before deleting it")
    db.execute("DELETE FROM import_schemes WHERE scheme_id=?", (scheme_id,))


def delete_export_scheme(db, scheme_id):
    get_export_scheme(db, scheme_id)
    if db.execute("SELECT count(*) FROM export_schemes").fetchone()[0] <= 1:
        raise StateConflict("The final export scheme cannot be deleted")
    db.execute("DELETE FROM export_schemes WHERE scheme_id=?", (scheme_id,))


def require_executable_export(scheme):
    if scheme.pipeline_json:
        raise UnsupportedScheme("Post-processing pipelines are stored but not implemented")
