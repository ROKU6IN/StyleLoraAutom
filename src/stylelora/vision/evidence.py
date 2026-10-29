from dataclasses import dataclass

from ..project_db.repositories import annotations, images
from ..project_db.validation import digest, normalize_tag, primitive

INPUTS = (('original', 'tags'), ('original', 'nl'), ('wd', 'tags'))
OUTPUTS = (('llm', 'tags'), ('llm', 'nl'))


@dataclass(frozen=True)
class Evidence:
    image: object
    refs: tuple
    tags: tuple[str, ...]
    natural_language: str
    checksum: str
    current: tuple


def read_evidence(db, image_id):
    rows = [annotations.get_current(db, image_id, *target) for target in (*INPUTS, *OUTPUTS)]
    tags, seen = [], set()
    for row in (rows[0], rows[2]):
        for item in row.content if row else ():
            tag = normalize_tag(item.tag)
            if tag not in seen:
                tags.append(tag)
                seen.add(tag)
    identity = [None if row is None else primitive(row.content) for row in rows[:3]]
    return Evidence(images.get_image(db, image_id), tuple(row.annotation_id if row else None for row in rows),
                    tuple(tags), rows[1].content if rows[1] else '', digest(identity), tuple(rows[3:]))


def unchanged(db, evidence, *, outputs=True):
    fresh = read_evidence(db, evidence.image.image_id)
    return fresh.refs[:5 if outputs else 3] == evidence.refs[:5 if outputs else 3]
