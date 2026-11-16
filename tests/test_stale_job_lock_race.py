"""Deterministic stale `.json.lock` pruning races and POSIX inode revalidation."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from remote_dev.core import retention
from remote_dev.core.locking import record_lock

_REPO_ROOT = Path(__file__).resolve().parents[1]

_LOCKER_SCRIPT = r"""
import fcntl
import os
import sys
import time
from pathlib import Path

from remote_dev.core.locking import record_lock

record = Path(sys.argv[1])
ready = Path(sys.argv[2])
proceed = Path(sys.argv[3])
acquired = Path(sys.argv[4])
release = Path(sys.argv[5])

real_flock = fcntl.flock
gated = [True]


def flock(fd, operation):
    if gated[0]:
        gated[0] = False
        # The old lock inode is already open; pause before `flock()` so the
        # parent can unlink it exactly in the open->flock window.
        ready.write_text("opened", encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while not proceed.exists():
            if time.monotonic() >= deadline:
                raise SystemExit("timeout waiting for stale-lock prune")
            time.sleep(0.005)
    return real_flock(fd, operation)


fcntl.flock = flock
with record_lock(record):
    lock = record.with_name(record.name + ".lock")
    acquired.write_text(str(os.stat(lock).st_ino), encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not release.exists():
        if time.monotonic() >= deadline:
            raise SystemExit("timeout waiting for release")
        time.sleep(0.005)
"""


def _stale_lock(jobs: Path, job_id: str = "job-race-0001") -> tuple[Path, Path]:
    record = jobs / f"{job_id}.json"
    lock_path = jobs / f"{job_id}.json.lock"
    lock_path.write_bytes(b"")
    old = time.time() - 2 * 86400
    os.utime(lock_path, (old, old))
    return record, lock_path


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {path}")


def _prune(jobs: Path) -> int:
    return retention._prune_stale_job_locks(
        jobs, now=time.time(), deadline=time.monotonic() + 5.0
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock inode revalidation")
def test_record_lock_reopens_live_inode_after_stale_prune(tmp_path):
    pytest.importorskip("fcntl")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    record, lock_path = _stale_lock(jobs)
    old_inode = lock_path.stat().st_ino
    ready, proceed = tmp_path / "ready", tmp_path / "proceed"
    acquired, release = tmp_path / "acquired", tmp_path / "release"
    environment = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT),
        "REMOTE_DEV_STATE_DIR": str(tmp_path / "state"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCKER_SCRIPT, str(record), str(ready),
         str(proceed), str(acquired), str(release)],
        cwd=str(_REPO_ROOT), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_for(ready)  # actor B has the old inode open, blocked before flock
        assert _prune(jobs) == 1
        assert not lock_path.exists()
        proceed.write_text("go", encoding="utf-8")
        _wait_for(acquired)  # actor B detected the mismatch and re-opened
        assert int(acquired.read_text(encoding="utf-8")) != old_inode
        # Actor C must block on B's new-inode lock, never enter concurrently.
        with pytest.raises(TimeoutError, match="waiting for record lock"):
            with record_lock(record, timeout_seconds=0.1):
                raise AssertionError("entered while the re-opened lock was held")
    finally:
        proceed.write_text("go", encoding="utf-8")
        release.write_text("go", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, (stdout, stderr)
    with record_lock(record, timeout_seconds=1.0):
        assert lock_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock inode revalidation")
def test_stale_prune_keeps_a_held_in_process_record_lock(tmp_path, monkeypatch):
    pytest.importorskip("fcntl")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    record, lock_path = _stale_lock(jobs)
    entered, release = threading.Event(), threading.Event()
    unlink_attempts = []
    monkeypatch.setattr(
        retention, "_unlink_stale_lock",
        lambda *args, **kwargs: unlink_attempts.append(args) or 0,
    )

    def owner():
        with record_lock(record):
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        # The in-process RLock is busy, so the unlinker must skip even before
        # it can probe the OS lock.
        assert _prune(jobs) == 0
        assert unlink_attempts == []
        assert lock_path.exists()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_stale_prune_keeps_a_lock_held_by_another_descriptor(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _record, lock_path = _stale_lock(jobs)
    with lock_path.open("r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # No in-process RLock owns this path, so retention reaches the OS
            # lock probe and must treat contention as "keep".
            assert _prune(jobs) == 0
            assert lock_path.exists()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_stale_prune_rechecks_the_record_after_acquiring_flock(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    record, lock_path = _stale_lock(jobs)
    real_flock = fcntl.flock

    class CreatingFlock:
        LOCK_EX, LOCK_NB, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN

        @staticmethod
        def flock(fd, operation):
            if operation & fcntl.LOCK_EX and not record.exists():
                record.write_text('{"state": "running"}', encoding="utf-8")
            return real_flock(fd, operation)

    monkeypatch.setattr(retention, "fcntl", CreatingFlock)
    assert _prune(jobs) == 0
    assert lock_path.exists()
    assert record.exists()


@pytest.mark.parametrize(
    "companion",
    [
        "job-race-0001.stdout.log",
        "job-race-0001.stderr.log.prev",
        ".job-race-0001.json.abc123.tmp",
        "job-race-0001.json.tmpabc",
    ],
)
def test_stale_prune_keeps_locks_with_any_companion(tmp_path, companion):
    pytest.importorskip("fcntl")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    _record, lock_path = _stale_lock(jobs)
    (jobs / companion).write_bytes(b"x")
    assert _prune(jobs) == 0
    assert lock_path.exists()
