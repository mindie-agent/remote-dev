"""
[Role] Bounded stale session runtime directory reclamation across POSIX and Windows
(`core/session_retention.py`).
[Invariants]
- Never deletes a directory containing a SQLite receipt store or receipt file, so persistent
  `RemoteReceipts` deduplication rows for unknown/executed external mutations survive every GC pass.
- Windows PID liveness never calls `os.kill`: it uses `OpenProcess` + `GetExitCodeProcess`
  (`windows_pid_alive`), treating `ERROR_INVALID_PARAMETER` as exited and `ERROR_ACCESS_DENIED`
  or any unconfirmed result as alive/retained.
- Session lock probes work on both POSIX (`fcntl.flock(LOCK_EX | LOCK_NB)`) and Windows
  (`CreateFileW(dwShareMode=0)` + `msvcrt.locking(LK_NBLCK)` via `windows_session_lock_free`).
[Timeouts & Locks]
- Bounded by `PRUNE_TIME_BUDGET_SECONDS` (`2.0s`) and `PRUNE_MAX_ENTRIES` (`4096`).
- All lock and PID probes are non-blocking one-shot checks; any contention or ambiguity retains.
[Side-Effect Policy]
- Mutates only dead, unlocked, receipt-free `gate-v1*` / `runtime*` / `session*` directories
  directly under the caller-provided `root`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from remote_dev.core.windows_probe import windows_pid_alive, windows_session_lock_free

try:  # POSIX lock probing; Windows uses Win32 sharing + MSVCRT byte lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

PRUNE_TIME_BUDGET_SECONDS = 2.0
PRUNE_MAX_ENTRIES = 4096
SESSION_RUNTIME_PREFIXES = ("gate-v1", "runtime", "session")
SESSION_LOCK_NAMES = ("session.lock", "runtime.lock", "worker.lock", "lock")
RECEIPT_STORE_SUFFIXES = (
    ".sqlite", ".sqlite3", ".db", ".sqlite-wal", ".sqlite-shm",
    ".sqlite3-wal", ".sqlite3-shm", ".sqlite-journal", ".sqlite3-journal",
    ".db-wal", ".db-shm",
)
_SESSION_RUNTIME_RE = re.compile(
    r"^(?:" + "|".join(re.escape(prefix) for prefix in SESSION_RUNTIME_PREFIXES) + r")[-_.]",
    re.IGNORECASE,
)


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


def _session_pid(directory):
    for name in ("pid", "session.json", "runtime.json"):
        path = directory / name
        if name == "pid":
            try:
                value = int(path.read_text(encoding="utf-8").strip())
            except TimeoutError:
                raise
            except (OSError, ValueError):
                continue
            if value > 0:
                return value
        else:
            record = _json_record(path)
            try:
                value = int(record.get("pid")) if record else 0
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return None


def _pid_is_dead(pid, *, platform=None, winapi=None):
    """Provably-gone check; `os.kill` is strictly POSIX-only."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    platform = os.name if platform is None else platform
    if platform == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except TimeoutError:
            raise
        except OSError:
            return False
        return False
    if platform == "nt":
        try:
            return not windows_pid_alive(pid, winapi)
        except TimeoutError:
            raise
        except (OSError, AttributeError):
            return False  # cannot verify liveness: retain the directory
    return False


def _session_lock_free(directory, *, platform=None, msvcrt_module=None, share_api=None):
    platform = os.name if platform is None else platform
    for name in SESSION_LOCK_NAMES:
        path = directory / name
        if not path.exists():
            continue
        if platform == "nt":
            if not windows_session_lock_free(
                path, msvcrt_module=msvcrt_module, share_api=share_api
            ):
                return False
            continue
        if fcntl is None:
            return False
        try:
            handle = path.open("rb")
        except TimeoutError:
            raise
        except OSError:
            return False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            except TimeoutError:
                raise
            except OSError:
                return False
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
    return True


def _contains_receipt_store(directory, deadline):
    """True when a receipt/SQLite store exists or the tree cannot be fully inspected."""
    pending = [Path(directory)]
    seen = 0
    while pending:
        if time.monotonic() >= deadline:
            return True  # incomplete inspection: preserve the directory
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except TimeoutError:
            raise
        except OSError:
            return True  # unreadable tree: never treat it as receipt-free
        with entries:
            for entry in entries:
                seen += 1
                if seen > PRUNE_MAX_ENTRIES:
                    return True  # incomplete inspection: preserve the directory
                try:
                    if entry.is_file(follow_symlinks=False):
                        name = entry.name.lower()
                        if "receipt" in name or name.endswith(RECEIPT_STORE_SUFFIXES):
                            return True
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                except TimeoutError:
                    raise
                except OSError:
                    return True
    return False


def prune_stale_session_runtime_dirs(
    root,
    *,
    now=None,
    max_age_seconds=24 * 60 * 60,
    max_entries=PRUNE_MAX_ENTRIES,
    dry_run=False,
    platform=None,
    winapi=None,
    msvcrt_module=None,
    share_api=None,
):
    """Remove dead-PID, unlocked `gate-v1*`/`runtime*`/`session*` dirs older than 24h."""
    root = Path(root)
    now = time.time() if now is None else float(now)
    stats = {"root": str(root), "scanned": 0, "removed": [], "would_remove": [],
             "skipped": {}, "errors": []}
    if not root.is_dir():
        return stats
    deadline = time.monotonic() + PRUNE_TIME_BUDGET_SECONDS
    for entry in _scandir(root, deadline)[:max_entries]:
        name = entry.name
        if not _SESSION_RUNTIME_RE.match(name) or not entry.is_dir(follow_symlinks=False):
            continue
        stats["scanned"] += 1
        directory = Path(entry.path)
        reason = None
        try:
            info = entry.stat(follow_symlinks=False)
            if now - info.st_mtime <= max_age_seconds:
                reason = "not_stale"
            elif _contains_receipt_store(directory, deadline):
                reason = "receipt_store"
            elif not _session_lock_free(
                directory, platform=platform, msvcrt_module=msvcrt_module, share_api=share_api
            ):
                reason = "locked_or_unverifiable"
            else:
                pid = _session_pid(directory)
                if pid is None:
                    reason = "no_pid"
                elif not _pid_is_dead(pid, platform=platform, winapi=winapi):
                    reason = "pid_alive_or_unknown"
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if reason is not None:
            stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
            continue
        if dry_run:
            stats["would_remove"].append(name)
            continue
        try:
            shutil.rmtree(directory)
        except TimeoutError:
            raise
        except OSError as exc:
            stats["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        stats["removed"].append(name)
    return stats
