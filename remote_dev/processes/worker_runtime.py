# Prepended before processes/worker.py by remote_dev.processes.client.worker_source().
"""Remote process identity/ownership and bounded capped-ring job logs.

[Role] Runtime companion for the remote job supervisor (`worker_runtime.py`): Linux
process identity/ownership plus the capped ring log writer/reader. Terminal-job
housekeeping lives in the concatenated `worker_gc.py` companion.
[Invariants]
- `process_identity()` matches PID start ticks so a recycled PID is never signaled; an
  unreadable `/proc` observation stays `unknown` instead of being treated as absent.
- A capped stream retains at most `max_log_bytes` on disk (`<stream>.log.prev` plus
  `<stream>.log`); `logical_bytes_written`/`truncated_head_bytes` keep cursors honest when
  the head has been rotated away instead of silently losing offsets.
- `truncated_head_bytes` is derived from `logical_bytes_written - retained_segment_bytes`,
  so a crash between segment rotation and `logs.json` publication self-heals instead of
  reporting a stale zero.
[Timeouts & Locks]
- `logs.lock` (`flock` SH for readers, EX for append/rotate/close) makes metadata, sizes,
  and bytes one consistent snapshot across the supervisor and the control RPC worker.
- `CappedLog.close()` always attempts to flush/persist pending accounting after an earlier
  rotation failure and surfaces a warning instead of silently zeroing the head offset.
[Side-Effect Policy]
- Writes only inside the assigned job directory; reads or rewrites job output only through
  bounded log windows and never signals a process outside explicit supervision.
"""
import contextlib
import fcntl
import json
import os
import uuid
from pathlib import Path

# worker.py defines these before any runtime call; the defaults keep this module
# independently importable for focused unit tests.
JOB_TOKEN_ENV = globals().get("JOB_TOKEN_ENV", "REMOTE_DEV_JOB_TOKEN")
DEFAULT_MAX_LOG_BYTES = 256 * 1024 * 1024
LOGS_SCHEMA_VERSION = "remote-dev.job.logs.v1"


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
    """Metadata plus retained segment sizes for one stream (caller may hold the lock).

    `truncated_head_bytes` is derived from the logical size and the bytes still
    on disk (`logical - retained`) rather than trusted blindly. A crash between
    segment rotation and `logs.json` publication therefore self-heals on the
    next snapshot instead of reporting a stale zero/old head offset.
    """
    directory = Path(directory)
    meta = _read_logs_meta(directory)
    stored = meta.get(stream)
    stored = stored if isinstance(stored, dict) else {}
    stored_truncated = _positive_int(stored.get("truncated_head_bytes"))
    stored_logical = _positive_int(stored.get("logical_bytes_written"))
    prev_size = _file_size(directory / (stream + ".log.prev"))
    current_size = _file_size(directory / (stream + ".log"))
    retained_size = prev_size + current_size
    logical_size = max(stored_truncated + retained_size, stored_logical)
    truncated = max(stored_truncated, logical_size - retained_size)
    warning = str(stored.get("accounting_warning") or "") or None
    if prev_size and not (stored_truncated or stored_logical):
        # Rotated segments exist but the per-stream publication is absent or
        # zeroed. The dropped head is unknowable from segment sizes alone, so
        # never present this as a clean untruncated log.
        warning = warning or (
            "logs metadata is missing/inconsistent for a rotated stream; "
            "truncated_head_bytes may be understated"
        )
    return {
        "truncated_head_bytes": truncated,
        "prev_size": prev_size,
        "current_size": current_size,
        "retained_bytes": retained_size,
        "logical_bytes": logical_size,
        "generation": _positive_int(stored.get("generation")),
        "accounting_warning": warning,
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
        snapshot = _log_snapshot(self.directory, stream)
        self.truncated_head_bytes = snapshot["truncated_head_bytes"]
        self.logical_bytes_written = snapshot["logical_bytes"]
        self.generation = snapshot["generation"]
        self.accounting_warning = snapshot["accounting_warning"]

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
        entry = {
            "logical_bytes_written": self.logical_bytes_written,
            "truncated_head_bytes": self.truncated_head_bytes,
            "generation": self.generation,
            "retained_bytes": _file_size(self.prev_path) + _file_size(self.path),
        }
        if self.accounting_warning:
            entry["accounting_warning"] = self.accounting_warning
        meta[self.stream] = entry
        _write_json_atomic(self.meta_path, meta)

    def _rotate_locked(self):
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
        try:
            dropped = _file_size(self.prev_path)
            if dropped:
                try:
                    self.prev_path.unlink()
                except FileNotFoundError:
                    dropped = 0
                self.truncated_head_bytes += dropped
            os.replace(self.path, self.prev_path)
            self.generation += 1
            self._write_meta_locked()
        except BaseException as exc:
            self.accounting_warning = (
                "log rotation failed: " + type(exc).__name__ + ": " + str(exc)[:200]
            )
            # Persist whatever is already known even when a rotation step
            # raised; a later snapshot derives any remaining head loss from the
            # actual segment sizes.
            try:
                self._write_meta_locked()
            except (OSError, ValueError) as meta_exc:
                self.accounting_warning += (
                    "; logs.json update failed: " + type(meta_exc).__name__
                )
            raise
        finally:
            if self._handle is None:
                try:
                    self._handle = self.path.open("ab")
                except (OSError, ValueError) as reopen_exc:
                    self.accounting_warning = (
                        "log reopen failed: " + type(reopen_exc).__name__ + ": "
                        + str(reopen_exc)[:200]
                    )

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
        """Flush and publish pending accounting even after a rotation failure.

        Accounting errors are surfaced through `accounting_warning`/`result.json`
        instead of being silently replaced by `truncated_head_bytes=0`.
        """
        error = None
        with self._locked(fcntl.LOCK_EX):
            if self._handle is not None:
                try:
                    self._handle.flush()
                except (OSError, ValueError) as exc:
                    error = exc
                    self.accounting_warning = (
                        "log flush failed: " + type(exc).__name__ + ": " + str(exc)[:200]
                    )
                try:
                    self._handle.close()
                except (OSError, ValueError) as exc:
                    error = error or exc
                finally:
                    self._handle = None
            snapshot = _log_snapshot(self.directory, self.stream)
            self.truncated_head_bytes = max(
                self.truncated_head_bytes, snapshot["truncated_head_bytes"]
            )
            self.logical_bytes_written = max(
                self.logical_bytes_written, snapshot["logical_bytes"]
            )
            if snapshot["accounting_warning"] and not self.accounting_warning:
                self.accounting_warning = snapshot["accounting_warning"]
            try:
                self._write_meta_locked()
            except (OSError, ValueError) as exc:
                self.accounting_warning = (
                    "logs.json update failed: " + type(exc).__name__ + ": " + str(exc)[:200]
                )
                error = error or exc
        if error is not None and not self.accounting_warning:
            self.accounting_warning = type(error).__name__ + ": " + str(error)[:200]


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
