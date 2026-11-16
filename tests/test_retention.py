"""Tier 0/1 retention contracts: capped remote logs, terminal-job GC, and local pruning."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from _worker_source import load_worker_module
from remote_dev.core import retention, state_store
from remote_dev.core.endpoint import Endpoint


@pytest.fixture(scope="module")
def worker():
    pytest.importorskip("fcntl")
    return load_worker_module()


def _remote_job(
    root: Path,
    job_id: str,
    *,
    state: str = "succeeded",
    finished_at: float | None = None,
    drained: bool = True,
    retain: bool = False,
    retain_spec: bool = False,
    unknown=None,
    size: int = 0,
    write_result: bool = True,
) -> Path:
    directory = root / ".remote-dev" / "jobs" / job_id
    directory.mkdir(parents=True)
    if write_result:
        result = {
            "state": state,
            "descendants_drained": drained,
            "finished_at": time.time() if finished_at is None else finished_at,
        }
        if retain:
            result["retain_until_release"] = True
        if unknown is not None:
            result["unknown"] = unknown
        (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if retain_spec:
        (directory / "spec.json").write_text(
            json.dumps({"retain_until_release": True}), encoding="utf-8"
        )
    if size:
        (directory / "stdout.log").write_text("x" * size, encoding="utf-8")
    return directory


def _terminal_names(jobs_root: Path) -> set[str]:
    return {
        child.name for child in jobs_root.iterdir()
        if child.is_dir() and not child.name.startswith(".trash-")
    }


def test_remote_gc_removes_jobs_past_ttl_only(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    old = _remote_job(tmp_path, "job-expired-0001", finished_at=now - 8 * 86400)
    fresh = _remote_job(tmp_path, "job-fresh-0001", finished_at=now - 60)

    stats = worker.collect_terminal_jobs(jobs_root=jobs, now=now)

    assert stats["removed"] == ["job-expired-0001"]
    assert not old.exists()
    assert fresh.exists()
    assert not list(jobs.glob(".trash-*"))


def test_remote_gc_keeps_newest_count_and_byte_limits(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    for index in range(5):
        _remote_job(
            tmp_path,
            f"job-count-{index:04d}",
            finished_at=now - 1000 + index,
            size=1000,
        )

    stats = worker.collect_terminal_jobs(
        jobs_root=jobs,
        now=now,
        ttl_seconds=10**9,
        max_terminal_jobs=2,
        max_terminal_bytes=10**9,
    )

    assert sorted(stats["removed"]) == [
        "job-count-0000", "job-count-0001", "job-count-0002",
    ]
    assert _terminal_names(jobs) == {"job-count-0003", "job-count-0004"}

    stats = worker.collect_terminal_jobs(
        jobs_root=jobs,
        now=now,
        ttl_seconds=10**9,
        max_terminal_jobs=128,
        max_terminal_bytes=1500,
    )
    assert sorted(stats["removed"]) == ["job-count-0003"]
    assert _terminal_names(jobs) == {"job-count-0004"}
    retained = sum(
        (jobs / name / "stdout.log").stat().st_size for name in _terminal_names(jobs)
    )
    assert retained <= 2500


def test_remote_gc_never_touches_live_unknown_retained_or_locked_jobs(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    old = now - 30 * 86400
    _remote_job(tmp_path, "job-running-0001", state="running", finished_at=old, drained=False)
    _remote_job(tmp_path, "job-prepared-0001", state="prepared", finished_at=old,
                drained=False, write_result=False)
    _remote_job(tmp_path, "job-unknown-0001", state="succeeded", finished_at=old,
                unknown=["cannot observe descendants"])
    _remote_job(tmp_path, "job-retained-0001", state="succeeded", finished_at=old,
                retain=True)
    _remote_job(tmp_path, "job-retained-0002", state="succeeded", finished_at=old,
                retain_spec=True)
    _remote_job(tmp_path, "job-excluded-0001", state="succeeded", finished_at=old)
    locked = _remote_job(tmp_path, "job-locked-0001", state="succeeded", finished_at=old)

    fcntl = pytest.importorskip("fcntl")
    with (locked / "worker.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stats = worker.collect_terminal_jobs(
            jobs_root=jobs, now=now, exclude=("job-excluded-0001",)
        )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    assert stats["removed"] == []
    assert _terminal_names(jobs) == {
        "job-running-0001", "job-prepared-0001", "job-unknown-0001",
        "job-retained-0001", "job-retained-0002", "job-excluded-0001",
        "job-locked-0001",
    }
    assert stats["skipped_unknown"] == 1
    assert stats["skipped_retained"] == 2
    assert stats["skipped_active"] >= 3


def test_remote_gc_requires_uid_ownership(worker, tmp_path):
    if not hasattr(os, "getuid"):
        pytest.skip("UID ownership is a POSIX invariant")
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    _remote_job(tmp_path, "job-foreign-0001", finished_at=now - 30 * 86400)
    with mock.patch.object(worker.os, "getuid", return_value=os.getuid() + 1):
        stats = worker.collect_terminal_jobs(jobs_root=jobs, now=now)
    assert stats["removed"] == []
    assert (jobs / "job-foreign-0001").exists()
    assert stats["skipped_unowned"] == 1


def test_housekeeping_cooldown_and_forced_run(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    _remote_job(tmp_path, "job-cooldown-0001", finished_at=now - 8 * 86400)
    first = worker.maybe_run_job_housekeeping(tmp_path, now=now)
    assert first["removed"] == ["job-cooldown-0001"]
    assert (jobs / ".housekeeping.json").exists()

    _remote_job(tmp_path, "job-cooldown-0002", finished_at=now - 8 * 86400)
    second = worker.maybe_run_job_housekeeping(tmp_path, now=now + 1)
    assert second == {"skipped": "cooldown", "cooldown_seconds": 300}
    assert (jobs / "job-cooldown-0002").exists()

    third = worker.maybe_run_job_housekeeping(tmp_path, now=now + 1, force=True)
    assert third["removed"] == ["job-cooldown-0002"]


def test_capped_log_ring_keeps_disk_bounded_and_accounts_logically(worker, tmp_path):
    payload = bytes(range(256)) * 16  # 4096 bytes
    with worker.CappedLog(tmp_path, "stdout", 1024) as log:
        for offset in range(0, len(payload), 333):
            log.write(payload[offset:offset + 333])

    current = (tmp_path / "stdout.log").stat().st_size
    previous = (tmp_path / "stdout.log.prev").stat().st_size
    assert current + previous <= 1024
    assert log.logical_bytes_written == len(payload)
    assert log.truncated_head_bytes == len(payload) - (current + previous)

    meta = json.loads((tmp_path / "logs.json").read_text(encoding="utf-8"))
    assert meta["stdout"]["logical_bytes_written"] == len(payload)
    assert meta["stdout"]["truncated_head_bytes"] == log.truncated_head_bytes

    window = worker.read_log_window(tmp_path, "stdout", 0, 128)
    assert window["logical_bytes"] == len(payload)
    assert window["truncated_head_bytes"] == log.truncated_head_bytes
    assert window["skipped_bytes"] == log.truncated_head_bytes
    assert window["start"] == log.truncated_head_bytes
    assert window["data"] == payload[window["start"]:window["start"] + 128]

    next_window = worker.read_log_window(tmp_path, "stdout", window["end"], 128)
    assert next_window["data"] == payload[next_window["start"]:next_window["end"]]
    assert next_window["skipped_bytes"] == 0

    tail = worker.read_log_window(tmp_path, "stdout", None, 128, tail_bytes=128)
    assert tail["end"] == len(payload)
    assert tail["data"] == payload[-128:]


def test_tail_action_reports_truncated_head_instead_of_silent_loss(worker, tmp_path):
    payload = b"".join(f"line-{index:05d}\n".encode() for index in range(2000))
    job_id = "job-tail-truncated"
    directory = tmp_path / ".remote-dev" / "jobs" / job_id
    directory.mkdir(parents=True)
    with worker.CappedLog(directory, "stdout", 1024) as log:
        log.write(payload)
    (directory / "receipt.json").write_text(
        json.dumps({"pid": 1, "ppid": 1, "pgid": 1, "start_ticks": "1",
                    "boot_id": "boot-test", "marker": "m" * 32,
                    "supervision": "subreaper", "job_id": job_id}),
        encoding="utf-8",
    )
    with mock.patch.object(worker, "job_status", return_value={"state": "succeeded", "quiet": True}):
        row = worker.control_job(
            {"root": str(tmp_path), "job_id": job_id, "action": "tail",
             "stdout_offset": 0, "max_bytes": 128},
            "",
        )
    truncated = row["stdout_truncated_head_bytes"]
    assert truncated > 0
    assert row["stdout_truncated"] is True
    assert row["stdout_skipped_bytes"] == truncated
    assert row["stdout_offset"] == truncated + len(row["stdout"].encode("utf-8"))
    assert row["stdout"].encode("utf-8") == payload[truncated:row["stdout_offset"]]
    assert row["stdout_logical_bytes"] == len(payload)
    assert row["stdout_bytes_remaining"] == len(payload) - row["stdout_offset"]


def _local_reads(base: Path, count: int, *, mtime: float) -> Path:
    directory = base / "reads" / "default"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        path = directory / f"{index:04d}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    return directory


def _local_logs(base: Path, count: int, *, mtime: float) -> Path:
    directory = base / "logs" / "remote.read"
    for index in range(count):
        invocation = directory / f"inv-{index:04d}"
        invocation.mkdir(parents=True)
        (invocation / "log.txt").write_text("x", encoding="utf-8")
        os.utime(invocation, (mtime, mtime))
    return directory


def _local_jobs(base: Path, count: int, *, mtime: float, state: str = "succeeded") -> Path:
    directory = base / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        job_id = f"job-{state}-{index:04d}"
        record = directory / f"{job_id}.json"
        record.write_text(json.dumps({"job_id": job_id, "state": state}), encoding="utf-8")
        (directory / f"{job_id}.stdout.log").write_text("x" * 10, encoding="utf-8")
        os.utime(record, (mtime, mtime))
    return directory


def test_local_prune_caps_reads_logs_and_terminal_jobs(tmp_path):
    base = tmp_path / "endpoints" / "ep1"
    now = time.time()
    reads = _local_reads(base, 300, mtime=now - 10)
    logs = _local_logs(base, 300, mtime=now - 10)
    jobs = _local_jobs(base, 130, mtime=now - 10)
    _local_jobs(base, 1, mtime=now - 10, state="running")
    _local_jobs(base, 1, mtime=now - 10, state="unknown")

    stats = retention.prune_endpoint_state(base, now=now, force=True)

    assert stats["reads"]["removed"] == 44
    assert stats["logs"]["removed"] == 44
    assert stats["jobs"]["removed"] == 2
    assert len(list(reads.glob("*.json"))) == 256
    assert len(list(logs.iterdir())) == 256
    assert len(list(jobs.glob("*.json"))) == 130  # 128 terminal + running + unknown
    assert (jobs / "job-running-0000.json").exists()
    assert (jobs / "job-unknown-0000.json").exists()


def test_local_prune_bytes_age_and_cooldown(tmp_path):
    base = tmp_path / "endpoints" / "ep1"
    now = time.time()
    reads = _local_reads(base, 3, mtime=now - 30 * 86400)
    jobs = _local_jobs(base, 5, mtime=now - 10)
    for index in range(5):
        (jobs / f"job-succeeded-{index:04d}.stdout.log").write_text(
            "x" * 1000, encoding="utf-8"
        )
    stats = retention.prune_endpoint_state(
        base, now=now, force=True,
        reads_ttl_seconds=60,
        jobs_max_entries=128,
        jobs_max_bytes=2500,
    )
    assert stats["reads"]["removed"] == 3
    assert not list(reads.glob("*.json"))
    remaining = list(jobs.glob("job-succeeded-*.json"))
    assert len(remaining) <= 2
    assert sum(path.stat().st_size for path in jobs.glob("job-succeeded-*.stdout.log")) <= 2500

    _local_reads(base, 1, mtime=now)
    skipped = retention.prune_endpoint_state(base, now=now + 1, force=False)
    assert skipped["skipped"] == "cooldown"
    forced = retention.prune_endpoint_state(base, now=now + 1, force=True)
    assert "skipped" not in forced


def test_local_prune_preserves_a_locked_terminal_record(tmp_path):
    base = tmp_path / "endpoints" / "ep1"
    now = time.time()
    jobs = _local_jobs(base, 1, mtime=now - 10)
    record = jobs / "job-succeeded-0000.json"
    fcntl = pytest.importorskip("fcntl")
    with (jobs / "job-succeeded-0000.json.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        retention.prune_endpoint_state(
            base, now=now, force=True, jobs_max_entries=0, jobs_max_bytes=0
        )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert record.exists()


def test_state_store_force_prune_preserves_active_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "state"))
    endpoint = Endpoint(host="127.0.0.1", port=22, root="/srv/app", cwd="/srv/app")
    base = state_store.ensure_endpoint_state(endpoint)
    now = time.time()
    _local_reads(base, 300, mtime=now - 10)
    _local_jobs(base, 130, mtime=now - 10)
    _local_jobs(base, 1, mtime=now - 10, state="running")
    _local_jobs(base, 1, mtime=now - 10, state="uncertain")
    _local_jobs(base, 1, mtime=now - 10, state="unknown")

    stats = state_store.maybe_prune_endpoint_state(endpoint, force=True)

    assert stats["reads"]["removed"] == 44
    assert stats["jobs"]["removed"] == 2
    jobs = base / "jobs"
    assert (jobs / "job-running-0000.json").exists()
    assert (jobs / "job-uncertain-0000.json").exists()
    assert (jobs / "job-unknown-0000.json").exists()
    assert len(list(jobs.glob("job-succeeded-*.json"))) == 128


def test_prune_stale_session_runtime_dirs_keeps_receipts_alive_and_locked(tmp_path):
    now = time.time()
    old = (now - 48 * 3600, now - 48 * 3600)
    root = tmp_path / "sessions"

    dead = root / "gate-v1-999999-dead"
    dead.mkdir(parents=True)
    (dead / "pid").write_text("999999", encoding="utf-8")
    os.utime(dead, old)

    alive = root / f"gate-v1-{os.getpid()}-alive"
    alive.mkdir()
    (alive / "pid").write_text(str(os.getpid()), encoding="utf-8")
    os.utime(alive, old)

    receipts = root / "runtime-999998-receipts"
    receipts.mkdir()
    (receipts / "pid").write_text("999998", encoding="utf-8")
    (receipts / "remote-receipts.sqlite3").write_text("rows", encoding="utf-8")
    os.utime(receipts, old)

    locked = root / "session-999997-locked"
    locked.mkdir()
    (locked / "pid").write_text("999997", encoding="utf-8")
    fcntl = pytest.importorskip("fcntl")
    with (locked / "session.lock").open("a+b") as handle:
        os.utime(locked, old)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stats = retention.prune_stale_session_runtime_dirs(root, now=now)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    unrelated = root / "unrelated"
    unrelated.mkdir()
    os.utime(unrelated, old)

    assert stats["removed"] == ["gate-v1-999999-dead"]
    assert not dead.exists()
    assert alive.exists()
    assert receipts.exists()
    assert locked.exists()
    assert unrelated.exists()
    assert stats["skipped"]["pid_alive_or_unknown"] == 1
    assert stats["skipped"]["receipt_store"] == 1
    assert stats["skipped"]["locked_or_unverifiable"] == 1


def test_windows_pid_probe_never_calls_os_kill_and_distinguishes_codes():
    from remote_dev.core import windows_probe

    class FakeApi:
        def __init__(self, *, handle=None, error=0, code=None):
            self.handle = handle
            self.error = error
            self.code = code
            self.closed = 0

        def open_process(self, pid):
            assert pid == 4242
            return self.handle

        def last_error(self):
            return self.error

        def exit_code(self, handle):
            assert handle is self.handle
            return self.code

        def close(self, handle):
            self.closed += 1

    assert windows_probe.ERROR_INVALID_PARAMETER == 87
    assert windows_probe.ERROR_ACCESS_DENIED == 5
    assert windows_probe.STILL_ACTIVE == 259

    with mock.patch.object(retention.os, "kill") as kill:
        invalid = FakeApi(error=87)
        assert retention._pid_is_dead(4242, platform="nt", winapi=invalid) is True
        denied = FakeApi(error=5)
        assert retention._pid_is_dead(4242, platform="nt", winapi=denied) is False
        unknown = FakeApi(error=0)
        assert retention._pid_is_dead(4242, platform="nt", winapi=unknown) is False
        active = FakeApi(handle=object(), code=259)
        assert retention._pid_is_dead(4242, platform="nt", winapi=active) is False
        exited = FakeApi(handle=object(), code=0)
        assert retention._pid_is_dead(4242, platform="nt", winapi=exited) is True
        failed = FakeApi(handle=object(), code=1)
        assert retention._pid_is_dead(4242, platform="nt", winapi=failed) is True
        kill.assert_not_called()
    assert invalid.closed == 0  # no handle was opened for error 87
    assert active.closed == 1
    assert exited.closed == 1


def test_windows_lock_probe_is_conservative_and_matches_byte_lock(tmp_path):
    from remote_dev.core import windows_probe

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self, *, locked):
            self.locked = locked

        def locking(self, fileno, mode, length):
            assert length == 1
            if self.locked and mode == self.LK_NBLCK:
                raise OSError("already locked")

    lock_path = tmp_path / "job.json.lock"
    lock_path.write_bytes(b"\0")
    assert windows_probe.windows_lock_free(
        lock_path, msvcrt_module=FakeMsvcrt(locked=False)
    ) is True
    assert windows_probe.windows_lock_free(
        lock_path, msvcrt_module=FakeMsvcrt(locked=True)
    ) is False
    assert windows_probe.windows_lock_free(
        tmp_path / "missing.json.lock", msvcrt_module=FakeMsvcrt(locked=False)
    ) is True


def _local_record(jobs: Path, job_id: str, state: str) -> dict[str, Path]:
    record = jobs / f"{job_id}.json"
    record.write_text(json.dumps({"job_id": job_id, "state": state}), encoding="utf-8")
    stdout = jobs / f"{job_id}.stdout.log"
    stdout.write_text("x" * 10, encoding="utf-8")
    return {"record": record, "stdout": stdout}


@pytest.mark.parametrize("sibling_state", ["unknown", "running"])
def test_local_prune_never_matches_dotted_job_id_siblings(tmp_path, sibling_state):
    base = tmp_path / "endpoints" / "ep1"
    jobs = base / "jobs"
    jobs.mkdir(parents=True)
    now = time.time()
    terminal = _local_record(jobs, "job-a", "completed")
    sibling = _local_record(jobs, "job-a.retry", sibling_state)
    os.utime(sibling["record"], (now - 10, now - 10))
    before = sibling["record"].read_bytes()
    sibling_stdout = sibling["stdout"].read_bytes()

    retention.prune_endpoint_state(
        base, now=now, force=True, jobs_max_entries=0, jobs_max_bytes=0,
    )

    assert sibling["record"].read_bytes() == before
    assert sibling["stdout"].read_bytes() == sibling_stdout
    assert not terminal["record"].exists()
    assert not terminal["stdout"].exists()


def test_local_prune_protects_the_active_transaction_job(tmp_path):
    base = tmp_path / "endpoints" / "ep1"
    jobs = base / "jobs"
    jobs.mkdir(parents=True)
    now = time.time()
    active = _local_record(jobs, "job-active", "succeeded")
    retention.prune_endpoint_state(
        base, now=now, force=True, jobs_max_entries=0, jobs_max_bytes=0,
        protect_job_ids={"job-active"},
    )
    assert active["record"].exists()
    assert active["stdout"].exists()


def test_local_prune_rechecks_terminal_state_after_acquiring_lock(tmp_path, monkeypatch):
    base = tmp_path / "endpoints" / "ep1"
    jobs = base / "jobs"
    jobs.mkdir(parents=True)
    now = time.time()
    record = _local_record(jobs, "job-raced", "succeeded")["record"]
    real_acquire = retention._acquire_record_lock

    def acquire_after_writer(path, timeout):
        path.write_text(json.dumps({"job_id": "job-raced", "state": "running"}),
                        encoding="utf-8")
        return real_acquire(path, timeout)

    monkeypatch.setattr(retention, "_acquire_record_lock", acquire_after_writer)
    retention.prune_endpoint_state(
        base, now=now, force=True, jobs_max_entries=0, jobs_max_bytes=0,
    )
    assert record.exists()


def test_remote_gc_cursor_advances_past_uncollectable_prefix(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    _remote_job(tmp_path, "job-a-active", state="running", drained=False)
    _remote_job(tmp_path, "job-b-active", state="running", drained=False)
    _remote_job(tmp_path, "job-c-expired", finished_at=now - 30 * 86400)

    first = worker.collect_terminal_jobs(
        jobs_root=jobs, now=now, max_entries=2, time_budget_seconds=2.0,
    )
    assert first["scanned"] == 2
    assert first["next_cursor"] == "job-b-active"
    assert (jobs / "job-c-expired").exists()

    second = worker.collect_terminal_jobs(
        jobs_root=jobs, now=now, max_entries=2,
        cursor=first["next_cursor"], time_budget_seconds=2.0,
    )
    assert second["removed"] == ["job-c-expired"]
    assert not (jobs / "job-c-expired").exists()
    assert (jobs / "job-a-active").exists()
    assert (jobs / "job-b-active").exists()
    assert second["cursor_wrapped"] is True
    assert second["next_cursor"] == ""


def test_housekeeping_persists_cursor_and_wraps(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    _remote_job(tmp_path, "job-a-active", state="running", drained=False)
    _remote_job(tmp_path, "job-b-active", state="running", drained=False)
    _remote_job(tmp_path, "job-c-expired", finished_at=now - 30 * 86400)

    first = worker.maybe_run_job_housekeeping(tmp_path, now=now, force=True, max_entries=2)
    marker = json.loads((jobs / ".housekeeping.json").read_text(encoding="utf-8"))
    assert marker["last_cursor"] == marker["cursor"] == first["next_cursor"] == "job-b-active"
    assert (jobs / "job-c-expired").exists()

    second = worker.maybe_run_job_housekeeping(tmp_path, now=now + 1, force=True, max_entries=2)
    assert second["removed"] == ["job-c-expired"]
    marker = json.loads((jobs / ".housekeeping.json").read_text(encoding="utf-8"))
    assert marker["last_cursor"] == marker["cursor"] == ""


def test_remote_gc_dry_run_never_removes_trash_or_jobs(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    _remote_job(tmp_path, "job-dryrun-0001", finished_at=now - 30 * 86400)
    trash = jobs / ".trash-job-old-12345678"
    trash.mkdir()
    old = now - 3600
    os.utime(trash, (old, old))

    stats = worker.collect_terminal_jobs(jobs_root=jobs, now=now, dry_run=True)

    assert stats["would_remove"] == ["job-dryrun-0001"]
    assert stats["trash_would_clean"] == 1
    assert stats["trash_cleaned"] == 0
    assert (jobs / "job-dryrun-0001").exists()
    assert trash.exists()


def test_remote_gc_holds_control_and_supervisor_locks_during_removal(worker, tmp_path):
    now = time.time()
    jobs = tmp_path / ".remote-dev" / "jobs"
    job = _remote_job(tmp_path, "job-fenced-0001", finished_at=now - 30 * 86400)
    fcntl = pytest.importorskip("fcntl")
    with (job / "lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stats = worker.collect_terminal_jobs(jobs_root=jobs, now=now)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert stats["removed"] == []
    assert job.exists()
    assert stats["skipped_active"] >= 1

    with worker._held_job_locks(job) as first:
        assert first is True
        with worker._held_job_locks(job) as second:
            assert second is False


def test_capped_log_derives_truncation_after_metadata_crash(worker, tmp_path):
    payload = bytes(range(256)) * 20
    with worker.CappedLog(tmp_path, "stdout", 1024) as log:
        log.write(payload)
    meta = json.loads((tmp_path / "logs.json").read_text(encoding="utf-8"))
    expected_truncated = meta["stdout"]["truncated_head_bytes"]
    expected_logical = meta["stdout"]["logical_bytes_written"]
    assert expected_truncated > 0

    # Simulate a crash between segment rotation and logs.json publication by
    # restoring the pre-rotation truncated offset while keeping logical bytes.
    meta["stdout"]["truncated_head_bytes"] = 0
    (tmp_path / "logs.json").write_text(json.dumps(meta), encoding="utf-8")
    reopened = worker.CappedLog(tmp_path, "stdout", 1024)
    reopened.close()
    healed = json.loads((tmp_path / "logs.json").read_text(encoding="utf-8"))["stdout"]
    assert healed["logical_bytes_written"] == expected_logical
    assert healed["truncated_head_bytes"] == expected_truncated


def test_capped_log_close_persists_metadata_after_rotation_failure(worker, tmp_path):
    (tmp_path / "stdout.log.prev").write_bytes(b"p" * 100)
    log = worker.CappedLog(tmp_path, "stdout", 64)
    real_replace = worker.os.replace

    def fail_rotation(source, destination):
        if str(destination).endswith(".log.prev"):
            raise OSError("simulated rotation failure")
        return real_replace(source, destination)

    with mock.patch.object(worker.os, "replace", side_effect=fail_rotation):
        with pytest.raises(OSError, match="rotation failure"):
            log.write(b"x" * 200)
        log.close()

    meta = json.loads((tmp_path / "logs.json").read_text(encoding="utf-8"))["stdout"]
    assert meta["truncated_head_bytes"] >= 100
    assert "accounting_warning" in meta
    assert log.accounting_warning


def test_stop_records_stop_json_before_bounded_maintenance(worker, tmp_path):
    job_id = "job-stop-order"
    directory = tmp_path / ".remote-dev" / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text("{}", encoding="utf-8")
    seen = {}

    def fake_maintenance(root, identifier):
        seen["stop_json"] = (directory / "stop.json").exists()
        return {"skipped": "timeout", "error": "maintenance timeout"}

    running = {"state": "running", "quiet": False, "unknown": [],
               "processes": [{"pid": 123, "start_ticks": "1"}]}
    with mock.patch.object(worker, "job_status", return_value=running), \
            mock.patch.object(worker, "signal_processes"), \
            mock.patch.object(worker, "_bounded_housekeeping", side_effect=fake_maintenance):
        result = worker.control_job(
            {"root": str(tmp_path), "job_id": job_id, "action": "stop"}, ""
        )
    assert seen["stop_json"] is True
    assert "maintenance timeout" in result["maintenance_warning"]


def test_capped_log_warns_when_rotated_metadata_is_missing(worker, tmp_path):
    payload = bytes(range(256)) * 20
    with worker.CappedLog(tmp_path, "stdout", 1024) as log:
        log.write(payload)
    (tmp_path / "logs.json").unlink()
    reopened = worker.CappedLog(tmp_path, "stdout", 1024)
    assert reopened.accounting_warning
    assert "understated" in reopened.accounting_warning
    reopened.close()
    meta = json.loads((tmp_path / "logs.json").read_text(encoding="utf-8"))["stdout"]
    assert meta["accounting_warning"]


def test_housekeeping_cooldown_marker_failure_is_bounded(worker, tmp_path, monkeypatch):
    def blocked(_path):
        raise TimeoutError("blocked marker read")

    monkeypatch.setattr(worker, "_read_json_dict", blocked)
    stats = worker.maybe_run_job_housekeeping(tmp_path, now=time.time())
    assert stats["skipped"] == "timeout"
    assert "TimeoutError" in stats["error"]
