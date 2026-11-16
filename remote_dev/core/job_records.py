"""
[Role] Durable local job-record persistence: endpoint resolution, output/cursor commits, and
confirmed-terminal state projection (`job_records.py`).
[Invariants]
- A job record is resolved from either the explicit endpoint or the unique endpoint-scoped
  record for the job id; a fixed full container ID is required before any container resolution.
- Output commits retain the acknowledged remote observation when the local log/cursor write
  fails (`_recording_failure` in `job_results.py` owns the response projection).
- `CappedLog` head-truncation metadata (`truncated_head_bytes` / `logical_bytes_written`) is
  persisted in `output_logs` so public receipts never lose the retained byte range.
- A confirmed terminal remote state is written back under the record lock only when the remote
  supervisor did not report `unknown`; a failed auxiliary write is reported as `receipt_warning`
  without changing the primary status/stop response.
[Timeouts & Locks]
- `save_output` runs under the caller's `record_lock(path)` for the whole read-modify-write.
- Terminal-state projection acquires its own short bounded `record_lock`; lock contention or
  local I/O failure is non-fatal to the remote observation.
[Side-Effect Policy]
- Writes only `<job_id>.json`, `<job_id>.stdout.log`, `<job_id>.stderr.log` and the associated
  `.json.lock` under the caller-provided endpoint state directory.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from remote_dev.core.container_endpoint import FULL_CONTAINER_ID
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError, caller_error
from remote_dev.core.job_output import merge_output_logs, observed_accounting_warning
from remote_dev.core.locking import record_lock
from remote_dev.core.state_store import (
    atomic_write_json,
    find_job_record,
    job_record_path,
    maybe_prune_endpoint_state_dir,
)
from remote_dev.result import utc_now_iso

TERMINAL_REMOTE_JOB_STATES = frozenset({"succeeded", "failed", "timeout", "cancelled"})
TERMINAL_LOCK_TIMEOUT_SECONDS = 0.5


def endpoint_from_record(record: dict[str, Any]) -> Endpoint:
    target = record.get("connection") or record.get("target", {})
    fields = Endpoint.__dataclass_fields__
    endpoint = Endpoint(**{key: value for key, value in target.items() if key in fields})
    if endpoint.container and not FULL_CONTAINER_ID.fullmatch(endpoint.container):
        raise ValueError("job record lacks a fixed full container ID; refusing to resolve a replacement by name")
    return endpoint


def load_record(
    endpoint: Endpoint | None, job_id: str
) -> tuple[Endpoint, dict[str, Any], Path]:
    if endpoint is not None:
        path = job_record_path(endpoint, job_id)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise caller_error(
                f"unknown remote job id for endpoint: {job_id}", FileNotFoundError,
                error_code="unknown_job", field="job_id"
            ) from None
        return endpoint, json.loads(content), path
    found = find_job_record(job_id)
    if not found:
        raise caller_error(f"unknown remote job id: {job_id}", FileNotFoundError,
                            error_code="unknown_job", field="job_id")
    path, data = found
    return endpoint_from_record(data), data, path


def save_output(record, path, row, *, cursor_key="stdin_cursors", write_json=None):
    """Commit one remote output observation and its cursors to the local record.

    Hold the record lock from read through exchange and cursor commit. Overwrite
    from the committed length after a crash before cursor commit. Do not expose
    a cursor that the local transaction did not commit. If a file write or the
    receipt fails, the remote observation still exists and the next read resumes
    the previous committed cursor without replaying input.
    """
    updated = copy.deepcopy(record)
    cursors = updated.setdefault(cursor_key, {})
    local_offsets = updated.setdefault("local_output_offsets", {})
    for name in ("stdout", "stderr"):
        log = path.with_name(path.stem + "." + name + ".log")
        with log.open("r+b" if log.exists() else "w+b") as stream:
            stream.seek(int(local_offsets.get(name, 0)))
            stream.write(str(row.get(name) or "").encode("utf-8"))
            stream.truncate()
            local_offsets[name] = stream.tell()
        cursors[name + "_offset"] = int(row.get(name + "_offset", cursors.get(name + "_offset", 0)))
    updated["state"] = row.get("state", "unknown")
    updated["output_logs"] = merge_output_logs(updated, row)
    (write_json or atomic_write_json)(path, updated)
    record.clear()
    record.update(updated)
    # Bounded local retention for terminal records/logs; the current job is
    # protected so an active transaction can never evict itself mid-write.
    maybe_prune_endpoint_state_dir(path.parent.parent, protect_job_ids={path.stem})


def persist_terminal_state(
    record_path: Path, record: dict[str, Any], supervisor: dict[str, Any], *,
    write_json=None,
) -> dict[str, Any] | None:
    """Write a confirmed terminal remote state back to the local job record.

    Returns a `receipt_warning` payload on lock/write failure and never raises
    for an auxiliary receipt problem: the remote status/stop response remains
    the primary, authoritative observation.
    """
    state = str(supervisor.get("state") or "")
    if (state not in TERMINAL_REMOTE_JOB_STATES or supervisor.get("unknown")
            or not supervisor.get("quiet")):
        return None
    remote_result = supervisor.get("result")
    remote_result = remote_result if isinstance(remote_result, dict) else {}
    accounting_row: dict[str, Any] = {}
    remote_logs = remote_result.get("logs")
    log_warning = observed_accounting_warning({"result": remote_result})
    if isinstance(remote_logs, dict):
        for name in ("stdout", "stderr"):
            entry = remote_logs.get(name)
            if not isinstance(entry, dict):
                continue
            if entry.get("logical_bytes_written") is not None:
                accounting_row[name + "_logical_bytes"] = entry["logical_bytes_written"]
            if entry.get("truncated_head_bytes") is not None:
                accounting_row[name + "_truncated_head_bytes"] = entry["truncated_head_bytes"]
    lock = record_lock(record_path, timeout_seconds=TERMINAL_LOCK_TIMEOUT_SECONDS)
    try:
        lock.__enter__()
    except TimeoutError:
        return {"stage": "job_terminal_state", "error_type": "TimeoutError"}
    except RemoteExecutionError:
        return {"stage": "job_terminal_state", "error_type": "RemoteExecutionError"}
    except OSError as exc:
        return {"stage": "job_terminal_state", "error_type": type(exc).__name__}
    try:
        live = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(live, dict):
            raise ValueError("local job record is not a JSON object")
        updated = copy.deepcopy(live)
        changed = str(updated.get("state") or "") != state
        updated["state"] = state
        if accounting_row:
            merged = merge_output_logs(updated, accounting_row, warning=log_warning)
            if merged != live.get("output_logs"):
                updated["output_logs"] = merged
                changed = True
        if not changed:
            return None  # already terminal: keep the original retention mtime
        updated["terminal_observed_at"] = utc_now_iso()
        (write_json or atomic_write_json)(record_path, updated)
        record.clear()
        record.update(updated)
        return None
    except TimeoutError:
        return {"stage": "job_terminal_state", "error_type": "TimeoutError"}
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        return {"stage": "job_terminal_state", "error_type": type(exc).__name__}
    finally:
        try:
            lock.__exit__(None, None, None)
        except (OSError, RemoteExecutionError):
            pass  # the primary remote observation already exists


__all__ = [
    "TERMINAL_LOCK_TIMEOUT_SECONDS",
    "TERMINAL_REMOTE_JOB_STATES",
    "endpoint_from_record",
    "load_record",
    "persist_terminal_state",
    "save_output",
]
