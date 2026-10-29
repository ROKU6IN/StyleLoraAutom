"""Same-volume, exclusive hardlinks with identity and content verification."""
import errno
import hashlib
import os
import stat

from .common import is_link
from .errors import ExchangeError


def _verify(source, target, stream, expected_hash, expected_size):
    before = os.fstat(stream.fileno())
    if (not stat.S_ISREG(before.st_mode) or is_link(source) or is_link(target)
            or not os.path.samestat(before, source.stat())
            or not os.path.samestat(before, target.stat())):
        raise ExchangeError('export:hardlink_changed')
    stream.seek(0)
    checksum, size = hashlib.sha256(), 0
    while block := stream.read(1024 * 1024):
        size += len(block)
        if size > expected_size:
            raise ExchangeError('input:changed')
        checksum.update(block)
    after = os.fstat(stream.fileno())
    if (size != expected_size or checksum.hexdigest() != expected_hash
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            or is_link(source) or is_link(target)
            or not os.path.samestat(after, source.stat())
            or not os.path.samestat(after, target.stat())):
        raise ExchangeError('input:changed')
    return size, checksum.hexdigest()


def hardlink_verified(source, target, expected_hash, expected_size):
    """Never overwrite or fall back to copy; remove only a link created here on failure."""
    if is_link(source):
        raise ExchangeError('export:hardlink_unsafe_source')
    with source.open('rb') as stream:
        source_stat = os.fstat(stream.fileno())
        if not stat.S_ISREG(source_stat.st_mode):
            raise ExchangeError('export:hardlink_unsafe_source')
        if source_stat.st_dev != target.parent.stat().st_dev:
            raise ExchangeError('export:hardlink_cross_volume')
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ExchangeError('export:destination_exists') from exc
        except NotImplementedError as exc:
            raise ExchangeError('export:hardlink_unsupported') from exc
        except OSError as exc:
            if exc.errno == errno.EXDEV or getattr(exc, 'winerror', None) == 17:
                raise ExchangeError('export:hardlink_cross_volume') from exc
            if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS) or getattr(exc, 'winerror', None) in (1, 50):
                raise ExchangeError('export:hardlink_unsupported') from exc
            raise
        try:
            return _verify(source, target, stream, expected_hash, expected_size)
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def verify_hardlink(source, target, expected_hash, expected_size):
    with source.open('rb') as stream:
        return _verify(source, target, stream, expected_hash, expected_size)
