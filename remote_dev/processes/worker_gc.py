# Concatenated after processes/worker_runtime.py by remote_dev.processes.client.worker_source().
"""Bounded terminal-job garbage collection for the remote job supervisor (`worker_gc.py`).

[Role] Housekeeping companion: paginated terminal-job classification, lock-held atomic
eviction, and cooldown/cursor state for the remote `.remote-dev/jobs` directory.
[Invariants]
- Removal requires same remote UID, a terminal `result.json` state, an empty `unknown`
  field, no `retain_until_release`/`retain` marker, drained descendants or a freshly
  observed quiet status, and an exclusive `worker.lock` + `lock` fence held across
  re-verification and `os.replace(job_dir, trash_dir)`. `running`, `unknown`, and retained
  directories are never removed.
- A persisted `cursor` advances past an uncollectable sorted prefix and wraps to `""`,
  so entries beyond the first `HOUSEKEEPING_MAX_ENTRIES` are not starved.
- `dry_run=True` never renames or removes job directories and never `rmtree`s `.trash-*`;
  it only counts/reports `would_remove`/`trash_would_clean`.
[Timeouts & Locks]
- One monotonic `time_budget_seconds` (default `2.0s`) covers directory enumeration and the
  deletion loop; `max_entries` caps classification work, and each fence acquisition is
  non-blocking. Lock files are only created during a real (non-dry-run) eviction.
[Side-Effect Policy]
- Writes only inside the `.remote-dev/jobs` parent: renames owned terminal directories to
  `.trash-*`, removes old owned `.trash-*` directories, and writes `.housekeeping.json`.
  Never signals a process.
"""
import contextlib
import fcntl
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

# `_read_json_dict`, `_write_json_atomic`, `_tree_size`, and the log helpers are
# defined by the concatenated `worker_runtime.py` module immediately above this one.
_JOB_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
_JOBS_DIRNAME = ".remote-dev"
TERMINAL_REMOTE_STATES = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "timeout", "timed_out"}
)
TERMINAL_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TERMINAL_JOBS = 128
MAX_TERMINAL_BYTES = 2 * 1024 * 1024 * 1024
HOUSEKEEPING_COOLDOWN_SECONDS = 300
HOUSEKEEPING_TIME_BUDGET_SECONDS = 2.0
HOUSEKEEPING_MAX_ENTRIES = 512
HOUSEKEEPING_MAX_FILES = 10000


@contextlib.contextmanager
def _held_job_locks(directory, *, create=True):
    """Hold exclusive `worker.lock` + `lock` fences; yield False if unavailable.

    Both fences are opened and locked non-blockingly and stay held across the
    terminal re-check and atomic `os.replace` into `.trash-*`, closing the
    probe-then-delete TOCTOU window a concurrent tail/exchange reader could win.
    `create=True` also closes the race where a control actor creates the lock
    file after the check; dry runs keep `create=False` and stay read-only.
    """
    handles = []
    acquired = True
    try:
        for name in ("worker.lock", "lock"):
            path = directory / name
            try:
                handle = path.open("a+b" if create else "r+b")
            except FileNotFoundError:
                continue
            except TimeoutError:
                raise
            except OSError:
                acquired = False
                break
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                acquired = False
                break
            except TimeoutError:
                handle.close()
                raise
            except OSError:
                handle.close()
                acquired = False
                break
            handles.append(handle)
        yield acquired
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _retained(directory, result):
    for key in ("retain_until_release", "retain"):
        if result.get(key):
            return True
    for name in ("spec.json", "receipt.json"):
        row = _read_json_dict(directory / name)
        if row.get("retain_until_release") or row.get("retain"):
            return True
    return (directory / "retain.json").exists()


def _job_quiet(directory):
    checker = globals().get("job_status")
    if checker is None:
        return False  # cannot prove quiet: never delete
    try:
        status = checker(directory)
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError):
        return False
    return bool(status.get("quiet")) and not status.get("unknown")


def _tree_size(directory, *, max_files=HOUSEKEEPING_MAX_FILES, deadline=None):
    total, files, pending = 0, 0, [str(directory)]
    while pending:
        if deadline is not None and time.monotonic() >= deadline:
            return total, files, False
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except TimeoutError:
            raise
        except OSError:
            continue
        with entries:
            for entry in entries:
                if files >= max_files or (deadline is not None and time.monotonic() >= deadline):
                    return total, files, False
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
    return total, files, True


def _remove_terminal(directory, stats, *, dry_run):
    """Report a dry-run candidate, or atomically move a locked job to `.trash-*`."""
    if dry_run:
        stats["would_remove"].append(directory.name)
        return None
    trash = directory.with_name(
        ".trash-" + directory.name + "-" + uuid.uuid4().hex[:8]
    )
    os.replace(directory, trash)
    stats["removed"].append(directory.name)
    return trash


def _select_job_page(names, cursor, max_entries):
    """Return one sorted page after `cursor`, wrapping once at the end."""
    names = sorted(names)
    if cursor:
        tail = [name for name in names if name > cursor]
        head = [name for name in names if name <= cursor]
    else:
        tail, head = names, []
    page = tail[:max_entries]
    wrapped = False
    if cursor and len(page) < max_entries:
        page.extend(head[:max_entries - len(page)])
        wrapped = True
    if not cursor and len(page) >= len(names):
        next_cursor = ""
    elif wrapped:
        next_cursor = ""
    else:
        next_cursor = page[-1] if page else cursor
    return page, next_cursor, wrapped


def collect_terminal_jobs(
    root=None,
    *,
    jobs_root=None,
    now=None,
    ttl_seconds=TERMINAL_TTL_SECONDS,
    max_terminal_jobs=MAX_TERMINAL_JOBS,
    max_terminal_bytes=MAX_TERMINAL_BYTES,
    exclude=(),
    dry_run=False,
    max_entries=HOUSEKEEPING_MAX_ENTRIES,
    cursor="",
    time_budget_seconds=HOUSEKEEPING_TIME_BUDGET_SECONDS,
):
    """Bounded GC over owned, quiet, definitively terminal remote job directories.

    The 2s monotonic budget covers directory enumeration and the delete loop.
    `cursor` is the last fully classified job name; the next cycle starts after
    it and wraps to the beginning, so an uncollectable prefix cannot starve
    entries beyond the first `max_entries`.
    """
    now = time.time() if now is None else float(now)
    deadline = time.monotonic() + max(0.05, float(time_budget_seconds))
    base = Path(jobs_root) if jobs_root is not None else Path(root) / _JOBS_DIRNAME / "jobs"
    excluded = {str(name) for name in exclude}
    cursor = str(cursor or "")
    stats = {
        "jobs_root": str(base),
        "scanned": 0,
        "terminal": 0,
        "removed": [],
        "would_remove": [],
        "removed_bytes": 0,
        "trash_cleaned": 0,
        "trash_would_clean": 0,
        "skipped_active": 0,
        "skipped_unknown": 0,
        "skipped_retained": 0,
        "skipped_unquiet": 0,
        "skipped_unowned": 0,
        "skipped_raced": 0,
        "deferred": 0,
        "truncated": False,
        "deadline_exceeded": False,
        "cursor": cursor,
        "last_cursor": cursor,
        "next_cursor": cursor,
        "cursor_wrapped": False,
        "errors": [],
    }
    entries = []
    scan_complete = True
    try:
        with os.scandir(base) as iterator:
            for entry in iterator:
                if time.monotonic() >= deadline:
                    scan_complete = False
                    stats["deadline_exceeded"] = True
                    break
                entries.append(entry)
    except FileNotFoundError:
        return stats
    except TimeoutError:
        raise
    except OSError as exc:
        stats["errors"].append(f"scan {base}: {type(exc).__name__}: {exc}")
        return stats
    entries.sort(key=lambda entry: entry.name)
    uid = os.getuid() if hasattr(os, "getuid") else None
    for entry in entries:
        if time.monotonic() >= deadline:
            stats["deadline_exceeded"] = True
            break
        if not entry.name.startswith(".trash-"):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except TimeoutError:
            raise
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or entry.is_symlink():
            continue
        if uid is not None and info.st_uid != uid:
            continue
        if now - info.st_mtime < 60:
            continue  # a concurrent remover may still be walking it
        if dry_run:
            stats["trash_would_clean"] += 1
            continue
        try:
            shutil.rmtree(Path(entry.path))
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(
                f"trash {entry.name}: {type(exc).__name__}: {exc}"
            )
            continue
        stats["trash_cleaned"] += 1
    entries_by_name = {entry.name: entry for entry in entries}
    candidates = []
    page, next_cursor, wrapped = _select_job_page(
        [entry.name for entry in entries if _JOB_DIR_RE.fullmatch(entry.name)],
        cursor, max_entries,
    )
    stats["cursor_wrapped"] = wrapped
    if scan_complete:
        stats["truncated"] = len(page) < len(
            [entry.name for entry in entries if _JOB_DIR_RE.fullmatch(entry.name)]
        )
    else:
        stats["truncated"] = True
    last_processed = None
    page_complete = True
    for name in page:
        if time.monotonic() >= deadline:
            page_complete = False
            stats["deadline_exceeded"] = True
            break
        entry = entries_by_name.get(name)
        if entry is None:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except TimeoutError:
            raise
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or entry.is_symlink():
            continue
        if uid is not None and info.st_uid != uid:
            stats["skipped_unowned"] += 1
            last_processed = name
            continue
        directory = Path(entry.path)
        stats["scanned"] += 1
        last_processed = name
        if name in excluded:
            stats["skipped_active"] += 1
            continue
        result = _read_json_dict(directory / "result.json")
        if str(result.get("state") or "") not in TERMINAL_REMOTE_STATES:
            stats["skipped_active"] += 1
            continue
        if result.get("unknown"):
            stats["skipped_unknown"] += 1
            continue
        if _retained(directory, result):
            stats["skipped_retained"] += 1
            continue
        if result.get("descendants_drained") is not True and not _job_quiet(directory):
            stats["skipped_unquiet"] += 1
            continue
        try:
            finished_at = float(result.get("finished_at"))
        except (TypeError, ValueError):
            finished_at = info.st_mtime
        size, _files, complete = _tree_size(directory, deadline=deadline)
        if not complete:
            stats["truncated"] = True
        candidates.append({"name": name, "path": directory,
                           "terminal_at": finished_at, "bytes": size})
        stats["terminal"] += 1
    if not scan_complete:
        stats["next_cursor"] = cursor
    elif not page_complete:
        # Do not advance past names this pass did not classify; a wrapped page
        # restarts from the beginning because it already contains head entries.
        stats["next_cursor"] = "" if wrapped else (last_processed or cursor)
    else:
        stats["next_cursor"] = next_cursor
    stats["last_cursor"] = stats["next_cursor"]
    if not candidates:
        return stats
    expired = {
        row["name"] for row in candidates
        if now - row["terminal_at"] > ttl_seconds and row["name"] not in excluded
    }
    remove = set(expired)
    kept = [row for row in candidates if row["name"] not in remove]
    kept_bytes = sum(row["bytes"] for row in kept)
    for row in sorted(candidates, key=lambda item: (item["terminal_at"], item["name"])):
        if row["name"] in remove:
            continue
        if len(kept) <= max_terminal_jobs and kept_bytes <= max_terminal_bytes:
            break
        remove.add(row["name"])
        kept = [item for item in kept if item["name"] != row["name"]]
        kept_bytes -= row["bytes"]
    for row in candidates:
        if row["name"] not in remove:
            continue
        if time.monotonic() >= deadline:
            stats["deferred"] += 1
            stats["deadline_exceeded"] = True
            continue
        trash = None
        try:
            with _held_job_locks(row["path"], create=not dry_run) as locked:
                if not locked:
                    stats["skipped_active"] += 1
                    continue
                # Re-verify every terminal invariant while the fences are held;
                # candidate classification above ran without them.
                result = _read_json_dict(row["path"] / "result.json")
                if str(result.get("state") or "") not in TERMINAL_REMOTE_STATES:
                    stats["skipped_raced"] += 1
                    continue
                if result.get("unknown"):
                    stats["skipped_unknown"] += 1
                    continue
                if _retained(row["path"], result):
                    stats["skipped_retained"] += 1
                    continue
                # `descendants_drained is True` is the definitive quiet
                # observation; otherwise ask the supervisor status again.
                if result.get("descendants_drained") is not True and not _job_quiet(row["path"]):
                    stats["skipped_unquiet"] += 1
                    continue
                if row["name"] in excluded:
                    stats["skipped_active"] += 1
                    continue
                trash = _remove_terminal(row["path"], stats, dry_run=dry_run)
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(
                f"remove {row['name']}: {type(exc).__name__}: {exc}"
            )
            continue
        if trash is None:
            continue
        stats["removed_bytes"] += row["bytes"]
        try:
            shutil.rmtree(trash)
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(
                f"trash {trash.name}: {type(exc).__name__}: {exc}"
            )
    return stats


def maybe_run_job_housekeeping(
    root, *, exclude=(), force=False, now=None,
    time_budget_seconds=HOUSEKEEPING_TIME_BUDGET_SECONDS,
    max_entries=HOUSEKEEPING_MAX_ENTRIES,
):
    """Run terminal-job GC under a cooldown; failures never fail job control."""
    now = time.time() if now is None else float(now)
    base = Path(root) / _JOBS_DIRNAME / "jobs"
    marker = base / ".housekeeping.json"
    try:
        # The whole maintenance check, including the cooldown marker read, is
        # inside this bounded boundary: a damaged/blocked marker must degrade
        # prepare/status/stop with `maintenance_warning`, never abort it.
        marker_record = _read_json_dict(marker)
        if not force:
            try:
                previous = float(marker_record.get("at") or 0)
            except (TypeError, ValueError):
                previous = 0.0
            if 0 <= now - previous < HOUSEKEEPING_COOLDOWN_SECONDS:
                return {"skipped": "cooldown",
                        "cooldown_seconds": HOUSEKEEPING_COOLDOWN_SECONDS}
        stats = collect_terminal_jobs(
            jobs_root=base, now=now, exclude=exclude,
            cursor=str(marker_record.get("last_cursor")
                       or marker_record.get("cursor") or ""),
            time_budget_seconds=time_budget_seconds, max_entries=max_entries,
        )
    except TimeoutError as exc:
        # Housekeeping is opportunistic: report the timeout instead of failing
        # the job preparation/stop/status control that triggered it.
        return {"skipped": "timeout", "error": f"TimeoutError: {exc}"}
    except OSError as exc:
        return {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}
    except (TypeError, ValueError) as exc:
        return {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}
    try:
        _write_json_atomic(marker, {
            "schema_version": "remote-dev.job.housekeeping.v1",
            "at": now,
            "last_cursor": stats.get("next_cursor", ""),
            "cursor": stats.get("next_cursor", ""),
            "scanned": stats.get("scanned", 0),
            "deferred": stats.get("deferred", 0),
            "deadline_exceeded": bool(stats.get("deadline_exceeded")),
            "removed": stats["removed"][:32],
            "errors": stats["errors"][:8],
        })
    except TimeoutError as exc:
        stats["errors"].append(f"housekeeping marker: TimeoutError: {exc}")
    except OSError as exc:
        stats["errors"].append(f"housekeeping marker: {type(exc).__name__}: {exc}")
    return stats


def maintenance_warning(stats):
    """Return a human-readable warning for a degraded maintenance pass, else None."""
    if not isinstance(stats, dict):
        return None
    if stats.get("skipped") in {"timeout", "error"}:
        return str(stats.get("error") or stats["skipped"])
    errors = stats.get("errors") or []
    if errors:
        return "; ".join(str(item) for item in errors[:2])
    if stats.get("deadline_exceeded") or stats.get("deferred"):
        return "housekeeping time budget exhausted; remaining work is deferred"
    return None


def attach_maintenance_warning(status, stats):
    """Attach a bounded maintenance warning to a control status without failing it."""
    warning = maintenance_warning(stats)
    if warning and isinstance(status, dict):
        status = {**status, "maintenance_warning": warning}
    return status
