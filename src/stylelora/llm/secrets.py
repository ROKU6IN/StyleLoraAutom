"""CurrentUser DPAPI only; encrypted files are created with a protected user DACL."""
import base64
import ctypes
from ctypes import wintypes as W
import json
import hmac
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from ..exchange.common import is_link
from ..runtime.contracts import strict_json
from .types import LlmFailure


def safe_path(path):
    path = Path(path).absolute()
    if any(is_link(p) for p in (path, *path.parents)):
        raise LlmFailure('unsafe_storage')
    return path


def dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise LlmFailure('secret_store_unavailable')
    from ..runtime.instance import _windows
    kernel, _ = _windows()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)

    class Blob(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('data', ctypes.c_void_p)]

    buffer = ctypes.create_string_buffer(data)
    source, target = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p)), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, W.DWORD, ctypes.POINTER(Blob)]
    fn.restype = W.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN only, never CRYPTPROTECT_LOCAL_MACHINE.
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise LlmFailure('secret_store_unavailable')
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel.LocalFree(target.data)


def private_write(path, data):
    from ..runtime.instance import _windows, UserSecurity, user_sid
    import msvcrt
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kernel, _ = _windows()
    security = UserSecurity(user_sid())
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        handle = kernel.CreateFileW(str(temporary), 0x40000000, 0, ctypes.byref(security.attributes), 1, 0x80, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise LlmFailure('secret_store_unavailable')
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        security.close()
        temporary.unlink(missing_ok=True)


class SecretStore:
    def __init__(self, directory=None):
        self.path = safe_path(Path(directory) / 'secrets.json') if directory is not None else None
        self.lock = threading.RLock()
        self.entries = {}
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError()
                data = strict_json(self.path.read_bytes())
                if set(data) != {'format', 'entries'} or data['format'] != 1 or not isinstance(data['entries'], dict):
                    raise ValueError()
                for ref, value in data['entries'].items():
                    self.env_name(ref)
                    base64.b64decode(value, validate=True)
                self.entries = data['entries']
            except Exception:
                raise LlmFailure('secret_store_unavailable') from None

    @staticmethod
    def env_name(ref):
        if not re.fullmatch(r'[a-z0-9-]{1,80}', ref):
            raise LlmFailure('invalid_token_ref')
        return 'STYLELORA_LLM_TOKEN_' + ref.upper().replace('-', '_')

    def read(self, ref):
        with self.lock:
            name = self.env_name(ref)
            if name in os.environ:
                value = os.environ[name]
            elif ref in self.entries:
                try:
                    value = dpapi(base64.b64decode(self.entries[ref], validate=True), decrypt=True).decode('utf-8')
                except Exception:
                    raise LlmFailure('secret_store_unavailable') from None
            else:
                return None
            return value if value and re.fullmatch(r'[\x21-\x7e]{1,8192}', value) else None

    def status(self, ref):
        with self.lock:
            origin = 'environment' if self.env_name(ref) in os.environ else 'stored' if ref in self.entries else None
            try:
                available = bool(self.read(ref))
            except LlmFailure:
                available = False
            return {'token_ref': ref, 'has_secret': ref in self.entries,
                    'effective_available': available, 'origin': origin}

    def put(self, ref, value):
        with self.lock:
            entries = dict(self.entries)
            if value is not None and ref in entries:
                try:
                    previous = dpapi(base64.b64decode(entries[ref]), decrypt=True)
                except Exception:
                    raise LlmFailure('secret_store_unavailable') from None
                if hmac.compare_digest(previous, value.encode()):
                    return False
            if value is None:
                entries.pop(ref, None)
            else:
                try:
                    entries[ref] = base64.b64encode(dpapi(value.encode())).decode('ascii')
                except Exception:
                    raise LlmFailure('secret_store_unavailable') from None
            if self.path:
                try:
                    private_write(self.path, (json.dumps({'format': 1, 'entries': entries}) + '\n').encode())
                except Exception:
                    try:
                        self.entries = strict_json(self.path.read_bytes())['entries']
                    except Exception:
                        pass
                    raise LlmFailure('secret_write_failed', outcome='unknown') from None
            self.entries = entries
            return True
