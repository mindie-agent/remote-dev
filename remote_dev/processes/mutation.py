"""
[Role] Remote host mutation file lock shared by `rpc_worker.py` and `artifact_worker.py` (`processes/mutation.py`).
[Invariants]
- Serializes concurrent remote workspace mutations across independent SSH worker processes via `~/.cache/remote-dev/mutation.lock`.
[Timeouts & Locks]
- Polls non-blocking `fcntl.flock` / `msvcrt.locking` every `20ms` (`0.02s`), aborting immediately if the request cancellation event is set.
[Side-Effect Policy]
- Creates and locks `~/.cache/remote-dev/mutation.lock` (`0o700` parent directory) on the remote host.
"""
import contextlib


@contextlib.contextmanager
def remote_mutation_lock(cancelled=None):
    import os
    from pathlib import Path
    import time
    directory = Path.home() / ".cache" / "remote-dev"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "mutation.lock").open("a+b") as lock:
        if os.name == "nt":  # Allows protocol tests on a Windows client.
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            def acquire():
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(lock, fcntl.LOCK_UN)
        while True:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("mutation cancelled while waiting for lock; not executed")
            try:
                acquire()
                break
            except TimeoutError:
                raise
            except (BlockingIOError, OSError):
                time.sleep(0.02)
        try:
            yield
        finally:
            release()
