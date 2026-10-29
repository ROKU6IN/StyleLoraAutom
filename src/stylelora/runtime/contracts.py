"""Session versions, signed pagination and finite JSON at transport boundaries."""
import base64
import hashlib
import hmac
import json
import threading
from uuid import UUID

from ..project_db.validation import digest, encode
from .errors import ServiceError


def uuid_string(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise ServiceError(422, "input:invalid", "A canonical UUID is required") from exc


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("non-finite JSON")
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ServiceError(400, "request:malformed_json") from exc


class Versions:
    def __init__(self, secret):
        self.secret = secret
        self._revisions = {}
        self._lock = threading.RLock()

    def get(self, key, value):
        with self._lock:
            data = encode([key, self._revisions.get(key, 0), digest(value)]).encode()
            return hmac.new(self.secret, data, hashlib.sha256).hexdigest()

    def bump(self, key):
        with self._lock:
            self._revisions[key] = self._revisions.get(key, 0) + 1

    @staticmethod
    def require(expected, actual):
        if expected is None:
            raise ServiceError(428, "concurrency:precondition_required")
        if not isinstance(expected, str) or not expected.isascii() or not hmac.compare_digest(expected, actual):
            raise ServiceError(409, "concurrency:version_conflict", details={"current_version": actual})


class CursorCodec:
    def __init__(self, secret):
        self.secret = secret

    def encode(self, value):
        raw = encode(value).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return token + "." + hmac.new(self.secret, raw, hashlib.sha256).hexdigest()

    def decode(self, token, identity):
        try:
            if len(token) > 4096:
                raise ValueError()
            data, signature = token.split(".")
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            if not hmac.compare_digest(signature, hmac.new(self.secret, raw, hashlib.sha256).hexdigest()):
                raise ValueError()
            value = strict_json(raw)
            if value["identity"] != identity:
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError, ServiceError) as exc:
            raise ServiceError(422, "page:invalid_cursor") from exc
