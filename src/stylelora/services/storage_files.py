"""Bounded, non-following inventories and identity-bound file deletion."""
import os
import stat
import hashlib
from pathlib import Path

from ..exchange.common import is_link
from ..project_db.validation import relative_path
from ..project_db.errors import ProjectError
from ..runtime.errors import ServiceError

MAX_ENTRIES = 50000


def identity(info):
    # Python 3.12 Windows stat/fstat disagree on legacy ctime (birth/change).
    stamp = getattr(info, 'st_birthtime_ns', info.st_ctime_ns) if os.name == 'nt' else info.st_ctime_ns
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stamp]


def inventory(root, start='', *, canceled=lambda: False):
    files, errors, directories = [], [], {}
    stack, count = [start], 0
    while stack:
        rel = stack.pop()
        if canceled():
            raise ServiceError(409, 'operation:canceled')
        try:
            path = relative_path(root, rel) if rel else root
            if any(is_link(p) for p in (path, *path.parents)):
                raise ValueError()
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError()
            directories[rel] = identity(info)[:2]
            with os.scandir(path) as entries:
                for entry in entries:
                    count += 1
                    if count % 256 == 0 and canceled():
                        raise ServiceError(409, 'operation:canceled')
                    if count > MAX_ENTRIES:
                        errors.append({'path': rel, 'code': 'storage:scan_limit'})
                        return files, errors, directories
                    name = (Path(rel)/entry.name).as_posix()
                    try:
                        checked = relative_path(root, name)
                        if is_link(checked):
                            raise ValueError()
                        # Windows DirEntry stat can omit inode and hardlink count.
                        info = checked.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(name)
                        elif stat.S_ISREG(info.st_mode):
                            files.append({'path': name, 'bytes': info.st_size, 'identity': identity(info), 'links': info.st_nlink})
                        else:
                            raise ValueError()
                    except (OSError, ValueError, ProjectError):
                        errors.append({'path': name, 'code': 'storage:unsafe_or_unreadable'})
        except FileNotFoundError:
            errors.append({'path': rel, 'code': 'storage:directory_missing'})
        except (OSError, ValueError, ProjectError):
            errors.append({'path': rel, 'code': 'storage:unsafe_or_unreadable'})
    return sorted(files, key=lambda row: row['path']), errors, directories


def verify(root, row, directories):
    path = relative_path(root, row['path'])
    for rel, expected in directories.items():
        if rel == '' or row['path'].startswith(rel + '/'):
            parent = relative_path(root, rel) if rel else root
            if is_link(parent) or identity(parent.stat())[:2] != expected:
                raise ServiceError(409, 'cleanup:preview_stale')
    if is_link(path) or identity(path.stat()) != row['identity']:
        raise ServiceError(409, 'cleanup:preview_stale')
    return path


def unlink_verified(root, row, directories):
    path = verify(root, row, directories)
    if os.name != 'nt':
        # On non-Windows hosts the process project lease serializes application I/O.
        # Never follow a final symlink, and recheck the named inode before unlink.
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if identity(os.fstat(fd)) != row['identity']:
                raise ServiceError(409, 'cleanup:preview_stale')
            verify_image(fd, row)
            verify(root, row, directories).unlink()
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
    # A read/delete handle denies concurrent write/delete opens, and opens reparse
    # points themselves. Delete the verified handle, never re-resolve the path.
    handle = create(str(path), 0x80000000 | 0x10000, 1, None, 3, 0x00200000, None)
    if handle == W.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        if identity(os.fstat(fd)) != row['identity']:
            raise ServiceError(409, 'cleanup:preview_stale')
        verify_image(fd, row)
        final = kernel.GetFinalPathNameByHandleW
        final.argtypes, final.restype = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD], W.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = final(handle, buffer, len(buffer), 0)
        if not size or size >= len(buffer) or os.path.normcase(buffer.value.removeprefix('\\\\?\\')) != os.path.normcase(str(path)):
            raise ServiceError(409, 'cleanup:preview_stale')
        verify(root, row, directories)
        remove = kernel.SetFileInformationByHandle
        remove.argtypes, remove.restype = [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD], W.BOOL
        disposition = W.BOOL(True)
        if not remove(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fd is not None:
            os.close(fd)
        else:
            kernel.CloseHandle.argtypes = [W.HANDLE]
            kernel.CloseHandle(handle)


def verify_image(fd, row):
    if not row.get('image'):
        return
    checksum = hashlib.sha256()
    while chunk := os.read(fd, 1024*1024):
        checksum.update(chunk)
    if row['bytes'] != row['image']['file_size'] or checksum.hexdigest() != row['image']['sha256']:
        raise ServiceError(409, 'cleanup:preview_stale')
