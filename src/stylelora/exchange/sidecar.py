"""Explicit first-line tags / blank line / optional natural-language format."""

import hashlib

from ..project_db.types import OriginalInput, Tag
from ..project_db.validation import primitive
from ..tagging.plain import validate_plain_tag
from ..tagging.tokenizer import parse_original_prompt
from .errors import ExchangeError
from .types import SidecarResult


def decode_sidecar(data, encoding="utf-8-sig"):
    if encoding not in ("utf-8-sig", "utf-8", "gbk", "auto"):
        raise ExchangeError("encoding:unsupported")
    # A UTF-8 BOM is authoritative, even if its remaining bytes are malformed.
    selected = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else ("utf-8" if encoding == "auto" else encoding)
    try:
        result = data.decode(selected, errors="strict")
    except UnicodeError as exc:
        if encoding != "auto" or selected != "utf-8":
            raise ExchangeError("encoding:invalid") from exc
        selected = "gbk"
        try:
            result = data.decode(selected, errors="strict")
        except UnicodeError as fallback_error:
            raise ExchangeError("encoding:invalid") from fallback_error
    if "\0" in result:
        raise ExchangeError("sidecar:nul_character")
    return result.replace("\r\n", "\n").replace("\r", "\n"), selected


def parse_sidecar(data, *, options, catalog=None):
    if data is None:
        return SidecarResult((), ("sidecar:missing",))
    if len(data) > options.max_sidecar_bytes:
        raise ExchangeError("sidecar:too_large")
    value, encoding = decode_sidecar(data, options.encoding)
    if options.parser not in ("literal-tags-v1", "catalog-comma-v1"):
        raise ExchangeError("sidecar:unsupported_parser")
    if options.parser == "catalog-comma-v1" and (catalog is None or not catalog.entries):
        raise ExchangeError("catalog:required")
    lines = value.split("\n")
    header = lines[0]
    tail = lines[1:]
    has_separator = bool(tail) and not tail[0].strip()
    if tail and not has_separator and any(line.strip() for line in tail):
        raise ExchangeError("sidecar:invalid_layout", "Separate the tag line and description with a blank line")
    nl = "\n".join(tail[1:]).strip() if has_separator else ""
    tags, unknown, seen, evidence = [], [], set(), []
    if options.parser == 'catalog-comma-v1':
        parsed = parse_original_prompt(header if header.strip() else '',catalog=catalog)
        if parsed.unverifiable:
            raise ExchangeError('tokenizer:unverifiable')
        tags.extend(parsed.tags)
        unknown.extend([parsed.natural_language] if parsed.natural_language else [])
        evidence = primitive(parsed.segments)
    for part in header.split(",") if header.strip() and options.parser == 'literal-tags-v1' else ():
        raw = part.strip()
        if not raw:
            raise ExchangeError("tag:empty_segment")
        key = validate_plain_tag(raw)
        if key not in seen:
            tags.append(Tag(raw))
            seen.add(key)
    fallback = options.encoding == "auto" and encoding == "gbk"
    warnings = ["encoding:gbk_fallback"] if fallback else []
    if unknown:
        if not has_separator:
            raise ExchangeError("sidecar:ambiguous_unknown", "Use an explicit description paragraph or literal-tags-v1")
        nl = ", ".join(unknown) + ("\n" + nl if nl else "")
        warnings.append("sidecar:unknown_to_nl")
    params = {
        "schema_version": 1, "reader": "txt_sidecar", "parser": options.parser, "encoding": encoding,
        "sidecar_sha256": hashlib.sha256(data).hexdigest(),
        "catalog_revision": catalog.revision if options.parser == "catalog-comma-v1" else None,
        "tokenizer_version": 'comma-v1' if options.parser == 'catalog-comma-v1' else 'literal-tags-v1',
        "segments": evidence,
        "encoding_requested": options.encoding, "encoding_fallback": fallback,
        "warnings": list(warnings),
    }
    originals = [OriginalInput("tags", tuple(tags), params=params)]
    if nl:
        originals.append(OriginalInput("nl", nl, params=params))
    return SidecarResult(tuple(originals), tuple(warnings), encoding, fallback)


def serialize_caption(tags, natural_language=None):
    if not tags:
        raise ExchangeError("caption:empty")
    keys = tuple(validate_plain_tag(tag) for tag in tags)
    if len(set(keys)) != len(keys):
        raise ExchangeError("caption:duplicate_tag")
    nl = (natural_language or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\0" in nl:
        raise ExchangeError("caption:nul_character")
    return (", ".join(keys) + ("\n\n" + nl if nl else "") + "\n").encode("utf-8")
