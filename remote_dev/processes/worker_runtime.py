# Prepended before processes/worker.py by remote_dev.processes.client.worker_source().
"""Bounded remote job logs, process identity, and terminal-job garbage collection.

[Role] Runtime companion for the remote job supervisor (`worker_runtime.py`): Linux
process identity/ownership, the capped ring log writer/reader, and bounded terminal-job
garbage collection.
[Invariants]
- `process_identity()` matches PID start ticks so a recycled PID is never signaled; an
  unreadable `/proc` observation stays `unknown` instead of being treated as absent.
- Terminal-job removal requires: same remote UID, no held `worker.lock`/`lock` fence, a
  `result.json` terminal state (`succeeded`/`failed`/`timeout`/`timed_out`/`cancelled`/
  `completed`), an empty `unknown` field, and either `descendants_drained is True` or a
  freshly observed `quiet` status. `running`, `starting`, `stopping`, `unknown`, and
  `retain_until_release` directories are never removed.
- A capped stream retains at most `max_log_bytes` on disk (`<stream>.log.prev` plus
  `<stream>.log`); `logical_bytes_written`/`truncated_head_bytes` keep cursors honest when
  the head has been rotated away instead of silently losing offsets.
[Timeouts & Locks]
- `logs.lock` (`flock` SH for readers, EX for append/rotate/close) makes metadata, sizes,
  and bytes one consistent snapshot across the supervisor and the control RPC worker.
- Housekeeping is bounded to `HOUSEKEEPING_MAX_ENTRIES` directory observations and runs
  at most once per `HOUSEKEEPING_COOLDOWN_SECONDS` unless forced.
[Side-Effect Policy]
- Writes only inside the assigned job directory and renames only owned terminal job
  directories under the same `.remote-dev/jobs` parent; never signals a process from
  housekeeping and never reads or rewrites job output beyond log windows.
"""
import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

# worker.py defines these before any runtime call; the defaults keep this module
# independently importable for focused unit tests.
JOB_TOKEN_ENV = globals().get("JOB_TOKEN_ENV", "REMOTE_DEV_JOB_TOKEN")
_JOB_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
_JOBS_DIRNAME = ".remote-dev"
DEFAULT_MAX_LOG_BYTES = 256 * 1024 * 1024
LOGS_SCHEMA_VERSION = "remote-dev.job.logs.v1"
TERMINAL_REMOTE_STATES = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "timeout", "timed_out"}
)
TERMINAL_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TERMINAL_JOBS = 128
MAX_TERMINAL_BYTES = 2 * 1024 * 1024 * 1024
HOUSEKEEPING_COOLDOWN_SECONDS = 300
HOUSEKEEPING_MAX_ENTRIES = 512
HOUSEKEEPING_MAX_FILES = 10000


def _utf8_incomplete_tail(chunk):
    """Trailing bytes (0-3) of an unfinished UTF-8 sequence; invalid tails return 0."""
    size = len(chunk)
    for back in range(1, min(4, size) + 1):
        byte = chunk[size - back]
        if byte & 0xC0 == 0x80:
            continue  # continuation byte, keep scanning for the lead byte
        if byte < 0x80:
            return 0  # ASCII: the chunk ends on a complete character
        if 0xC2 <= byte <= 0xDF:
            needed = 2
        elif 0xE0 <= byte <= 0xEF:
            needed = 3
        elif 0xF0 <= byte <= 0xF4:
            needed = 4
        else:
            return 0  # invalid lead byte: not an incomplete sequence
        return back if back < needed else 0
    return 0


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "ppid": int(fields[1]), "pgid": int(fields[2]),
                "start_ticks": fields[19], "state": fields[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def supervised_family(receipt):
    """Walk the verified subreaper's kernel child lists, including all threads."""
    anchor = process_identity(receipt["pid"])
    if not anchor or anchor["state"] == "Z" or anchor["start_ticks"] != receipt["start_ticks"]:
        return None
    found, pending, unknown = {anchor["pid"]: anchor}, [anchor["pid"]], []
    while pending:
        pid = pending.pop()
        try:
            tasks = Path(f"/proc/{pid}/task")
            for task in tasks.iterdir():
                try:
                    children = (task / "children").read_text(encoding="utf-8").split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                for child_pid in children:
                    row = process_identity(child_pid)
                    if row and row["state"] != "Z" and row["ppid"] == pid and row["pid"] not in found:
                        found[row["pid"]] = row
                        pending.append(row["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            unknown.append(f"cannot observe children of {pid}")
    return list(found.values()), unknown


def owned_processes(receipt):
    """Observe marker ownership and ancestry below the verified subreaper.

    Ancestry covers setsid/clean-environment daemons; unowned process-group
    members are ambiguous, not free. The marker identifies, never authorizes.
    """
    if receipt["boot_id"] != boot_id():
        return [], ["boot identity changed"]
    if receipt.get("supervision") == "subreaper":
        family = supervised_family(receipt)
        if family is not None:
            return family
    identities, tagged, unknown = {}, set(), []
    marker = (JOB_TOKEN_ENV + "=" + receipt["marker"]).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        identity = None
        try:
            identity = process_identity(entry.name)
            if not identity or identity["state"] == "Z":
                continue
            identities[identity["pid"]] = identity
            environ = (entry / "environ").read_bytes().split(b"\0")
            if marker in environ:
                tagged.add(identity["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Other users' processes are not family; readable ancestry still helps.
            if identity is None:
                unknown.append(int(entry.name))
    anchor = identities.get(receipt["pid"])
    if receipt.get("supervision") == "subreaper" and anchor and anchor["start_ticks"] == receipt["start_ticks"]:
        family = {anchor["pid"]}
        while True:
            descendants = {pid for pid, row in identities.items() if row["ppid"] in family}
            if descendants.issubset(family):
                break
            family.update(descendants)
        tagged.update(family)
    unknown.extend(pid for pid, row in identities.items() if row["pgid"] == receipt["pgid"] and pid not in tagged)
    return [identities[pid] for pid in sorted(tagged)], unknown


def signal_processes(processes, sig, *, exclude=None):
    for process in processes:
        if process["pid"] == exclude:
            continue
        current = process_identity(process["pid"])
        if current and current["start_ticks"] == process["start_ticks"]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process["pid"], sig)


def _file_size(path):
    try:
        return max(0, path.stat().st_size)
    except FileNotFoundError:
        return 0


def _read_json_dict(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_logs_meta(directory):
    return _read_json_dict(Path(directory) / "logs.json")


def _positive_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _log_snapshot(directory, stream):
    """Metadata plus retained segment sizes for one stream (caller may hold the lock)."""
    directory = Path(directory)
    stored = _read_logs_meta(directory).get(stream)
    stored = stored if isinstance(stored, dict) else {}
    truncated = _positive_int(stored.get("truncated_head_bytes"))
    prev_size = _file_size(directory / (stream + ".log.prev"))
    current_size = _file_size(directory / (stream + ".log"))
    logical_size = max(
        truncated + prev_size + current_size,
        _positive_int(stored.get("logical_bytes_written")),
    )
    return {
        "truncated_head_bytes": truncated,
        "prev_size": prev_size,
        "current_size": current_size,
        "logical_bytes": logical_size,
    }


def log_stream_size(directory, stream):
    """Current logical byte count for one stream's retained ring plus dropped head."""
    directory = Path(directory)
    lock_path = directory / "logs.lock"
    try:
        handle = lock_path.open("r+b")
    except FileNotFoundError:
        return _log_snapshot(directory, stream)["logical_bytes"]
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        return _log_snapshot(directory, stream)["logical_bytes"]
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class CappedLog:
    """Append-only ring log with honest logical-vs-retained byte accounting."""

    def __init__(self, directory, stream, max_bytes):
        self.directory = Path(directory)
        self.stream = stream
        self.path = self.directory / (stream + ".log")
        self.prev_path = self.directory / (stream + ".log.prev")
        self.meta_path = self.directory / "logs.json"
        self.lock_path = self.directory / "logs.lock"
        self.max_bytes = max(2, int(max_bytes))
        self._handle = None
        stored = _read_logs_meta(self.directory).get(stream)
        stored = stored if isinstance(stored, dict) else {}
        self.truncated_head_bytes = _positive_int(stored.get("truncated_head_bytes"))
        on_disk = _file_size(self.prev_path) + _file_size(self.path)
        self.logical_bytes_written = max(
            _positive_int(stored.get("logical_bytes_written")),
            self.truncated_head_bytes + on_disk,
        )

    @contextlib.contextmanager
    def _locked(self, mode):
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self):
        self.lock_path.touch(exist_ok=True)
        self._handle = self.path.open("ab")
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _write_meta_locked(self):
        meta = _read_logs_meta(self.directory)
        meta["schema_version"] = LOGS_SCHEMA_VERSION
        meta[self.stream] = {
            "logical_bytes_written": self.logical_bytes_written,
            "truncated_head_bytes": self.truncated_head_bytes,
        }
        _write_json_atomic(self.meta_path, meta)

    def _rotate_locked(self):
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
        dropped = _file_size(self.prev_path)
        if dropped:
            self.truncated_head_bytes += dropped
            with contextlib.suppress(FileNotFoundError):
                self.prev_path.unlink()
        os.replace(self.path, self.prev_path)
        self._write_meta_locked()
        self._handle = self.path.open("ab")

    def write(self, chunk):
        if not chunk:
            return
        if self._handle is None:
            self._handle = self.path.open("ab")
        with self._locked(fcntl.LOCK_EX):
            view = memoryview(chunk)
            half = max(1, self.max_bytes // 2)
            while view:
                room = half - _file_size(self.path)
                if room <= 0:
                    self._rotate_locked()
                    room = half
                piece = view[:max(1, min(len(view), room))]
                self._handle.write(piece)
                self._handle.flush()
                self.logical_bytes_written += len(piece)
                view = view[len(piece):]

    def close(self):
        if self._handle is None:
            return
        with self._locked(fcntl.LOCK_EX):
            self._handle.flush()
            self._handle.close()
            self._handle = None
            self._write_meta_locked()


def _read_span(prev_path, path, prev_size, truncated, start, budget):
    relative = start - truncated
    pieces = []
    remaining = budget
    if relative < prev_size and remaining > 0:
        with prev_path.open("rb") as stream:
            stream.seek(relative)
            piece = stream.read(min(remaining, prev_size - relative))
        pieces.append(piece)
        remaining -= len(piece)
    if remaining > 0 and path.exists():
        with path.open("rb") as stream:
            stream.seek(max(0, relative - prev_size))
            pieces.append(stream.read(remaining))
    return b"".join(pieces)


def read_log_window(directory, stream, start_logical, max_bytes, *, tail_bytes=None):
    """Return a consistent logical window over `<stream>.log.prev` + `<stream>.log`.

    `start_logical=None` selects the retained tail (`tail_bytes`). When the caller's
    cursor predates the retained ring, `skipped_bytes` reports the rotated-away head and
    `start` is the first byte actually returned, so callers never mistake loss for EOF.
    """
    directory = Path(directory)
    path = directory / (stream + ".log")
    prev_path = directory / (stream + ".log.prev")
    budget = max(0, int(max_bytes))
    lock_path = directory / "logs.lock"
    try:
        handle = lock_path.open("r+b")
    except FileNotFoundError:
        handle = None
    try:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        snapshot = _log_snapshot(directory, stream)
        truncated = snapshot["truncated_head_bytes"]
        prev_size = snapshot["prev_size"]
        logical_size = snapshot["logical_bytes"]
        if start_logical is None:
            length = max(0, int(tail_bytes if tail_bytes is not None else budget))
            start = max(truncated, logical_size - length)
        else:
            start = max(0, int(start_logical))
        skipped = max(0, truncated - start)
        start = max(start, truncated)
        data = _read_span(prev_path, path, prev_size, truncated, start, budget)
        return {
            "data": data,
            "start": start,
            "end": start + len(data),
            "logical_bytes": logical_size,
            "truncated_head_bytes": truncated,
            "skipped_bytes": skipped,
        }
    finally:
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _job_locks_free(directory):
    """True only when no control/supervisor flock is currently held."""
    for name in ("worker.lock", "lock"):
        path = directory / name
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except TimeoutError:
            raise
        except OSError:
            return False  # unverifiable fence: never treat it as free
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
    return True


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


def _tree_size(directory, *, max_files=HOUSEKEEPING_MAX_FILES):
    total, files, pending = 0, 0, [str(directory)]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except TimeoutError:
            raise
        except OSError:
            continue
        with entries:
            for entry in entries:
                if files >= max_files:
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
    trash = directory.with_name(
        ".trash-" + directory.name + "-" + uuid.uuid4().hex[:8]
    )
    if dry_run:
        stats["would_remove"].append(directory.name)
        return 0
    os.replace(directory, trash)
    shutil.rmtree(trash)
    stats["removed"].append(directory.name)
    return 1


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
):
    """Bounded GC over owned, quiet, definitively terminal remote job directories."""
    now = time.time() if now is None else float(now)
    base = Path(jobs_root) if jobs_root is not None else Path(root) / _JOBS_DIRNAME / "jobs"
    excluded = {str(name) for name in exclude}
    stats = {
        "jobs_root": str(base),
        "scanned": 0,
        "terminal": 0,
        "removed": [],
        "would_remove": [],
        "removed_bytes": 0,
        "trash_cleaned": 0,
        "skipped_active": 0,
        "skipped_unknown": 0,
        "skipped_retained": 0,
        "skipped_unquiet": 0,
        "skipped_unowned": 0,
        "truncated": False,
        "errors": [],
    }
    try:
        entries = list(os.scandir(base))
    except FileNotFoundError:
        return stats
    except TimeoutError:
        raise
    except OSError as exc:
        stats["errors"].append(f"scan {base}: {type(exc).__name__}: {exc}")
        return stats
    entries.sort(key=lambda entry: entry.name)
    if len(entries) > max_entries:
        entries = entries[:max_entries]
        stats["truncated"] = True
    uid = os.getuid() if hasattr(os, "getuid") else None
    for entry in entries:
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
    candidates = []
    for entry in entries:
        if not _JOB_DIR_RE.fullmatch(entry.name):
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
            continue
        directory = Path(entry.path)
        stats["scanned"] += 1
        if entry.name in excluded:
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
        if not _job_locks_free(directory):
            stats["skipped_active"] += 1
            continue
        try:
            finished_at = float(result.get("finished_at"))
        except (TypeError, ValueError):
            finished_at = info.st_mtime
        size, _files, complete = _tree_size(directory)
        if not complete:
            stats["truncated"] = True
        candidates.append({"name": entry.name, "path": directory,
                           "terminal_at": finished_at, "bytes": size})
        stats["terminal"] += 1
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
        try:
            _remove_terminal(row["path"], stats, dry_run=dry_run)
            stats["removed_bytes"] += row["bytes"]
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(
                f"remove {row['name']}: {type(exc).__name__}: {exc}"
            )
    return stats


def maybe_run_job_housekeeping(root, *, exclude=(), force=False, now=None):
    """Run terminal-job GC under a cooldown; failures stay in the marker, not job control."""
    now = time.time() if now is None else float(now)
    base = Path(root) / _JOBS_DIRNAME / "jobs"
    marker = base / ".housekeeping.json"
    if not force:
        try:
            previous = float(_read_json_dict(marker).get("at") or 0)
        except (TypeError, ValueError):
            previous = 0.0
        if 0 <= now - previous < HOUSEKEEPING_COOLDOWN_SECONDS:
            return {"skipped": "cooldown",
                    "cooldown_seconds": HOUSEKEEPING_COOLDOWN_SECONDS}
    try:
        stats = collect_terminal_jobs(jobs_root=base, now=now, exclude=exclude)
    except TimeoutError as exc:
        # Housekeeping is opportunistic: report the timeout instead of failing
        # the job preparation/stop/status control that triggered it.
        return {"skipped": "timeout", "error": f"TimeoutError: {exc}"}
    except OSError as exc:
        return {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}
    try:
        _write_json_atomic(marker, {
            "schema_version": "remote-dev.job.housekeeping.v1",
            "at": now,
            "removed": stats["removed"][:32],
            "errors": stats["errors"][:8],
        })
    except TimeoutError as exc:
        stats["errors"].append(f"housekeeping marker: TimeoutError: {exc}")
    except OSError as exc:
        stats["errors"].append(f"housekeeping marker: {type(exc).__name__}: {exc}")
    return stats
