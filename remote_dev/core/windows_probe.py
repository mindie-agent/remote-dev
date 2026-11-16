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


__all__ = [
    "WindowsProcessApi",
    "windows_lock_free",
    "windows_pid_alive",
]
