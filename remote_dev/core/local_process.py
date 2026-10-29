"""
[Role] Owned local subprocess execution with descendant process-group / Windows Job Object cleanup (`local_process.py`).
[Invariants]
- POSIX children launch under `_posix_owner.py` in a dedicated session (`start_new_session=True`) with a parent-death pipe guardian; Windows children launch suspended and bind to a `KILL_ON_JOB_CLOSE` Job Object before resuming.
- `stop()` terminates the entire descendant group and is idempotent after completion.
- Memory enforcement is separate: a configured Windows Job or joined cgroup v2 is `hard-limit`; all other cases explicitly remain `observed-only`.
[Timeouts & Locks]
- Bounded `stop(timeout=5.0)` wait before escalating to `SIGKILL` / `TerminateJobObject`.
[Side-Effect Policy]
- Spawns and signals only its owned local child process group / Job Object and closes inherited stdio pipes on exit.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import RemoteExecutionError, record_cleanup_failure
from .cgroup_memory import MemoryCgroup, memory_limit_report


JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class OwnedProcess:
    """A local subprocess whose inherited child group ends with its owner.

    ``env`` overlays the inherited environment. Stdio remains byte-oriented
    unless a caller explicitly requests a text adapter. ``stop`` is idempotent
    after success and includes descendants after the direct child has exited.
    """

    def __init__(self, argv: Sequence[str], *, cwd=None,
                 env: Mapping[str, str] | None = None,
                 max_memory_mb: int | None = None, **stdio: Any) -> None:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("local command requires a non-empty argv array")
        if max_memory_mb is not None and (type(max_memory_mb) is not int or max_memory_mb <= 0):
            raise ValueError("max_memory_mb must be a positive integer")
        forbidden = {"shell", "start_new_session", "creationflags", "preexec_fn"} & stdio.keys()
        if forbidden:
            raise ValueError("process ownership controls " + ", ".join(sorted(forbidden)))
        self._closed = False
        self._owner_fd = None
        self._memory_cgroup = None
        self.max_memory_mb = max_memory_mb
        self._job = _WindowsJob(max_memory_mb=max_memory_mb) if os.name == "nt" else None
        self.memory_limit = memory_limit_report(max_memory_mb, reason="no memory limit requested")
        options = dict(stdio, cwd=cwd, env=None if env is None else {**os.environ, **env})
        if self._job is None:
            options["start_new_session"] = True
        else:
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        try:
            if self._job is None:
                if max_memory_mb is not None:
                    self._memory_cgroup, reason = MemoryCgroup.create(max_memory_mb)
                    self.memory_limit = memory_limit_report(max_memory_mb, reason=reason)
                self._spawn_posix(list(argv), options)
                if self._memory_cgroup is not None:
                    self.memory_limit = memory_limit_report(max_memory_mb, mechanism="cgroup-v2-memory.max")
            else:
                self.process = subprocess.Popen(list(argv), **options)
                self._job.assign_and_resume(self.process)
                self.memory_limit = self._job.memory_limit
        except BaseException as primary:
            process = getattr(self, "process", None)
            try:
                if self._job is not None:
                    self._job.close()
                    if process is not None:
                        process.kill()  # Assignment may fail before job ownership.
                        process.wait(timeout=5)
                elif process is not None:
                    self.stop()
                elif self._memory_cgroup is not None:
                    self._memory_cgroup.close()
            except Exception as cleanup_error:
                record_cleanup_failure(primary, cleanup_error)
            finally:
                try:
                    self._close_owner_pipe()
                except Exception as cleanup_error:
                    record_cleanup_failure(primary, cleanup_error)
                if process is not None:
                    for name in ("stdin", "stdout", "stderr"):
                        stream = getattr(process, name, None)
                        if stream is not None:
                            try:
                                stream.close()
                            except Exception as cleanup_error:
                                record_cleanup_failure(primary, cleanup_error)
            raise

    @property
    def memory_limit_mode(self) -> str:
        return self.memory_limit["mode"]

    def _spawn_posix(self, argv, options):
        """Do not release target code until its caller-death guardian exists."""
        owner_read, owner_write = os.pipe()
        self._owner_fd = owner_write
        try:
            started, notify = os.pipe()
        except BaseException:
            os.close(owner_read)
            raise
        try:
            inherited = options.pop("pass_fds", ())
            ownership = (list(inherited) if self._memory_cgroup is None else
                         {"pass_fds": list(inherited), "memory_cgroup": str(self._memory_cgroup.path)})
            self.process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("_posix_owner.py")),
                 str(owner_read), str(notify), json.dumps(ownership), *argv],
                pass_fds=(*inherited, owner_read, notify), **options)
        finally:
            os.close(owner_read)
            os.close(notify)
            if not hasattr(self, "process"):
                os.close(started)
        raw = bytearray()
        deadline = time.monotonic() + 5.0
        try:
            while len(raw) <= 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([started], [], [], remaining)[0]:
                    raise TimeoutError("owned process startup receipt timed out")
                chunk = os.read(started, 1025 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            os.close(started)
        if len(raw) > 1024:
            raise RemoteExecutionError("owned process startup receipt exceeds its bound",
                                       category="process_start", submission_state="uncertain")
        try:
            receipt = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise RemoteExecutionError("owned process could not confirm target startup",
                                       category="process_start", submission_state="uncertain") from exc
        expected = {"started": True}
        if self._memory_cgroup is not None:
            expected["memory_cgroup"] = str(self._memory_cgroup.path)
        if receipt != expected:
            if isinstance(receipt, dict) and receipt.get("memory_error"):
                raise RuntimeError("owned process memory enforcement failed: " + receipt["memory_error"])
            number = receipt.get("errno") if isinstance(receipt, dict) else None
            if isinstance(receipt, dict) and receipt.get("started") is False and type(number) is int:
                raise OSError(number, os.strerror(number), os.fspath(argv[0]))
            raise RemoteExecutionError("owned process has no valid startup receipt",
                                       category="process_start", submission_state="uncertain")

    def _close_owner_pipe(self):
        descriptor = getattr(self, "_owner_fd", None)
        if descriptor is not None:
            self._owner_fd = None
            os.close(descriptor)

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
            # Darwin filters zombies from killpg and returns EPERM when only
            # zombie members remain in our spawned group.
            if sys.platform != "darwin" or exc.errno != errno.EPERM:
                raise

    def stop(self, *, force: bool = True, timeout: float = 5.0) -> int | None:
        if self._closed:
            self._close_memory_cgroup(timeout)
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
            try:
                self._signal_group(signal.SIGKILL if force else signal.SIGTERM)
                if not force:
                    try:
                        self.process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        pass
                    # The parent may already have exited while a child ignores TERM.
                    self._signal_group(signal.SIGKILL)
                result = self.process.wait(timeout=timeout)
            finally:
                primary = sys.exc_info()[1]
                try:
                    self._close_owner_pipe()
                except Exception as cleanup_error:
                    if primary is None:
                        raise
                    record_cleanup_failure(primary, cleanup_error)
        self._closed = True
        self._close_memory_cgroup(timeout)
        return result

    def _close_memory_cgroup(self, timeout):
        cgroup = getattr(self, "_memory_cgroup", None)
        if cgroup is None:
            return
        try:
            cgroup.close(timeout=timeout)
        except Exception as exc:
            # A cleanup failure must not erase the target's confirmed exit
            # status. Keep both facts inspectable and permit cleanup retries.
            self.memory_limit["cleanup_error"] = str(exc)
            self.memory_limit["exit_code"] = self.process.returncode
            raise
        self._memory_cgroup = None
        self.memory_limit.pop("cleanup_error", None)

    def __enter__(self) -> OwnedProcess:
        return self

    def __exit__(self, _type, error, _traceback) -> None:
        try:
            self.stop()
        except Exception as cleanup_error:
            if error is None:
                raise
            record_cleanup_failure(error, cleanup_error)


class _WindowsJob:
    """Win32 declarations stay lazy so POSIX has no Windows dependency."""

    def __init__(self, *, max_memory_mb: int | None = None) -> None:
        if max_memory_mb is not None and (type(max_memory_mb) is not int or max_memory_mb <= 0):
            raise ValueError("max_memory_mb must be a positive integer")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.max_memory_mb = max_memory_mb
        self.memory_limit = memory_limit_report(max_memory_mb, reason="no memory limit requested")

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

        self.Accounting = Accounting
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
        self.close_handle = api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
        self.handle = self.check(create(None, None))
        try:
            value = ExtendedLimits()
            flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if max_memory_mb is not None:
                flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
                value.job_memory = int(max_memory_mb) * 1024 * 1024
            value.basic.flags = flags
            self.check(limits(self.handle, 9, ctypes.byref(value), ctypes.sizeof(value)))
            if max_memory_mb is not None:
                self.memory_limit = memory_limit_report(max_memory_mb, mechanism="windows-job-memory")
        except BaseException:
            self.close()
            raise

    def check(self, value):
        if not value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return value

    def assign_and_resume(self, process) -> None:
        self.check(self.assign(self.handle, int(process._handle)))
        _resume_windows_process(process)

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


def _resume_windows_process(process):
    """Resume only the owned, suspended child after Job assignment.

    NtResumeProcess is also used by psutil's Windows resume implementation.
    The retained Popen handle avoids PID reuse and system-wide thread scans.
    NTSTATUS is converted explicitly; GetLastError is not its error channel.
    """
    import ctypes
    from ctypes import wintypes

    native = ctypes.WinDLL("ntdll")
    resume = native.NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = wintypes.LONG
    status = resume(int(process._handle))
    if status < 0:
        convert = native.RtlNtStatusToDosError
        convert.argtypes = [wintypes.LONG]
        convert.restype = wintypes.ULONG
        raise ctypes.WinError(convert(status))
