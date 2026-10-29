import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from ..exchange.common import is_link
from ..project_db.validation import digest, relative_path
from .contracts import uuid_string
from .errors import ServiceError


def expires(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Grant:
    grant_id: str
    purpose: str
    path: Path
    project_id: str | None
    expires_at: str
    deadline: float
    identity: tuple
    version: str
    users: int = 0

    def view(self):
        return {"grant_id": self.grant_id, "purpose": self.purpose, "display_name": self.path.name,
                "project_id": self.project_id, "expires_at": self.expires_at, "version": self.version}


class PathGrants:
    def __init__(self):
        self._values = {}
        self._lock = threading.RLock()

    def add(self, path, purpose, project_id=None):
        if purpose not in ("project_create", "project_open", "import_root"):
            raise ServiceError(422, "path:invalid_purpose")
        raw = Path(path).absolute()
        if str(raw).startswith(("\\\\", "//")) or any(is_link(part) for part in (raw, *raw.parents)):
            raise ServiceError(422, "path:unsafe")
        if any(part in (".", "..") or part.endswith((" ", ".")) or ":" in part or PureWindowsPath(part).is_reserved()
               for part in raw.parts[1:]):
            raise ServiceError(422, "path:unsafe")
        checked = raw.resolve()
        if purpose == "project_create":
            if checked.suffix != ".slproj" or checked.exists() or not checked.parent.is_dir():
                raise ServiceError(422, "path:invalid_project_target")
        elif not checked.is_dir():
            raise ServiceError(422, "path:unavailable")
        if purpose == "project_open":
            for name in ("project.json", "project.db", "images", "exports"):
                if not relative_path(checked, name).exists():
                    raise ServiceError(422, "path:invalid_project")
        if purpose == "import_root" and project_id is None:
            raise ServiceError(422, "path:project_required")
        stat = (checked.parent if purpose == "project_create" else checked).stat()
        value = Grant(str(uuid4()), purpose, checked, project_id, expires(900), time.monotonic()+900,
                      (stat.st_dev, stat.st_ino), secrets.token_hex(16))
        with self._lock:
            self._values = {key: grant for key, grant in self._values.items() if grant.users or grant.deadline > time.monotonic()}
            if len(self._values) >= 256:
                raise ServiceError(429, "path:grant_limit")
            self._values[value.grant_id] = value
        return value.view()

    def get(self, grant_id, purpose, project_id=None):
        with self._lock:
            value = self._values.get(uuid_string(grant_id))
            if value is None or value.deadline <= time.monotonic():
                raise ServiceError(410, "path:grant_expired")
            if value.purpose != purpose or value.project_id != project_id:
                raise ServiceError(403, "path:grant_denied")
            if any(is_link(part) for part in (value.path, *value.path.parents)):
                raise ServiceError(422, "path:unsafe")
            stat = (value.path.parent if purpose == "project_create" else value.path).stat()
            if (stat.st_dev, stat.st_ino) != value.identity:
                raise ServiceError(409, "path:identity_changed")
            return value.path

    def acquire(self, grant_id, purpose, project_id=None):
        with self._lock:
            path = self.get(grant_id, purpose, project_id)
            value = self._values[grant_id]
            value.users += 1
        released = False
        def release():
            nonlocal released
            with self._lock:
                if not released:
                    released = True
                    value.users -= 1
        return path, release

    def revoke(self, grant_id, expected):
        from .contracts import Versions
        with self._lock:
            value = self._values.get(grant_id)
            if value is None:
                raise ServiceError(410, "path:grant_expired")
            Versions.require(expected, value.version)
            if value.users:
                raise ServiceError(409, "path:grant_in_use")
            del self._values[grant_id]

    def invalidate(self, project_id):
        with self._lock:
            self._values = {key: value for key, value in self._values.items() if value.project_id != project_id}


class Previews:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def add(self, project_id, kind, content, private):
        with self.lock:
            self.values = {key: value for key, value in self.values.items() if value[0] > time.monotonic()}
            if len(self.values) >= 256:
                raise ServiceError(429, "preview:limit")
            value = {**content, "preview_id": str(uuid4()), "digest": digest(content), "expires_at": expires(900), "version": secrets.token_hex(16), "issues": content.get("issues", [])}
            self.values[value["preview_id"]] = (time.monotonic()+900, project_id, kind, value, private)
            return value

    def get(self, project_id, kind, preview_id, digest_value):
        with self.lock:
            entry = self.values.get(preview_id)
            if entry is None or entry[0] <= time.monotonic() or entry[1] != project_id:
                raise ServiceError(410, "preview:expired")
            if entry[2] != kind or not secrets.compare_digest(entry[3]["digest"], digest_value):
                raise ServiceError(409, "preview:stale")
            return entry[3], entry[4]

    def invalidate(self, project_id):
        with self.lock:
            self.values = {key: value for key, value in self.values.items() if value[1] != project_id}


class Idempotency:
    def __init__(self):
        self.values, self.lock = {}, threading.Lock()

    def run(self, identity, key, body, fn):
        if key is None:
            raise ServiceError(428, "idempotency:key_required")
        key = (identity, uuid_string(key))
        signature = digest(body)
        with self.lock:
            self.values = {k: v for k, v in self.values.items() if not v["event"].is_set() or time.monotonic()-v["created"] < 86400}
            entry = self.values.get(key)
            owner = entry is None
            if owner:
                if len(self.values) >= 4096:
                    raise ServiceError(429, "idempotency:capacity")
                entry = {"signature": signature, "event": threading.Event(), "result": None, "error": None, "created": time.monotonic()}
                self.values[key] = entry
            elif entry["signature"] != signature:
                raise ServiceError(409, "idempotency:key_conflict")
        if owner:
            try:
                entry["result"] = fn()
            except Exception as exc:
                entry["error"] = exc
            finally:
                entry["event"].set()
        else:
            if not entry["event"].wait(30):
                raise ServiceError(503, "idempotency:in_flight", outcome="unknown")
        if entry["error"]:
            raise entry["error"]
        return entry["result"]
