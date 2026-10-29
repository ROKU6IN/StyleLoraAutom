import json
from dataclasses import dataclass
from pathlib import Path

from ..exchange.errors import ExchangeError
from ..project_db.validation import digest, normalize_tag
from .plain import validate_plain_tag


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class TagCatalog:
    revision: str
    entries: dict[str, str]
    categories: dict[str, str]
    declared_complete: bool = False

    def lookup(self, value):
        return self.entries.get(normalize_tag(value))


def load_catalog(path):
    """Read a user-supplied local catalog. No remote queries or implicit installation."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError()
        if value.get("kind") != "tag_catalog" or not isinstance(value.get("tags"), list):
            raise ValueError()
        complete = value.get("complete", False)
        if type(complete) is not bool:
            raise ValueError()
        entries, categories = {}, {}
        for item in value["tags"]:
            if not isinstance(item, dict) or not isinstance(item.get("tag"), str):
                raise ValueError()
            canonical = validate_plain_tag(item["tag"])
            category = item.get("category", "general")
            if not isinstance(category, str):
                raise ValueError()
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
                raise ValueError()
            for alias in (canonical, *aliases):
                key = validate_plain_tag(alias)
                if key in entries and entries[key] != canonical:
                    raise ValueError()
                entries[key] = canonical
            if canonical in categories and categories[canonical] != category:
                raise ValueError()
            categories[canonical] = category
        return TagCatalog(digest(value), entries, categories, complete)
    except (ValueError, TypeError, KeyError, UnicodeError, ExchangeError) as exc:
        raise ExchangeError("catalog:invalid") from exc
