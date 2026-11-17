"""
[Role] Bounded local retention for endpoint read ledgers, tool logs, and job records
(`core/retention.py`).
[Invariants]
- Deletes local job records only when their persisted state is definitively terminal
  (`succeeded`/`failed`/`timeout`/`cancelled`/`completed`/`timed_out`) and not `unknown`;
  `running`, `prepared`, `starting`, `stopping`, `uncertain`, and `unknown` records are untouched.
- A job's local artifacts are exactly `<job_id>.json`, `<job_id>.stdout.log`,
  `<job_id>.stderr.log`, and their `.prev` segments; arbitrary `job_id.*` siblings (for example
  `job-a.retry.json`) are never matched or removed.
- Read ledgers and tool logs expire after 7 days, 256 entries, or 128 MiB per endpoint;
  terminal local job artifacts expire after 14 days, 128 records, or 256 MiB.
- `prune_stale_session_runtime_dirs()` never deletes a directory containing a SQLite
  receipt store, so persistent RemoteReceipts deduplication rows for unknown/executed
  external mutations survive every cleanup.
- Windows PID liveness never calls `os.kill`; it uses `OpenProcess` + `GetExitCodeProcess`
  and treats `ERROR_INVALID_PARAMETER` as exited while `ERROR_ACCESS_DENIED` or any
  unconfirmed result keeps the directory.
- A stale `.json.lock` is only removed while the in-process record `RLock` and the OS lock are both held,
  after re-proving the record and every companion are absent; `record_lock` re-checks its handle against
  the live directory entry after `flock`, so an already-opened old inode can never grant mutual exclusion.
[Timeouts & Locks]
- Pruning is opportunistic on writes and skipped for `300s` by the endpoint `.prune.json`
  marker; each pass is capped by `2s` and `4096` directory observations.
- Job deletion holds the exclusive `record_lock` across re-verification and unlinking; a
  concurrent holder or an unverifiable Windows/MSVCRT lock means "keep". Stale `.json.lock`
  inodes are only removed in a separate pass when >24h old, unlocked, and the record and all
  companion segments are absent; `unlink` happens before the OS lock is released.
[Side-Effect Policy]
- Mutates only paths below the explicit endpoint-state directory or session runtime root
  passed by the caller. No network, subprocess, or remote job side effects.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.locking import record_lock, try_record_thread_lock
from remote_dev.core.session_retention import (
    RECEIPT_STORE_SUFFIXES,
    SESSION_LOCK_NAMES,
    SESSION_RUNTIME_PREFIXES,
    _contains_receipt_store,
    _pid_is_dead,
    _session_lock_free,
    _session_pid,
    prune_stale_session_runtime_dirs,
)

try:  # POSIX lock probing; Windows uses msvcrt.locking for the same byte lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

TERMINAL_LOCAL_JOB_STATES = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "timeout", "timed_out"}
)
READS_TTL_SECONDS = 7 * 24 * 60 * 60
LOGS_TTL_SECONDS = 7 * 24 * 60 * 60
READS_MAX_ENTRIES = 256
LOGS_MAX_ENTRIES = 256
READS_MAX_BYTES = 128 * 1024 * 1024
LOGS_MAX_BYTES = 128 * 1024 * 1024
JOBS_TTL_SECONDS = 14 * 24 * 60 * 60
JOBS_MAX_ENTRIES = 128
JOBS_MAX_BYTES = 256 * 1024 * 1024
CONTEXT_TTL_SECONDS = 7 * 24 * 60 * 60
CONTEXT_MAX_ENTRIES = 64
CONTEXT_MAX_BYTES = 64 * 1024 * 1024
ARTIFACTS_TTL_SECONDS = 7 * 24 * 60 * 60
ARTIFACTS_MAX_ENTRIES = 64
ARTIFACTS_MAX_BYTES = 512 * 1024 * 1024
ARTIFACTS_MIN_AGE_SECONDS = 60
PATCHES_TTL_SECONDS = 7 * 24 * 60 * 60
PATCHES_MAX_ENTRIES = 256
PATCHES_MAX_BYTES = 128 * 1024 * 1024
PRUNE_COOLDOWN_SECONDS = 300
PRUNE_TIME_BUDGET_SECONDS = 2.0
PRUNE_MAX_ENTRIES = 4096
JOB_LOCK_WAIT_SECONDS = 0.25
STALE_JOB_LOCK_SECONDS = 24 * 60 * 60
# Data/log segments first so a crashed delete leaves the record (and therefore
# a later retry) rather than an unreachable orphan log.
JOB_COMPANION_SUFFIXES = (
    ".stdout.log", ".stderr.log", ".stdout.log.prev", ".stderr.log.prev", ".json",
)
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")


def _scandir(directory, deadline):
    try:
        entries = os.scandir(directory)
    except TimeoutError:
        raise
    except OSError:
        return []
    result = []
    with entries:
        for entry in entries:
            if time.monotonic() >= deadline:
                break
            result.append(entry)
    return result


def _json_record(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entry_size(path, deadline, *, max_files=PRUNE_MAX_ENTRIES):
    total, files, pending = 0, 0, [str(path)]
    while pending:
        if time.monotonic() >= deadline or files >= max_files:
            break
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except TimeoutError:
            raise
        except OSError:
            continue
        with entries:
            for entry in entries:
                if files >= max_files or time.monotonic() >= deadline:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except TimeoutError:
                    raise
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    files += 1
    return total


def _remove(path):
    try:
        info = path.stat(follow_symlinks=False)
    except TimeoutError:
        raise
    except OSError:
        return 0
    try:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
    except TimeoutError:
        raise
    except OSError:
        return 0
    return int(getattr(info, "st_size", 0))


def _apply_limits(entries, *, now, ttl_seconds, max_entries, max_bytes, deadline):
    """Delete stale then oldest entries; each entry is {path, mtime, size}."""
    removed, freed = 0, 0
    expired = {entry["path"] for entry in entries if now - entry["mtime"] > ttl_seconds}
    kept = [entry for entry in entries if entry["path"] not in expired]
    kept_bytes = sum(entry["size"] for entry in kept)
    for entry in sorted(entries, key=lambda row: (row["mtime"], str(row["path"]))):
        if entry["path"] in expired:
            if time.monotonic() >= deadline:
                break
            freed += _remove(entry["path"])
            removed += 1
            continue
        if len(kept) <= max_entries and kept_bytes <= max_bytes:
            break
        if time.monotonic() >= deadline:
            break
        freed += _remove(entry["path"])
        removed += 1
        kept = [row for row in kept if row["path"] != entry["path"]]
        kept_bytes -= entry["size"]
    return {"removed": removed, "freed_bytes": freed}


def _prune_reads(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes):
    entries = []
    for scope in _scandir(directory, deadline):
        path = Path(scope.path)
        try:
            if scope.is_file(follow_symlinks=False):
                if path.suffix == ".json":
                    info = scope.stat(follow_symlinks=False)
                    entries.append({"path": path, "mtime": info.st_mtime, "size": info.st_size})
                continue
            if not scope.is_dir(follow_symlinks=False):
                continue
            for record in _scandir(path, deadline):
                if not record.name.endswith(".json") or not record.is_file(follow_symlinks=False):
                    continue
                info = record.stat(follow_symlinks=False)
                entries.append({"path": Path(record.path), "mtime": info.st_mtime,
                                "size": info.st_size})
        except TimeoutError:
            raise
        except OSError:
            continue
    stats = _apply_limits(entries, now=now, ttl_seconds=ttl_seconds,
                          max_entries=max_entries, max_bytes=max_bytes, deadline=deadline)
    for scope in _scandir(directory, deadline):
        path = Path(scope.path)
        if path.is_dir():
            try:
                path.rmdir()  # only succeeds when every ledger was removed
            except TimeoutError:
                raise
            except OSError:
                pass
    return stats


def _prune_logs(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes):
    entries = []
    for tool in _scandir(directory, deadline):
        tool_path = Path(tool.path)
        try:
            if tool.is_file(follow_symlinks=False):
                info = tool.stat(follow_symlinks=False)
                entries.append({"path": tool_path, "mtime": info.st_mtime, "size": info.st_size})
                continue
            if not tool.is_dir(follow_symlinks=False):
                continue
            for invocation in _scandir(tool_path, deadline):
                info = invocation.stat(follow_symlinks=False)
                size = (info.st_size if stat.S_ISREG(info.st_mode)
                        else _entry_size(Path(invocation.path), deadline))
                entries.append({"path": Path(invocation.path), "mtime": info.st_mtime,
                                "size": size})
        except TimeoutError:
            raise
        except OSError:
            continue
    stats = _apply_limits(entries, now=now, ttl_seconds=ttl_seconds,
                          max_entries=max_entries, max_bytes=max_bytes, deadline=deadline)
    for tool in _scandir(directory, deadline):
        path = Path(tool.path)
        if path.is_dir():
            try:
                path.rmdir()
            except TimeoutError:
                raise
            except OSError:
                pass
    return stats


def _job_parts(directory, job_id):
    """Exact known companion paths for one job; never a `job_id.*` prefix scan.

    Matching a prefix would let pruning terminal `job-a` delete a live
    `job-a.retry.json`, so only the suffixes written by `job_ops`/`job_logs`
    (plus their rotation-compatible `.prev` segments) are enumerated.
    """
    parts = []
    for suffix in JOB_COMPANION_SUFFIXES:
        path = Path(directory) / (job_id + suffix)
        if os.path.lexists(path):
            parts.append(path)
    return parts


def _job_has_companions(directory, job_id, *, record_path, lock_path, deadline):
    """True when the record, a log segment, or an atomic-write temp file exists."""
    if os.path.lexists(record_path):
        return True
    for suffix in JOB_COMPANION_SUFFIXES:
        path = Path(directory) / (job_id + suffix)
        if path == lock_path:
            continue
        if os.path.lexists(path):
            return True
    temp_prefixes = (f".{job_id}.json.", f"{job_id}.json.tmp")
    for entry in _scandir(directory, deadline):
        if entry.name.startswith(temp_prefixes):
            return True
    return False


def _unlink_stale_lock(directory, job_id, record_path, lock_path, *, now, deadline):
    """Unlink one stale lock while holding the strongest fence available.

    POSIX keeps `flock` until after `unlink`. Windows relies on `DeleteFileW`'s kernel
    sharing check: an open `.lock` yields `ERROR_SHARING_VIOLATION` (`PermissionError`).
    """
    if os.name == "nt":
        try:
            current = os.lstat(lock_path)
        except FileNotFoundError:
            return 0
        except TimeoutError:
            raise
        except OSError:
            return 0
        if now - current.st_mtime <= STALE_JOB_LOCK_SECONDS:
            return 0
        if _job_has_companions(directory, job_id, record_path=record_path,
                               lock_path=lock_path, deadline=deadline):
            return 0
        try:
            os.unlink(lock_path)
        except TimeoutError:
            raise
        except OSError:
            return 0
        return 1
    try:
        handle = lock_path.open("rb")
    except FileNotFoundError:
        return 0
    except TimeoutError:
        raise
    except OSError:
        return 0
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # a live record_lock owns this inode: keep
        except TimeoutError:
            raise
        except OSError:
            return 0
        try:
            try:
                own = os.fstat(handle.fileno())
                current = os.lstat(lock_path)
            except FileNotFoundError:
                return 0
            except TimeoutError:
                raise
            except OSError:
                return 0
            if (own.st_dev, own.st_ino) != (current.st_dev, current.st_ino):
                return 0  # the entry was replaced after open: keep the live lock
            if now - current.st_mtime <= STALE_JOB_LOCK_SECONDS:
                return 0
            if _job_has_companions(directory, job_id, record_path=record_path,
                                   lock_path=lock_path, deadline=deadline):
                return 0
            try:
                os.unlink(lock_path)
            except TimeoutError:
                raise
            except OSError:
                return 0
            return 1
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _acquire_record_lock(path, timeout_seconds):
    """Enter `record_lock` or return None on contention/cancellation.

    Only acquisition errors are converted; file operations performed inside the
    returned lock keep their own TimeoutError/OSError semantics.
    """
    lock = record_lock(path, timeout_seconds=max(0.01, float(timeout_seconds)))
    try:
        lock.__enter__()
    except (TimeoutError, RemoteExecutionError):
        return None
    except OSError:
        return None  # opportunistic maintenance: an unusable fence means keep
    return lock


def _prune_stale_job_locks(directory, *, now, deadline):
    """Remove only stale, unlocked `.json.lock` inodes whose record is absent.

    The in-process `RLock` is held across the OS lock, companion re-check, and `unlink`, so a
    same-process writer cannot race the old inode. On POSIX `unlink` precedes `flock` release;
    a `record_lock` that already opened the old inode then re-opens the live directory entry.
    """
    removed = 0
    for entry in _scandir(directory, deadline):
        if time.monotonic() >= deadline:
            break
        if not entry.name.endswith(".json.lock"):
            continue
        job_id = entry.name[:-len(".json.lock")]
        if not _JOB_ID_RE.fullmatch(job_id):
            continue
        record_path = Path(directory) / (job_id + ".json")
        if os.path.lexists(record_path):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except TimeoutError:
            raise
        except OSError:
            continue
        if now - info.st_mtime <= STALE_JOB_LOCK_SECONDS:
            continue
        lock_path = Path(entry.path)
        thread_lock = try_record_thread_lock(record_path)
        if thread_lock is None:
            continue  # a same-process record_lock owner exists: keep
        try:
            if os.name == "nt" or fcntl is not None:
                removed += _unlink_stale_lock(
                    directory, job_id, record_path, lock_path, now=now, deadline=deadline
                )
        finally:
            thread_lock.release()
    return removed


def _prune_jobs(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes,
                protect_job_ids=()):
    protected = {str(value) for value in protect_job_ids}
    terminated, errors = [], []
    for entry in _scandir(directory, deadline):
        if time.monotonic() >= deadline:
            break
        name = entry.name
        if not name.endswith(".json") or not entry.is_file(follow_symlinks=False):
            continue
        job_id = name[:-5]
        if job_id in protected:
            continue  # an active transaction owns this record
        if not _JOB_ID_RE.fullmatch(job_id):
            continue
        path = Path(entry.path)
        record = _json_record(path)
        if record is None:
            errors.append(f"unreadable job record: {path.name}")
            continue
        if (str(record.get("state") or "") not in TERMINAL_LOCAL_JOB_STATES
                or record.get("unknown")):
            continue  # running/unknown/prepared records are never candidates
        try:
            info = entry.stat(follow_symlinks=False)
        except TimeoutError:
            raise
        except OSError:
            continue
        parts = _job_parts(directory, job_id)
        size = 0
        for part in parts:
            try:
                part_info = part.stat(follow_symlinks=False)
            except TimeoutError:
                raise
            except OSError:
                continue
            if stat.S_ISREG(part_info.st_mode):
                size += part_info.st_size
        terminated.append({"path": path, "job_id": job_id, "parts": parts,
                           "mtime": info.st_mtime, "size": size})
    stats = {"removed": 0, "freed_bytes": 0, "errors": errors,
             "skipped_locked": 0, "deferred": 0, "stale_locks_removed": 0}
    expired = {row["job_id"] for row in terminated if now - row["mtime"] > ttl_seconds}
    kept = [row for row in terminated if row["job_id"] not in expired]
    removed = set(expired)
    kept_bytes = sum(row["size"] for row in kept)
    for row in sorted(terminated, key=lambda item: (item["mtime"], item["job_id"])):
        if row["job_id"] in removed:
            continue
        if len(kept) <= max_entries and kept_bytes <= max_bytes:
            break
        if time.monotonic() >= deadline:
            break
        removed.add(row["job_id"])
        kept = [item for item in kept if item["job_id"] != row["job_id"]]
        kept_bytes -= row["size"]
    for row in terminated:
        if row["job_id"] not in removed:
            continue
        if time.monotonic() >= deadline:
            stats["deferred"] += 1
            continue
        remaining = max(0.01, min(JOB_LOCK_WAIT_SECONDS, deadline - time.monotonic()))
        lock = _acquire_record_lock(row["path"], remaining)
        if lock is None:
            stats["skipped_locked"] += 1
            continue
        try:
            # Re-verify inside the exclusive lock: a concurrent writer may have
            # resumed the record between candidate collection and deletion.
            live = _json_record(row["path"])
            if live is None or (str(live.get("state") or "")
                                not in TERMINAL_LOCAL_JOB_STATES or live.get("unknown")):
                stats["skipped_locked"] += 1
                continue
            freed = 0
            failed = False
            for part in _job_parts(directory, row["job_id"]):
                if part == row["path"]:
                    continue  # remove the record last
                freed += _remove(part)
                if os.path.lexists(part):
                    stats["errors"].append(
                        f"remove {row['job_id']}: {part.name} still present"
                    )
                    failed = True
            if failed:
                continue
            freed += _remove(row["path"])
            if os.path.lexists(row["path"]):
                stats["errors"].append(f"remove {row['job_id']}: record still present")
                continue
            stats["removed"] += 1
            stats["freed_bytes"] += freed
        finally:
            lock.__exit__(None, None, None)
    # Never unlink an active shared lock inode during deletion; only reap old,
    # unlocked lock files whose record is already gone.
    stats["stale_locks_removed"] = _prune_stale_job_locks(
        directory, now=now, deadline=deadline
    )
    return stats


def _write_marker(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                         dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _marker_recent(marker, now, cooldown_seconds):
    record = _json_record(marker)
    if record is None:
        return False
    try:
        previous = float(record.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= now - previous < cooldown_seconds


def _protected_context_names(directory):
    protected = {"latest.json"}
    latest = _json_record(Path(directory) / "latest.json")
    if isinstance(latest, dict):
        refs = latest.get("refs")
        if isinstance(refs, dict):
            full_probe = refs.get("full_probe")
            if isinstance(full_probe, str) and full_probe:
                protected.add(Path(full_probe).name)
        created_at = latest.get("created_at")
        if isinstance(created_at, str) and created_at:
            stamp = created_at.replace(":", "").replace("-", "")
            protected.add(f"context-{stamp}.json")
    return protected


def _prune_context(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes):
    protected = _protected_context_names(directory)
    entries = []
    for item in _scandir(directory, deadline):
        if item.name.startswith(".") or item.name in protected:
            continue
        try:
            if not item.is_file(follow_symlinks=False) or not item.name.endswith(".json"):
                continue
            info = item.stat(follow_symlinks=False)
            entries.append({"path": Path(item.path), "mtime": info.st_mtime, "size": info.st_size})
        except TimeoutError:
            raise
        except OSError:
            continue
    return _apply_limits(
        entries, now=now, ttl_seconds=ttl_seconds,
        max_entries=max_entries, max_bytes=max_bytes, deadline=deadline,
    )


def _prune_artifacts(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes):
    entries = []
    min_age = ARTIFACTS_MIN_AGE_SECONDS if ttl_seconds > 0 else 0
    for item in _scandir(directory, deadline):
        if item.name.startswith(".") or item.name.endswith(".lock"):
            continue
        try:
            info = item.stat(follow_symlinks=False)
            if min_age and 0 <= now - info.st_mtime < min_age:
                continue  # protect young staging directories
            path = Path(item.path)
            size = (info.st_size if stat.S_ISREG(info.st_mode)
                    else _entry_size(path, deadline))
            entries.append({"path": path, "mtime": info.st_mtime, "size": size})
        except TimeoutError:
            raise
        except OSError:
            continue
    removed, freed, skipped_locked = 0, 0, 0
    expired = {entry["path"] for entry in entries if now - entry["mtime"] > ttl_seconds}
    kept = [entry for entry in entries if entry["path"] not in expired]
    kept_bytes = sum(entry["size"] for entry in kept)
    for entry in sorted(entries, key=lambda row: (row["mtime"], str(row["path"]))):
        if time.monotonic() >= deadline:
            break
        over_limit = len(kept) > max_entries or kept_bytes > max_bytes
        if entry["path"] not in expired and not over_limit:
            break
        lock = _acquire_record_lock(entry["path"], 0.01)
        if lock is None:
            skipped_locked += 1
            continue  # active transfer holds `record_lock(base)`: never delete!
        try:
            freed += _remove(entry["path"])
            if not os.path.lexists(entry["path"]):
                removed += 1
                if entry["path"] not in expired:
                    kept = [row for row in kept if row["path"] != entry["path"]]
                    kept_bytes -= entry["size"]
        finally:
            lock.__exit__(None, None, None)
    for item in _scandir(directory, deadline):
        if time.monotonic() >= deadline or not item.name.endswith(".lock"):
            continue
        target_name = item.name[:-5]
        target_path = Path(directory) / target_name
        if not target_name or os.path.lexists(target_path):
            continue
        thread_lock = try_record_thread_lock(target_path)
        if thread_lock is None:
            continue
        try:
            if os.name == "nt" or fcntl is not None:
                _unlink_stale_lock(
                    directory, target_name, target_path, Path(item.path), now=now, deadline=deadline
                )
        finally:
            thread_lock.release()
    return {"removed": removed, "freed_bytes": freed, "skipped_locked": skipped_locked}


def _prune_patches(directory, *, now, deadline, ttl_seconds, max_entries, max_bytes):
    entries = []
    for item in _scandir(directory, deadline):
        if item.name.startswith("."):
            continue
        try:
            if not item.is_file(follow_symlinks=False) or not item.name.endswith(".json"):
                continue
            info = item.stat(follow_symlinks=False)
            entries.append({"path": Path(item.path), "mtime": info.st_mtime, "size": info.st_size})
        except TimeoutError:
            raise
        except OSError:
            continue
    return _apply_limits(
        entries, now=now, ttl_seconds=ttl_seconds,
        max_entries=max_entries, max_bytes=max_bytes, deadline=deadline,
    )


def prune_endpoint_state(
    base,
    *,
    now=None,
    force=False,
    cooldown_seconds=PRUNE_COOLDOWN_SECONDS,
    time_budget_seconds=PRUNE_TIME_BUDGET_SECONDS,
    reads_ttl_seconds=READS_TTL_SECONDS,
    reads_max_entries=READS_MAX_ENTRIES,
    reads_max_bytes=READS_MAX_BYTES,
    logs_ttl_seconds=LOGS_TTL_SECONDS,
    logs_max_entries=LOGS_MAX_ENTRIES,
    logs_max_bytes=LOGS_MAX_BYTES,
    jobs_ttl_seconds=JOBS_TTL_SECONDS,
    jobs_max_entries=JOBS_MAX_ENTRIES,
    jobs_max_bytes=JOBS_MAX_BYTES,
    context_ttl_seconds=CONTEXT_TTL_SECONDS,
    context_max_entries=CONTEXT_MAX_ENTRIES,
    context_max_bytes=CONTEXT_MAX_BYTES,
    artifacts_ttl_seconds=ARTIFACTS_TTL_SECONDS,
    artifacts_max_entries=ARTIFACTS_MAX_ENTRIES,
    artifacts_max_bytes=ARTIFACTS_MAX_BYTES,
    patches_ttl_seconds=PATCHES_TTL_SECONDS,
    patches_max_entries=PATCHES_MAX_ENTRIES,
    patches_max_bytes=PATCHES_MAX_BYTES,
    protect_job_ids=(),
):
    """Prune one endpoint state directory under a cooldown and bounded budget.

    `protect_job_ids` names records owned by the caller's current transaction;
    they are never candidates even if their persisted state looks terminal.
    """
    base = Path(base)
    now = time.time() if now is None else float(now)
    if not base.is_dir():
        return {"skipped": "missing_endpoint_state", "base": str(base)}
    marker = base / ".prune.json"
    if not force and _marker_recent(marker, now, cooldown_seconds):
        return {"skipped": "cooldown", "cooldown_seconds": cooldown_seconds}
    deadline = time.monotonic() + max(0.1, float(time_budget_seconds))
    stats = {"base": str(base), "at": now}
    stats["reads"] = _prune_reads(
        base / "reads", now=now, deadline=deadline, ttl_seconds=reads_ttl_seconds,
        max_entries=reads_max_entries, max_bytes=reads_max_bytes,
    )
    stats["logs"] = _prune_logs(
        base / "logs", now=now, deadline=deadline, ttl_seconds=logs_ttl_seconds,
        max_entries=logs_max_entries, max_bytes=logs_max_bytes,
    )
    stats["jobs"] = _prune_jobs(
        base / "jobs", now=now, deadline=deadline, ttl_seconds=jobs_ttl_seconds,
        max_entries=jobs_max_entries, max_bytes=jobs_max_bytes,
        protect_job_ids=protect_job_ids,
    )
    stats["context"] = _prune_context(
        base / "context", now=now, deadline=deadline, ttl_seconds=context_ttl_seconds,
        max_entries=context_max_entries, max_bytes=context_max_bytes,
    )
    stats["artifacts"] = _prune_artifacts(
        base / "artifacts", now=now, deadline=deadline, ttl_seconds=artifacts_ttl_seconds,
        max_entries=artifacts_max_entries, max_bytes=artifacts_max_bytes,
    )
    stats["patches"] = _prune_patches(
        base / "patches", now=now, deadline=deadline, ttl_seconds=patches_ttl_seconds,
        max_entries=patches_max_entries, max_bytes=patches_max_bytes,
    )
    try:
        _write_marker(marker, {
            "schema_version": "remote-dev.prune.v1",
            "at": now,
            "reads_removed": stats["reads"]["removed"],
            "logs_removed": stats["logs"]["removed"],
            "jobs_removed": stats["jobs"]["removed"],
            "context_removed": stats["context"]["removed"],
            "artifacts_removed": stats["artifacts"]["removed"],
            "patches_removed": stats["patches"]["removed"],
        })
    except TimeoutError:
        raise
    except OSError as exc:
        stats["marker_error"] = f"{type(exc).__name__}: {exc}"
    return stats
