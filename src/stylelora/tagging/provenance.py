"""Independent Git blob checks for reviewed small files; never establishes trust."""
import hashlib
import re

from ..runtime.errors import ServiceError


def verify_git_blob(content: bytes, *, oid: str, size: int) -> str:
    """Use an independently recorded publisher object ID, then return SHA-256.

    The returned SHA-256 is locally calculated, not a publisher-issued checksum.
    Callers must obtain and review the expected object identity before this call.
    """
    if (not re.fullmatch(r'[0-9a-f]{40}', oid) or type(size) is not int
            or not 0 < size <= 16*1024**2 or len(content) != size):
        raise ServiceError(503, 'tagger:provenance_mismatch')
    identity = hashlib.sha1(b'blob '+str(size).encode('ascii')+b'\0'+content).hexdigest()
    if identity != oid:
        raise ServiceError(503, 'tagger:provenance_mismatch')
    return hashlib.sha256(content).hexdigest()
