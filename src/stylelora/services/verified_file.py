"""Hold a restored managed file stable while its database flag is committed."""
from contextlib import contextmanager, ExitStack
import hashlib
import os
import stat

from ..exchange.common import is_link
from ..project_db.validation import relative_path
from ..runtime.errors import ServiceError
from .storage_files import identity

MAX_BYTES = 100 * 1024 * 1024


@contextmanager
def pinned(path, *, directory=False):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                     getattr(os, 'O_NONBLOCK', 0) | (getattr(os, 'O_DIRECTORY', 0) if directory else 0))
        try:
            yield fd
        finally:
            os.close(fd)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes as W
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
    create.restype = W.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [W.HANDLE], W.BOOL
    # Directory handles deny rename/delete; the file also denies existing/new
    # write handles. OPEN_REPARSE_POINT opens a link itself, never its target.
    handle = create(str(path), 0x80 if directory else 0x80000000, 3 if directory else 1,
                    None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
    if handle == W.HANDLE(-1).value:
        code = ctypes.get_last_error()
        if code in (32, 33):
            raise ServiceError(409, 'image:file_busy')
        raise ctypes.WinError(code)
    fd = None
    try:
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes, final.restype = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer) or os.path.normcase(buffer.value.removeprefix('\\\\?\\')) != os.path.normcase(str(path)):
            raise ServiceError(409, 'image:file_changed')
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        yield fd
    finally:
        if fd is None:
            close(handle)
        else:
            os.close(fd)


@contextmanager
def verified_file(project, image, canceled):
    path = relative_path(project.root, 'images/'+image.filename)
    parents = [project.root, path.parent]
    if any(is_link(parent) for parent in (path, *path.parents)):
        raise ServiceError(422, 'path:unsafe')
    if image.file_size > MAX_BYTES:
        raise ServiceError(413, 'image:too_large')
    with ExitStack() as stack:
        held = [(parent, stack.enter_context(pinned(parent, directory=True))) for parent in parents]
        fd = stack.enter_context(pinned(path))
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or getattr(initial, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ServiceError(422, 'path:unsafe')
        if initial.st_size != image.file_size:
            raise ServiceError(409, 'image:hash_mismatch')
        parent_ids = [(parent, identity(os.fstat(handle))[:2]) for parent, handle in held]
        def check():
            if canceled():
                raise ServiceError(409, 'operation:canceled')
            relative_path(project.root, 'images/'+image.filename)
            if any(is_link(parent) or identity(parent.stat())[:2] != expected for parent, expected in parent_ids):
                raise ServiceError(409, 'image:file_changed')
            if is_link(path) or identity(path.stat()) != identity(initial) or identity(os.fstat(fd)) != identity(initial):
                raise ServiceError(409, 'image:file_changed')
        check()
        checksum, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 1024 * 1024):
            check()
            size += len(chunk)
            if size > MAX_BYTES:
                raise ServiceError(413, 'image:too_large')
            checksum.update(chunk)
        if size != image.file_size or checksum.hexdigest() != image.sha256:
            raise ServiceError(409, 'image:hash_mismatch')
        check()
        yield check
