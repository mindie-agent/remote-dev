"""
[Role] Windows-only process-liveness and byte-lock probes used by local retention
(`windows_probe.py`).
[Invariants]
- PID liveness never calls `os.kill`: Windows `os.kill(pid, 0)` sends `CTRL_C_EVENT`
  and falls back to `TerminateProcess`, which can kill a live process.
- `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess` treats
  `ERROR_INVALID_PARAMETER` (87) as exited and `ERROR_ACCESS_DENIED` (5) or any
  unconfirmed result as alive/retained; `CloseHandle` always runs in `finally`.
- Lock probing uses the same one-byte `msvcrt.locking(LK_NBLCK)` region that
  `locking.record_lock` holds; an unverifiable probe means "locked/in use".
[Timeouts & Locks]
- Probes are one-shot and non-blocking; no wait loop is added here.
[Side-Effect Policy]
- Read-only kernel queries and a non-destructive one-byte lock probe; never signals,
  terminates, writes, or removes files.
"""
from __future__ import annotations

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = -1
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3


class WindowsProcessApi:
    """Minimal explicit-argtypes wrapper for the three Win32 calls we need."""

    def __init__(self):
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._ctypes = ctypes
        self._kernel32 = kernel32

    def open_process(self, pid):
        return self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )

    def last_error(self):
        return self._ctypes.get_last_error()

    def exit_code(self, handle):
        code = self._ctypes.c_uint32(0)
        if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(code)):
            return None
        return int(code.value)

    def close(self, handle):
        return self._kernel32.CloseHandle(handle)


def windows_pid_alive(pid, api=None):
    """Return False only when Win32 confirms the process has already exited."""
    api = api if api is not None else WindowsProcessApi()
    handle = api.open_process(pid)
    if not handle:
        # 87 = no such PID. 5 = the process exists but not its liveness; any
        # other failure is unconfirmed, so all non-87 results retain it.
        error = api.last_error()
        if error == ERROR_INVALID_PARAMETER:
            return False
        if error == ERROR_ACCESS_DENIED:
            return True
        return True
    try:
        code = api.exit_code(handle)
        if code is None:
            return True
        return code == STILL_ACTIVE
    finally:
        try:
            api.close(handle)
        except TimeoutError:
            raise
        except OSError:
            pass


def windows_lock_free(lock_path, *, msvcrt_module=None):
    """Non-destructive MSVCRT byte-lock probe matching `record_lock`."""
    if msvcrt_module is None:
        try:
            import msvcrt as msvcrt_module
        except ImportError:  # pragma: no cover - defensive
            return False
    try:
        handle = lock_path.open("r+b")
    except FileNotFoundError:
        return True
    except TimeoutError:
        raise
    except OSError:
        return False
    try:
        try:
            handle.seek(0)
            msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_NBLCK, 1)
        except TimeoutError:
            raise
        except OSError:
            return False  # held or unverifiable: conservatively in use
        try:
            handle.seek(0)
            msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_UNLCK, 1)
        except TimeoutError:
            raise
        except OSError:
            return False
        return True
    finally:
        handle.close()


class WindowsFileShareApi:
    """Explicit-argtypes Win32 CreateFileW(dwShareMode=0) open-handle probe."""

    def __init__(self):
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._invalid = ctypes.c_void_p(INVALID_HANDLE_VALUE).value

    def open_exclusive(self, path):
        handle = self._kernel32.CreateFileW(
            str(path),
            GENERIC_READ | GENERIC_WRITE,
            0,  # dwShareMode=0 fails if any live process holds the lock file open
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == 0 or handle == -1 or handle == self._invalid:
            return None
        return handle

    def last_error(self):
        return self._ctypes.get_last_error()

    def close(self, handle):
        return self._kernel32.CloseHandle(handle)


def windows_exclusive_open_free(lock_path, *, share_api=None):
    """Return True only when no process holds an open Win32 handle on `lock_path`."""
    try:
        api = share_api if share_api is not None else WindowsFileShareApi()
    except (OSError, AttributeError):
        return False
    handle = api.open_exclusive(lock_path)
    if handle is None:
        return api.last_error() in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND)
    try:
        return True
    finally:
        try:
            api.close(handle)
        except TimeoutError:
            raise
        except OSError:
            pass


def windows_session_lock_free(lock_path, *, msvcrt_module=None, share_api=None):
    """Verify both exclusive-handle freedom and MSVCRT byte-0 lock freedom on Windows."""
    import os

    if share_api is not None or os.name == "nt":
        if not windows_exclusive_open_free(lock_path, share_api=share_api):
            return False
    return windows_lock_free(lock_path, msvcrt_module=msvcrt_module)


__all__ = [
    "WindowsFileShareApi",
    "WindowsProcessApi",
    "windows_exclusive_open_free",
    "windows_lock_free",
    "windows_pid_alive",
    "windows_session_lock_free",
]
