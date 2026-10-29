"""Strict DTO validation and project-local path handling."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

from .errors import UnsafePath, ValidationError
from .types import JsonObject, JsonValue, Tag

LAYERS = ("original", "wd", "llm", "manual", "rule")
SOURCE_STATES = ("draft", "running", "paused", "done", "canceled")
ITEM_STATES = ("pending", "sent", "done", "failed", "skipped")
JOB_STATES = ("pending", "running", "paused", "done", "failed", "canceled")
REVIEWS = ("unreviewed", "keep", "reject")
ASPECTS = ("landscape", "portrait", "square")
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def integer(value: int, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def text(value: str, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (nonempty and not value.strip()):
        raise ValidationError(f"Invalid {name}")
    return value


def choice(value: str, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed or not isinstance(value, str):
        raise ValidationError(f"Invalid {name}")
    return value


def confidence(value: float | None) -> None:
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValidationError("Confidence must be finite and between 0 and 1")


def normalize_tag(value: str) -> str:
    key = text(value, "tag", nonempty=True).strip().lower().replace("_", " ").strip()
    if not key:
        raise ValidationError("Empty normalized tag")
    return key


def tags(values: tuple[Tag, ...]) -> None:
    if not isinstance(values, (tuple, list)):
        raise ValidationError("Tags must be a sequence")
    for value in values:
        if not isinstance(value, Tag):
            raise ValidationError("Expected Tag DTO")
        normalize_tag(value.tag)
        confidence(value.conf)


def timestamp(value: str) -> str:
    text(value, "UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValidationError("Invalid UTC timestamp") from exc
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", value):
        raise ValidationError("UTC timestamp must contain six fractional digits")
    return value


def utc_now(after: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    if after is not None:
        old = datetime.strptime(timestamp(after), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        now = max(now, old + timedelta(microseconds=1))
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def primitive(value):
    """Convert detached DTOs to JSON values; never coerce unknown types."""
    if is_dataclass(value):
        return primitive(asdict(value))
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValidationError("JSON object keys must be strings")
        return {k: primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValidationError("Value is not finite JSON")


def encode(value) -> str:
    return json.dumps(primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(value: str) -> JsonValue:
    try:
        result = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        primitive(result)
        return result
    except (ValueError, TypeError) as exc:
        raise ValidationError("Invalid JSON") from exc


def object_json(value: JsonObject, *, versioned: bool = False) -> str:
    if not isinstance(value, dict):
        raise ValidationError("Expected JSON object")
    if versioned and (type(value.get("schema_version")) is not int or value.get("schema_version") != 1):
        raise ValidationError("Expected schema_version=1")
    return encode(value)


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def sha256(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError("Invalid SHA-256")
    return value


def project_data(value) -> None:
    """Reject product credentials/locations in persisted project snapshots."""
    value = primitive(value)
    forbidden = {"url", "base_url", "endpoint", "token", "api_key", "secret", "password",
                 "authorization", "import_root", "absolute_path"}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in forbidden:
                raise ValidationError("Product credentials and locations cannot be stored in a project")
            project_data(child)
    elif isinstance(value, list):
        for child in value:
            project_data(child)
    elif isinstance(value, str):
        if value.startswith(("http://", "https://", "\\\\")) or PureWindowsPath(value).drive:
            raise ValidationError("External locations cannot be stored in project snapshots")


def relative_path(root: Path, value: str) -> Path:
    text(value, "relative path", nonempty=True)
    windows = PureWindowsPath(value)
    parts = value.split("/")
    if "\\" in value or ":" in value or windows.drive or windows.root or value.startswith("/") or any(
        p in ("", ".", "..") or p.endswith((" ", ".")) or PureWindowsPath(p).is_reserved() for p in parts
    ):
        raise UnsafePath("Unsafe project-relative path")
    base = root.resolve()
    result = base.joinpath(*parts)
    if not result.resolve().is_relative_to(base):
        raise UnsafePath("Path escapes the project")
    # A project-local symlink/junction is not a valid managed storage entry either.
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise UnsafePath("Managed project paths cannot be symlinks or junctions")
    return result


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
