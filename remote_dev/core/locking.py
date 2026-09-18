"""Cross-thread and cross-process locks for package-owned mutable records."""
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


@contextlib.contextmanager
def record_lock(path: Path):
    started = time.monotonic()
    key = os.path.normcase(str(path.resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    while not lock.acquire(timeout=0.05):
        event = current_event()
        if event is not None and event.is_set():
            raise RemoteExecutionError("request cancelled before acquiring record lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                # LK_LOCK has a fixed retry limit. An explicit nonblocking
                # loop permits a long poll without losing the lock contract.
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        event = current_event()
                        if event is not None and event.is_set():
                            raise RemoteExecutionError("request cancelled before acquiring record lock")
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
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        event = current_event()
                        if event is not None and event.is_set():
                            raise RemoteExecutionError("request cancelled before acquiring record lock")
                        time.sleep(0.02)
                try:
                    get_recorder("remote-dev").event("DEBUG", "lock.acquired", elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

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
