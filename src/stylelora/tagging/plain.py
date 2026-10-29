import re

from ..exchange.errors import ExchangeError
from ..project_db.errors import ValidationError
from ..project_db.validation import normalize_tag


def validate_plain_tag(value, *, trigger=False):
    try:
        key = normalize_tag(value)
    except ValidationError as exc:
        raise ExchangeError("tag:invalid") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or any(char in value for char in ",:{}[]\\|"):
        raise ExchangeError("tag:unsupported_syntax")
    if key.upper() in ("BREAK", "AND") or "::" in key:
        raise ExchangeError("tag:unsupported_syntax")
    if "(" in key or ")" in key:
        # Literal disambiguation tags are allowed; weighting syntax is not evaluated.
        if trigger or not re.fullmatch(r"[^()]+ \([^():]+\)", key):
            raise ExchangeError("tag:unsupported_syntax")
    return key


def parse_tag_argument(value):
    if not value.strip():
        return ()
    result, seen = [], set()
    for part in value.split(","):
        if not part.strip():
            raise ExchangeError("tag:empty_segment")
        key = validate_plain_tag(part)
        if key not in seen:
            result.append(key)
            seen.add(key)
    return tuple(result)
