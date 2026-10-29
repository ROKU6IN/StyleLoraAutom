"""Small durable files; database mutations remain in the writer."""

import os
import uuid
from pathlib import Path

from .errors import ValidationError
from .types import ProjectMetadata
from .validation import decode, encode, integer, text, timestamp


def atomic_bytes(path: Path, data: bytes):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, (encode(value) + "\n").encode("utf-8"))


def read_metadata(root: Path) -> ProjectMetadata:
    value = decode((root / "project.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"format_version", "name", "trigger_word", "created_at"}:
        raise ValidationError("Invalid project metadata fields")
    integer(value["format_version"], "format_version", 1)
    text(value["name"], "project name", nonempty=True)
    text(value["trigger_word"], "trigger word")
    timestamp(value["created_at"])
    return ProjectMetadata(**value)
