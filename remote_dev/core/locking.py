"""
[Role] Cross-thread and cross-process file locks for local records and endpoint mutations (`locking.py`).
[Invariants]
- Combines an in-process `threading.RLock` with an OS file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) so threads and processes share one serialization boundary.
- On POSIX, after `flock` the handle must still name the live `.lock` directory entry (`st_dev`/`st_ino`
  match and `st_nlink > 0`); a lock pruned between `open()` and `flock()` is re-opened instead of
  granting the critical section under an orphaned inode.
[Timeouts & Locks]
- Polls OS locks every 20ms, bounds thread acquisition by the remaining caller deadline, and checks cancellation before admission. Timeout/cancellation never enters the protected mutation.
[Side-Effect Policy]
- Creates `.lock` files under `state_root() / "locks"` or adjacent to target record paths.
"""
from __future__ import annotations

import contextlib
import hashlib
import functools
import os
import threading
import time
from mindie_diagnostics import get_recorder
import weakref
from .cancellation import current_event
from .errors import RemoteExecutionError
from pathlib import Path

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()


def _lock_handle_is_current(handle, lock_path: Path) -> bool:
    """True only while `handle` still names the live `lock_path` directory entry.

    A retention reaper may unlink a stale lock inode between `open()` and
    `flock()`; the following `flock()` would then succeed on an unreachable
    inode. Comparing device/inode (and `st_nlink`) after `flock` closes that
    window: the reaper takes the same `flock` before unlinking.
    """
    try:
        current = os.lstat(lock_path)
    except FileNotFoundError:
        return False
    own = os.fstat(handle.fileno())
    return own.st_nlink > 0 and (own.st_dev, own.st_ino) == (current.st_dev, current.st_ino)


def try_record_thread_lock(path: Path):
    """Acquire only the in-process half of `record_lock`, never blocking.

    Local retention uses this to prove no same-process `record_lock` owner
    exists before it takes the OS lock. A `None` return means "busy, skip".
    """
    key = os.path.normcase(str(Path(path).resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        return None
    return lock


@contextlib.contextmanager
def record_lock(path: Path, *, timeout_seconds: float | None = None):
    started = time.monotonic()
    deadline = None if timeout_seconds is None else started + timeout_seconds
    def waiting():
        event = current_event()
        if event is not None and event.is_set():
            raise RemoteExecutionError("request cancelled before acquiring record lock")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for record lock")
    key = os.path.normcase(str(path.resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    while True:
        waiting()
        remaining = 0.05 if deadline is None else max(0.0, min(0.05, deadline - time.monotonic()))
        if lock.acquire(timeout=remaining):
            break
    try:
        waiting()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                # LK_LOCK has a fixed retry limit. An explicit nonblocking
                # loop permits a long poll without losing the lock contract.
                while True:
                    waiting()
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except TimeoutError:
                        raise
                    except OSError:
                        waiting()
                        time.sleep(0.02)
                try:
                    get_recorder("remote-dev").event("DEBUG", "lock.acquired", elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                while True:
                    waiting()
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        waiting()
                        time.sleep(0.02)
                        continue
                    if _lock_handle_is_current(handle, lock_path):
                        break
                    # A stale-lock reaper unlinked this inode while we waited.
                    # Re-open the live directory entry; never enter the critical
                    # section holding only an orphaned lock.
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    handle = lock_path.open("a+b")
                try:
                    get_recorder("remote-dev").event("DEBUG", "lock.acquired", elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    finally:
        lock.release()


def mutation_lock(endpoint):
    from .container_endpoint import pin_container_endpoint
    from .state_store import state_root
    endpoint = pin_container_endpoint(endpoint)
    coordinate = f"{endpoint.user}@{endpoint.host}:{endpoint.port}"
    if endpoint.container:
        coordinate += "|container=" + endpoint.container
    key = hashlib.sha256(coordinate.encode()).hexdigest()
    return record_lock(state_root() / "locks" / key)


def serialize_mutation(function):
    @functools.wraps(function)
    def invoke(endpoint, *args, **kwargs):
        from .container_endpoint import pin_container_endpoint
        endpoint = pin_container_endpoint(endpoint, timeout_ms=kwargs.get("timeout_ms"))
        with mutation_lock(endpoint):
            return function(endpoint, *args, **kwargs)
    return invoke


def path_lock(path):
    from .state_store import state_root
    key = hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()
    return record_lock(state_root() / "locks" / ("path-" + key))
