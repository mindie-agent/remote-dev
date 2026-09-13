"""Owned local argv execution for SSH clients on Windows and POSIX.

Paths and argv belong to the executing OS; this component does not interpret a
shell or translate Windows/WSL paths. Windows owns descendants with a Job Object
assigned before the suspended child can run. POSIX owns an independent process
group. Deliberately detached POSIX sessions and remote processes are outside
that local group: remote quiet still requires a remote supervisor receipt.
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any


class OwnedProcess:
    """A local subprocess whose inherited child group ends with its owner.

    ``env`` overlays the inherited environment. Stdio remains byte-oriented
    unless a caller explicitly requests a text adapter. ``stop`` is idempotent
    after success and includes descendants after the direct child has exited.
    """

    def __init__(self, argv: Sequence[str], *, cwd=None,
                 env: Mapping[str, str] | None = None, **stdio: Any) -> None:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("local command requires a non-empty argv array")
        forbidden = {"shell", "start_new_session", "creationflags", "preexec_fn"} & stdio.keys()
        if forbidden:
            raise ValueError("process ownership controls " + ", ".join(sorted(forbidden)))
        self._closed = False
        self._job = _WindowsJob() if os.name == "nt" else None
        options = dict(stdio, cwd=cwd, env=None if env is None else {**os.environ, **env})
        if self._job is None:
            options["start_new_session"] = True
        else:
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        try:
            self.process = subprocess.Popen(list(argv), **options)
            if self._job is not None:
                self._job.assign_and_resume(self.process.pid)
        except BaseException:
            if self._job is not None:
                self._job.close()
            process = getattr(self, "process", None)
            if process is not None:
                process.kill()  # Assignment may fail while the child is suspended.
                process.wait(timeout=5)
            raise

    def _signal_group(self, sig: int) -> None:
        if self.process.returncode is not None:
            # Popen has already reaped our leader. A PID visible now belongs to
            # another process; a late forward.close() must not signal its group.
            # Original surviving descendants retain the PGID with no leader PID.
            try:
                os.getpgid(self.process.pid)
            except ProcessLookupError:
                pass
            else:
                return
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            # Darwin filters zombies from killpg and may return EPERM when no
            # live member remains after TERM. A real permission failure still
            # matters: accept only an independently observed exited group.
            if (sys.platform != "darwin" or exc.errno != errno.EPERM
                    or not self._darwin_group_exited()):
                raise

    def _darwin_group_exited(self) -> bool:
        try:
            result = subprocess.run(
                ["/bin/ps", "-x", "-g", str(self.process.pid), "-o", "pgid=,stat="],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "COMMAND_MODE": "unix2003"},
                text=True, encoding="utf-8", check=False, timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return False
        # Apple ps exits 1 for an empty selection, but some sysctl failures
        # exit 0 with stderr. Neither an error nor a partial listing proves exit.
        if result.stderr or len(result.stdout) > 65536 or result.returncode not in (0, 1):
            return False
        if result.returncode == 1:
            return not result.stdout.strip()
        if not result.stdout.strip():
            return False
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdecimal():
                return False
            if int(fields[0]) != self.process.pid or not fields[1].startswith("Z"):
                return False
        return True

    def stop(self, *, force: bool = True, timeout: float = 5.0) -> int | None:
        if self._closed:
            return self.process.returncode
        if self._job is not None:
            try:
                self._job.stop(timeout)
                result = self.process.wait(timeout=timeout)
            finally:
                # KILL_ON_JOB_CLOSE remains effective even if observation or
                # wait fails. Do not leak the handle or reuse a closed job while
                # unwinding an exception. The original failure still propagates.
                self._job.close()
                self._closed = True
        else:
            # Never use parent liveness as proof that its group is empty.
            # A proxy child can outlive SSH while still holding an output pipe.
            self._signal_group(signal.SIGKILL if force else signal.SIGTERM)
            if not force:
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
                # The parent may already have exited while a child ignores TERM.
                self._signal_group(signal.SIGKILL)
            result = self.process.wait(timeout=timeout)
        self._closed = True
        return result

    def __enter__(self) -> OwnedProcess:
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()


class _WindowsJob:
    """Win32 declarations stay lazy so POSIX has no Windows dependency."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
                        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
                        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]

        self.Accounting, self.ThreadEntry = Accounting, ThreadEntry
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def api(name, restype, *argtypes):
            function = getattr(kernel, name)
            function.restype, function.argtypes = restype, argtypes
            return function

        create = api("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
        limits = api("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                     ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.assign = api("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
        self.terminate = api("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
        self.query = api("QueryInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                         ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.open_process = api("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.snapshot = api("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
        self.first_thread = api("Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.next_thread = api("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
        self.open_thread = api("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.resume_thread = api("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
        self.close_handle = api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
        self.handle = self.check(create(None, None))
        try:
            value = ExtendedLimits()
            value.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.check(limits(self.handle, 9, ctypes.byref(value), ctypes.sizeof(value)))
        except BaseException:
            self.close()
            raise

    def check(self, value):
        if not value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return value

    def assign_and_resume(self, pid: int) -> None:
        process = self.check(self.open_process(0x0101, False, pid))  # SET_QUOTA | TERMINATE
        try:
            self.check(self.assign(self.handle, process))
        finally:
            self.close_handle(process)
        snapshot = self.snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            row = self.ThreadEntry()
            row.size = self.ctypes.sizeof(row)
            more = self.first_thread(snapshot, self.ctypes.byref(row))
            while more and row.owner_pid != pid:
                more = self.next_thread(snapshot, self.ctypes.byref(row))
            if not more:
                raise RuntimeError("cannot find the owned suspended process's primary thread")
            thread = self.check(self.open_thread(0x0002, False, row.thread_id))
            try:
                if self.resume_thread(thread) == 0xFFFFFFFF:
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
            finally:
                self.close_handle(thread)
        finally:
            self.close_handle(snapshot)

    def stop(self, timeout: float) -> None:
        self.check(self.terminate(self.handle, 1))
        deadline = time.monotonic() + timeout
        while True:
            accounting = self.Accounting()
            self.check(self.query(self.handle, 1, self.ctypes.byref(accounting),
                                  self.ctypes.sizeof(accounting), None))
            if accounting.active_processes == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("owned local process job did not become empty")
            time.sleep(0.01)

    def close(self) -> None:
        if self.handle is not None:
            self.close_handle(self.handle)
            self.handle = None
