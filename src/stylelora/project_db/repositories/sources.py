from dataclasses import dataclass, replace

from ..errors import LockedSource, SeedConflict, StateConflict, ValidationError
from ..types import SourceDraft, SourceItemRow, SourceRow, UNSET, WriteResult
from ..validation import (
    ASPECTS, ITEM_STATES, SOURCE_STATES, choice, integer, object_json, project_data,
    text, utc_now,
)
from .common import get, in_json, paged, row_dto


def get_source(db, source_id):
    return get(db, "sources", source_id, SourceRow)


@dataclass(frozen=True, slots=True)
class SourceState:
    source_id: int
    type: str
    status: str
    locked: bool


def get_source_state(db, source_id):
    """Read live control state without decoding a potentially large manifest."""
    integer(source_id, 'source ID', 1)
    return row_dto(db.execute('SELECT source_id,type,status,locked FROM sources WHERE source_id=?',
                             (source_id,)).fetchone(), SourceState)


def get_item(db, item_id):
    return get(db, "source_items", item_id, SourceItemRow)


def list_sources(db, query, page):
    for state in query.statuses:
        choice(state, SOURCE_STATES, "source status")
    where, param = in_json("status", query.statuses)
    return paged(db, "sources", SourceRow, page, where=where if query.statuses else "1=1",
                 params=(param,) if query.statuses else ())


def list_items(db, query, page):
    integer(query.source_id, "source ID", 1)
    for state in query.statuses:
        choice(state, ITEM_STATES, "item status")
    where, params = "source_id=?", [query.source_id]
    if query.statuses:
        clause, encoded = in_json("status", query.statuses)
        where += " AND " + clause
        params.append(encoded)
    return paged(db, "source_items", SourceItemRow, page, where=where, params=params)


def validate_draft(draft):
    choice(draft.type, ("import", "nai"), "source type")
    text(draft.name, "source name", nonempty=True)
    choice(draft.cost_policy, ("deny", "ask", "allow"), "cost policy")
    text(draft.style_prompt, "style prompt")
    text(draft.negative_prompt, "negative prompt")
    if draft.target_count is not None:
        integer(draft.target_count, "target_count", 1)
    object_json(draft.config)
    project_data(draft.config)
    if "cost_policy" in draft.config:
        raise ValidationError("cost_policy is a separate editable source field")
    if draft.type == "import" and (draft.style_prompt or draft.negative_prompt):
        raise ValidationError("Import sources cannot have generation prompts")


def create_source(db, draft):
    validate_draft(draft)
    cursor = db.execute(
        "INSERT INTO sources(type,name,config_json,cost_policy,style_prompt,negative_prompt,target_count,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (draft.type, draft.name, object_json(draft.config), draft.cost_policy, draft.style_prompt,
         draft.negative_prompt, draft.target_count, utc_now()),
    )
    return get_source(db, cursor.lastrowid)


def update_source(db, source_id, patch):
    old = get_source(db, source_id)
    protected = ("type", "config", "style_prompt", "negative_prompt", "target_count")
    if old.locked and any(getattr(patch, key) is not UNSET for key in protected):
        raise LockedSource("Started source configuration is immutable")
    draft = SourceDraft(old.type, old.name, old.config_json, old.cost_policy,
                        old.style_prompt, old.negative_prompt, old.target_count)
    changes = {key: getattr(patch, key) for key in protected if getattr(patch, key) is not UNSET}
    if patch.name is not None:
        changes["name"] = patch.name
    if patch.cost_policy is not None:
        changes["cost_policy"] = patch.cost_policy
    draft = replace(draft, **changes)
    validate_draft(draft)
    if draft == SourceDraft(old.type, old.name, old.config_json, old.cost_policy,
                            old.style_prompt, old.negative_prompt, old.target_count):
        return WriteResult(old, False)
    db.execute(
        "UPDATE sources SET type=?,name=?,config_json=?,cost_policy=?,style_prompt=?,negative_prompt=?,target_count=? "
        "WHERE source_id=?",
        (draft.type, draft.name, object_json(draft.config), draft.cost_policy, draft.style_prompt,
         draft.negative_prompt, draft.target_count, source_id),
    )
    if draft.cost_policy != old.cost_policy and db.project:
        db.after_commit.append(lambda: db.project._cycles.pop(source_id, None))
    return WriteResult(get_source(db, source_id), True)


def delete_empty_draft(db, source_id):
    source = get_source(db, source_id)
    if source.status != "draft" or db.execute(
        "SELECT 1 FROM source_items WHERE source_id=? UNION ALL SELECT 1 FROM images WHERE source_id=?",
        (source_id, source_id),
    ).fetchone():
        raise StateConflict("Only an empty, unstarted draft can be deleted")
    db.execute("DELETE FROM sources WHERE source_id=?", (source_id,))


def seed_items(db, source_id, items):
    db.require("start_source")
    source = get_source(db, source_id)
    items = tuple(items)
    if not items:
        raise ValidationError("A source must contain at least one item")
    if [item.seq for item in items] != list(range(1, len(items) + 1)):
        raise ValidationError("Item sequences must be continuous and start at 1")
    for item in items:
        integer(item.seq, "sequence", 1)
        text(item.prompt, "prompt", nonempty=True)
        if item.aspect is not None:
            choice(item.aspect, ASPECTS, "aspect")
        if source.type == "import" and item.aspect is not None:
            raise ValidationError("Import items cannot specify an aspect")
    existing = tuple(row_dto(row, SourceItemRow) for row in db.execute(
        "SELECT * FROM source_items WHERE source_id=? ORDER BY seq", (source_id,),
    ))
    wanted = tuple((item.seq, item.prompt, item.aspect) for item in items)
    if existing:
        actual = tuple((item.seq, item.prompt, item.aspect) for item in existing)
        if actual != wanted:
            raise SeedConflict("Source seeds conflict with the previously stored full list")
        return existing
    if source.locked or source.status != "draft":
        raise LockedSource("Seeds cannot be appended after source start")
    now = utc_now()
    db.executemany(
        "INSERT INTO source_items(source_id,seq,prompt,aspect,updated_at) VALUES(?,?,?,?,?)",
        ((source_id, item.seq, item.prompt, item.aspect, now) for item in items),
    )
    return tuple(row_dto(row, SourceItemRow) for row in db.execute(
        "SELECT * FROM source_items WHERE source_id=? ORDER BY seq", (source_id,),
    ))


def count_item_states(db, source_id):
    get_source_state(db, source_id)
    counts = dict.fromkeys(ITEM_STATES, 0)
    counts.update(dict(db.execute(
        "SELECT status,count(*) FROM source_items WHERE source_id=? GROUP BY status", (source_id,),
    )))
    return counts
