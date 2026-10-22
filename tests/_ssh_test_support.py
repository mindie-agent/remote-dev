"""Test-only SSH argv/subprocess adapters and PID helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import remote_dev.core.ssh_transport as ssh_transport
import remote_dev.core.local_process as local_process


def _rewrite_ssh_argv(args: object, fake_script: Path) -> object:
    """Keep composed ssh argv; run it through the fake as a Python process.

    Production still selects its supported SSH executable. Tests replace only
    argv[0] while ``ssh -G`` parser tests keep using that real OpenSSH binary.
    """
    if not isinstance(args, (list, tuple)) or not args:
        return args
    argv = [str(item) for item in args]
    if len(argv) >= 7 and Path(argv[2]).name == "_posix_owner.py":
        return [*argv[:6], *_rewrite_ssh_argv(argv[6:], fake_script)]
    if Path(argv[0]).name.lower() not in {"ssh", "ssh.exe"}:
        return args
    return [sys.executable, str(fake_script), *argv[1:]]


class _ComposedSshSubprocess:
    """Test-only argv hook on ``ssh_transport.subprocess``, not a PATH wrapper."""

    def __init__(self, fake_script: Path) -> None:
        self._fake_script = Path(fake_script)

    def __getattr__(self, name: str):
        return getattr(subprocess, name)

    def Popen(self, args, **kwargs):  # noqa: N802 - match subprocess.Popen
        return subprocess.Popen(_rewrite_ssh_argv(args, self._fake_script), **kwargs)

    def run(self, args, **kwargs):
        return subprocess.run(_rewrite_ssh_argv(args, self._fake_script), **kwargs)


def _patch_composed_ssh(fake_script: Path):
    stack = ExitStack()
    adapter = _ComposedSshSubprocess(fake_script)
    stack.enter_context(mock.patch.object(ssh_transport, "subprocess", adapter))
    stack.enter_context(mock.patch.object(local_process, "subprocess", adapter))
    return stack


def _windows_pid_alive(pid: int) -> bool:
    """Owned-process liveness on native Windows (``os.kill(pid, 0)`` is not)."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    wait_timeout = 258
    wait_failed = 0xFFFFFFFF

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information | synchronize, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        if int(code.value) != still_active:
            return False
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited == wait_failed:
            return True
        return waited == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    """True while this PID is still a live process we can observe."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
