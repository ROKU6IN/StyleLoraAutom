"""Windows current-user mutex and bounded, current-SID-only local control pipe.

The pipe only supplies session credentials and explicit CLI path grants. HTTP
never accepts arbitrary filesystem paths. No credential is written to disk.
"""
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import os
import threading
import time

from .contracts import strict_json
from .errors import ServiceError, safe_error

MAX_MESSAGE = 65536
_held, _gate = set(), threading.Lock()


def _windows():
    if os.name != "nt":
        raise ServiceError(501, "instance:windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    signatures = {
        "GetCurrentProcess": ([], W.HANDLE), "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL), "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
        "CreateMutexW": ([ctypes.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD), "ReleaseMutex": ([W.HANDLE], W.BOOL),
        "CreateNamedPipeW": ([W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, ctypes.c_void_p], W.HANDLE),
        "ConnectNamedPipe": ([W.HANDLE, ctypes.c_void_p], W.BOOL), "DisconnectNamedPipe": ([W.HANDLE], W.BOOL),
        "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        "ReadFile": ([W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p], W.BOOL),
        "WriteFile": ([W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p], W.BOOL),
        "GetNamedPipeClientProcessId": ([W.HANDLE, ctypes.POINTER(W.ULONG)], W.BOOL),
        "GetNamedPipeServerProcessId": ([W.HANDLE, ctypes.POINTER(W.ULONG)], W.BOOL),
        "WaitNamedPipeW": ([W.LPCWSTR, W.DWORD], W.BOOL),
        "SetNamedPipeHandleState": ([W.HANDLE, ctypes.POINTER(W.DWORD), ctypes.c_void_p, ctypes.c_void_p], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
    advapi.OpenProcessToken.restype = W.BOOL
    advapi.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    advapi.GetTokenInformation.restype = W.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL
    return kernel, advapi


def user_sid(pid=None):
    kernel, advapi = _windows()
    process = kernel.OpenProcess(0x1000, False, pid) if pid is not None else kernel.GetCurrentProcess()
    if not process:
        raise ServiceError(403, "instance:identity_unavailable")
    token, sid_text = W.HANDLE(), W.LPWSTR()
    try:
        if not advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise ServiceError(403, "instance:identity_unavailable")
        required = W.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(required)):
            raise ServiceError(403, "instance:identity_unavailable")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ServiceError(403, "instance:identity_unavailable")
        return sid_text.value
    finally:
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)
        if pid is not None:
            kernel.CloseHandle(process)


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", W.BOOL)]


class UserSecurity:
    def __init__(self, sid):
        self.kernel, advapi = _windows()
        self.descriptor = ctypes.c_void_p()
        # Protected DACL: only the current user, no inherited broad permissions.
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;GA;;;{sid})", 1, ctypes.byref(self.descriptor), None):
            raise OSError("Could not construct current-user security descriptor")
        self.attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), self.descriptor, False)

    def close(self):
        if self.descriptor:
            self.kernel.LocalFree(self.descriptor)
            self.descriptor = None


def instance_names():
    sid = user_sid()
    identity = hashlib.sha256(sid.encode()).hexdigest()[:24]
    return sid, "Local\\StyleLoraAutom-" + identity, "\\\\.\\pipe\\StyleLoraAutom-" + identity


class ProductLock:
    def __init__(self):
        sid, name, _ = instance_names()
        self.kernel, _ = _windows()
        self.handle, self.name, self.abandoned = None, name, False
        security = UserSecurity(sid)
        try:
            with _gate:
                if name in _held:
                    raise ServiceError(423, "instance:already_running")
                handle = self.kernel.CreateMutexW(ctypes.byref(security.attributes), False, name)
                if not handle:
                    raise ServiceError(423, "instance:unavailable")
                result = self.kernel.WaitForSingleObject(handle, 0)
                if result not in (0, 0x80):
                    self.kernel.CloseHandle(handle)
                    raise ServiceError(423, "instance:already_running")
                self.handle, self.abandoned = handle, result == 0x80
                _held.add(name)
        finally:
            security.close()

    def close(self):
        with _gate:
            if self.handle:
                self.kernel.ReleaseMutex(self.handle)
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                _held.discard(self.name)


def _write(kernel, handle, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ServiceError(413, "instance:message_too_large")
    written = W.DWORD()
    if not kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None) or written.value != len(data):
        raise OSError("Local pipe write failed")


def _read(kernel, handle):
    buffer, count = ctypes.create_string_buffer(MAX_MESSAGE), W.DWORD()
    if kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
        if not count.value:
            raise OSError("Local pipe disconnected")
        return strict_json(buffer.raw[:count.value])
    code = ctypes.get_last_error()
    if code in (232, 536):
        return None
    raise OSError("Local pipe read failed")


class PipeServer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sid, _, self.name = instance_names()
        self.kernel, _ = _windows()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.failure = None
        self.thread = threading.Thread(target=self._run, name="stylelora-control-pipe", daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.failure:
            self.stop.set()
            self.thread.join(timeout=5)
            raise ServiceError(503, "instance:pipe_unavailable")

    def _dispatch(self, value):
        if not isinstance(value, dict):
            raise ServiceError(422, "input:invalid")
        action = value.get("action")
        if action == "hello" and set(value) == {"action"}:
            return {"token": self.runtime.token, "authority": self.runtime.authority, "instance_id": self.runtime.instance_id, "pid": os.getpid(), "protocol_version": 1}
        if action == "bootstrap" and set(value) <= {"action", "instance_id"}:
            if "instance_id" in value and value["instance_id"] != self.runtime.instance_id:
                raise ServiceError(409, "instance:changed")
            return {"url": self.runtime.issue_bootstrap()}
        if action == "grant" and set(value) <= {"action", "path", "purpose", "project_id", "instance_id"}:
            if "instance_id" in value and value["instance_id"] != self.runtime.instance_id:
                raise ServiceError(409, "instance:changed")
            return self.runtime.grant(value["path"], value["purpose"], value.get("project_id"))
        raise ServiceError(422, "instance:invalid_action")

    def _run(self):
        security = UserSecurity(self.sid)
        invalid = ctypes.c_void_p(-1).value
        try:
            while not self.stop.is_set():
                handle = self.kernel.CreateNamedPipeW(self.name, 3 | 0x80000, 4 | 2 | 1 | 8, 1, MAX_MESSAGE, MAX_MESSAGE, 5000, ctypes.byref(security.attributes))
                if handle == invalid:
                    raise OSError("Cannot create local control pipe")
                self.ready.set()
                try:
                    while not self.stop.is_set():
                        if self.kernel.ConnectNamedPipe(handle, None) or ctypes.get_last_error() == 535:
                            break
                        if ctypes.get_last_error() not in (536, 232):
                            raise OSError("Cannot connect local control pipe")
                        self.stop.wait(0.01)
                    if self.stop.is_set():
                        break
                    pid = W.ULONG()
                    if not self.kernel.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)) or user_sid(pid.value) != self.sid:
                        continue
                    deadline, value = time.monotonic()+5, None
                    while not self.stop.is_set() and time.monotonic() < deadline and value is None:
                        value = _read(self.kernel, handle)
                        if value is None:
                            self.stop.wait(0.01)
                    if value is None:
                        continue
                    try:
                        result = {"ok": self._dispatch(value)}
                    except Exception as exc:
                        error = safe_error(exc)
                        result = {"error": {"status": error.status, "code": error.code}}
                    _write(self.kernel, handle, result)
                    # Keep the pipe open until the client acknowledges consumption.
                    deadline = time.monotonic()+2
                    while not self.stop.is_set() and time.monotonic() < deadline:
                        if _read(self.kernel, handle) is not None:
                            break
                        self.stop.wait(0.01)
                except (OSError, ServiceError):
                    pass
                finally:
                    self.kernel.DisconnectNamedPipe(handle)
                    self.kernel.CloseHandle(handle)
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
        finally:
            security.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=7)


def pipe_request(value):
    from .standalone import session_control
    local = session_control(value)
    if local is not None:
        return local
    sid, _, name = instance_names()
    kernel, _ = _windows()
    deadline, handle = time.monotonic()+3, ctypes.c_void_p(-1).value
    # A consumed connection is briefly absent while the server creates the next
    # pipe instance. Retry only connection establishment, never a written RPC.
    while time.monotonic() < deadline:
        if kernel.WaitNamedPipeW(name, 200):
            handle = kernel.CreateFileW(name, 0x80000000 | 0x40000000, 0, None, 3, 0, None)
            if handle != ctypes.c_void_p(-1).value:
                break
        time.sleep(0.01)
    if handle == ctypes.c_void_p(-1).value:
        raise ServiceError(503, "instance:not_running")
    try:
        mode = W.DWORD(2 | 1)
        if not kernel.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
            raise ServiceError(503, "instance:disconnected")
        pid = W.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or user_sid(pid.value) != sid:
            raise ServiceError(403, "instance:identity_mismatch")
        _write(kernel, handle, value)
        deadline, result = time.monotonic()+6, None
        while time.monotonic() < deadline and result is None:
            result = _read(kernel, handle)
            if result is None:
                time.sleep(0.01)
        if result is None:
            raise ServiceError(503, "instance:empty_response", outcome="unknown")
        _write(kernel, handle, {"ack": True})
        if "error" in result:
            raise ServiceError(result["error"]["status"], result["error"]["code"])
        if value["action"] == "hello" and result["ok"]["pid"] != pid.value:
            raise ServiceError(403, "instance:identity_mismatch")
        return result["ok"]
    except OSError as exc:
        raise ServiceError(503, "instance:disconnected", outcome="unknown") from exc
    finally:
        kernel.CloseHandle(handle)
