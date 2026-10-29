import errno
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from ..project_db.errors import ProjectClosed
from ..project_db.validation import relative_path
from .errors import ExchangeError


def is_link(path):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def input_root(project, root):
    raw = Path(root).absolute()
    if any(is_link(part) for part in (raw, *raw.parents)):
        raise ExchangeError("input:linked_root")
    root = raw.resolve()
    if not root.is_dir():
        raise ExchangeError("input:directory_missing")
    if root.is_relative_to(project.root) or project.root.is_relative_to(root):
        raise ExchangeError("input:project_overlap", "Choose a separate image directory")
    return root


@contextmanager
def project_operation(project):
    """Serialize file operations and prevent closing a live project underneath them."""
    with project._lifecycle:
        if project._closed or project._closing:
            raise ProjectClosed("Project is closed or closing")
        project._operations += 1
    try:
        with project._file_operations:
            yield
    finally:
        with project._lifecycle:
            project._operations -= 1


def read_bounded(path, maximum):
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ExchangeError("sidecar:too_large")
    return value


def copy_verified(source, target, expected_hash, expected_size, maximum):
    """Exclusive copy, fsync and SHA verification; callers own failure cleanup."""
    checksum = hashlib.sha256()
    copied = 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            copied += len(block)
            if copied > maximum:
                raise ExchangeError("image:too_large")
            checksum.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if copied != expected_size or checksum.hexdigest() != expected_hash:
        raise ExchangeError("input:changed")
    return copied, checksum.hexdigest()


def write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def remove_owned(root, paths):
    """Remove only explicitly owned files inside the supplied temporary directory."""
    root = root.resolve()
    for path in paths:
        path = Path(path)
        if not path.absolute().is_relative_to(root):
            raise ExchangeError("storage:unsafe_cleanup")
        checked = relative_path(root, path.absolute().relative_to(root).as_posix())
        checked.unlink(missing_ok=True)


def io_reason(exc):
    if isinstance(exc, ExchangeError):
        return exc.code
    if isinstance(exc, PermissionError):
        return "storage:permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "input:missing"
    if getattr(exc, "errno", None) == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
        return "storage:full"
    return "storage:io_error"


def notify_progress(callback, value):
    if callback is not None:
        try:
            callback(value)
        except Exception:
            pass  # Observers cannot change the durable outcome of an operation.
