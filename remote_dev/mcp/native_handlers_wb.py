"""
[Role] WorkBuddy (wb) native handlers (TaskOutput/BashOutput with raw-log incremental cursors & regex filter, ViewImage).
[Invariants] Preserves Claude/CodeBuddy task output XML envelope while supporting raw-offset incremental vs full=true output reads, regex line filtering, and ViewImage query metadata. Existing log-file or job-record stat/read OSError is never disguised as an empty incremental delta or a missing cursor. Cursor-commit recording_failure never invents outcome=success, never treats a live/non-FSM status as operation_completed, preserves real job execution_outcome over status-query success, and treats explicit wb_output_offsets or in-stream offset null as unreadable.
[Timeouts & Locks] Canonical job control owns deadlines; cursor persistence acquires record_lock(record_path).
[Side-Effect Policy] Read-only observation and media fetch via canonical core; persists wb_output_offsets into the reloaded job record and surfaces local_recording_failed on persistence errors.
"""

from __future__ import annotations

from pathlib import Path
import math
import re
from typing import Any

from remote_dev.core.errors import caller_error
from remote_dev.mcp.harness_profiles import NativeToolSpec
from remote_dev.mcp.native_common import _TERMINAL, _raise_job_log_read_error, _read_media_payload
from remote_dev.mcp.native_handlers_claude import claude_task_output
from remote_dev.mcp.native_runtime import CanonicalCall, handler

_WB_CURSORS: dict[str, dict[str, Any]] = {}
_MAX_DELTA_READ_BYTES = 4 * 1024 * 1024
_LIVE_JOB_STATES = frozenset({"prepared", "running"})
_FSM_EXECUTION_OUTCOME = {
    "prepared": "success",
    "running": "success",
    "succeeded": "success",
    "cancelled": "cancelled",
    "failed": "failed",
    "timeout": "timeout",
    "absent": "failed",
}


def _wb_cursor_offset(value: Any, *, field: str, source: str) -> int:
    if value is None:
        raise ValueError(f"job record unreadable: {source}: {field} is null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"job record unreadable: {source}: {field} is {type(value).__name__}")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"job record unreadable: {source}: {field} is not a finite integer")
        try:
            value = int(value)
        except OverflowError as exc:
            raise ValueError(f"job record unreadable: {source}: {field} overflows integer range") from exc
    if value < 0:
        raise ValueError(f"job record unreadable: {source}: {field} is negative")
    return value


def _wb_cursor_from_saved(saved: dict[str, Any], *, source: str) -> dict[str, Any]:
    try:
        return {
            "stdout_offset": _wb_cursor_offset(saved.get("stdout_offset", 0), field="stdout_offset", source=source),
            "stderr_offset": _wb_cursor_offset(saved.get("stderr_offset", 0), field="stderr_offset", source=source),
            "stdout_head": str(saved.get("stdout_head", "")),
            "stderr_head": str(saved.get("stderr_head", "")),
        }
    except TimeoutError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("job record unreadable:"):
            raise
        raise ValueError(f"job record unreadable: {source}: invalid wb_output_offsets: {exc}") from exc


def _saved_wb_offsets(container: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    if "wb_output_offsets" not in container:
        return None
    saved = container["wb_output_offsets"]
    if saved is None:
        raise ValueError(f"job record unreadable: {source}: wb_output_offsets is null")
    if not isinstance(saved, dict):
        raise ValueError(f"job record unreadable: {source}: wb_output_offsets is {type(saved).__name__}")
    return _wb_cursor_from_saved(saved, source=source)


def _fsm_states(observed: dict[str, Any]) -> tuple[Any, ...]:
    job = observed.get("job") if isinstance(observed.get("job"), dict) else {}
    return (job.get("state"), observed.get("state"), observed.get("status"))


def _cursor_commit_progress(observed: Any) -> str:
    """Classify a TaskOutput receipt as live, terminal, nonterminal, or incomplete.

    Only the canonical job FSM (prepared/running vs succeeded/failed/timeout/cancelled/absent)
    marks completion. Native projections and aliases (completed/success/finished/unknown) do not.
    """
    if not isinstance(observed, dict):
        return "incomplete"
    outcome = observed.get("outcome")
    status = observed.get("status")
    if not isinstance(outcome, str) or not outcome:
        return "incomplete"
    if not isinstance(status, str) or not status:
        return "incomplete"
    for candidate in _fsm_states(observed):
        if candidate in _LIVE_JOB_STATES:
            return "live"
        if candidate in _TERMINAL:
            return "terminal"
    return "nonterminal"


def _execution_outcome_from_fsm(observed: dict[str, Any]) -> str:
    """Job execution outcome from the FSM, never the status-query success bit."""
    for candidate in _fsm_states(observed):
        mapped = _FSM_EXECUTION_OUTCOME.get(candidate)
        if mapped is not None:
            return mapped
    outcome = observed.get("outcome")
    if outcome in {"failed", "timeout", "cancelled", "blocked"}:
        return str(outcome)
    return "failed"


def _load_wb_cursor(job_id: str, record_path: Path | None, record: dict[str, Any] | None) -> dict[str, Any]:
    if record_path is not None:
        try:
            from remote_dev.core.state_store import read_json

            latest = read_json(record_path)
        except TimeoutError:
            raise
        except FileNotFoundError:
            latest = None
        except OSError as exc:
            _raise_job_log_read_error(record_path, exc, kind="job record")
        except ValueError as exc:
            raise ValueError(f"job record unreadable: {record_path}: {type(exc).__name__}: {exc}") from exc
        else:
            if not isinstance(latest, dict):
                raise ValueError(
                    f"job record unreadable: {record_path}: expected object, got {type(latest).__name__}"
                )
            loaded = _saved_wb_offsets(latest, source=str(record_path))
            if loaded is not None:
                return loaded
    if isinstance(record, dict):
        loaded = _saved_wb_offsets(record, source="in-memory record")
        if loaded is not None:
            return loaded
    key = f"{record_path or ''}:{job_id}"
    return dict(
        _WB_CURSORS.get(
            key,
            {"stdout_offset": 0, "stderr_offset": 0, "stdout_head": "", "stderr_head": ""},
        )
    )


def _save_wb_cursor(
    job_id: str,
    record_path: Path | None,
    record: dict[str, Any] | None,
    cursor: dict[str, Any],
) -> None:
    if record_path is not None and record_path.is_file():
        from remote_dev.core.locking import record_lock
        from remote_dev.core.state_store import atomic_write_json, read_json

        with record_lock(record_path):
            latest = read_json(record_path)
            if not isinstance(latest, dict):
                latest = dict(record or {})
            latest["wb_output_offsets"] = dict(cursor)
            atomic_write_json(record_path, latest)
            if isinstance(record, dict):
                record.clear()
                record.update(latest)
    elif isinstance(record, dict):
        record["wb_output_offsets"] = dict(cursor)

    key = f"{record_path or ''}:{job_id}"
    if len(_WB_CURSORS) >= 1024 and key not in _WB_CURSORS:
        _WB_CURSORS.pop(next(iter(_WB_CURSORS)), None)
    _WB_CURSORS[key] = dict(cursor)


def _read_stream_incremental(
    record_path: Path | None,
    stream: str,
    prev_offset: int,
    prev_head: str,
    *,
    full_snapshot: bool,
    full_preview_text: str,
) -> tuple[str, int, str]:
    from remote_dev.core.job_logs import preview_stream_text
    from remote_dev.mcp import native_handlers_claude as claude_handlers

    if record_path is not None:
        log_path = Path(claude_handlers._job_file(record_path, stream))
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as fh:
                head_bytes = fh.read(64)
                head_str = head_bytes.decode("latin1")
                offset = prev_offset
                if offset < 0 or offset > size or (prev_head and not head_str.startswith(prev_head)):
                    offset = 0
                if full_snapshot:
                    return full_preview_text, size, head_str
                read_len = size - offset
                if read_len <= 0:
                    return "", size, head_str
                if read_len > _MAX_DELTA_READ_BYTES:
                    fh.seek(size - _MAX_DELTA_READ_BYTES)
                    raw_delta = fh.read(_MAX_DELTA_READ_BYTES)
                else:
                    fh.seek(offset)
                    raw_delta = fh.read(read_len)
            delta_text = raw_delta.decode("utf-8", errors="replace")
            return preview_stream_text(delta_text)["text"], size, head_str
        except TimeoutError:
            raise
        except FileNotFoundError:
            pass
        except OSError as exc:
            _raise_job_log_read_error(log_path, exc)

        raw_text, _ = claude_handlers._tail_text(log_path)
    else:
        raw_text = full_preview_text

    raw_bytes = raw_text.encode("utf-8")
    size = len(raw_bytes)
    head_str = raw_bytes[:64].decode("latin1")
    offset = prev_offset
    if offset < 0 or offset > size or (prev_head and not head_str.startswith(prev_head)):
        offset = 0
    if full_snapshot:
        return full_preview_text, size, head_str
    if offset >= size:
        return "", size, head_str
    delta_text = raw_bytes[offset:].decode("utf-8", errors="replace")
    return preview_stream_text(delta_text)["text"], size, head_str


def _replace_xml_output(text: str, new_output: str) -> str:
    if "<output>" in text and "</output>" in text:
        before, rest = text.split("<output>", 1)
        _mid, after = rest.split("</output>", 1)
        if new_output.strip():
            return f"{before}<output>\n{new_output.rstrip()}\n</output>{after}"
        return (before.rstrip() + after).strip()
    if new_output.strip():
        return f"{text.rstrip()}\n<output>\n{new_output.rstrip()}\n</output>"
    return text


@handler("wb_task_output")
def wb_task_output(
    profile: str,
    spec: NativeToolSpec,
    native: dict[str, Any],
    normalized: dict[str, Any],
    call_canonical: CanonicalCall,
) -> dict[str, Any]:
    from remote_dev.core.state_store import find_job_record
    from remote_dev.result import recording_failure

    adapted_native = dict(native)
    if "task_id" not in adapted_native and "shell_id" in adapted_native:
        adapted_native["task_id"] = adapted_native["shell_id"]
    if "timeout" not in adapted_native:
        adapted_native["timeout"] = 60000
    pattern_raw = native.get("filter")
    regex: re.Pattern[str] | None = None
    if pattern_raw is not None and str(pattern_raw) != "":
        try:
            regex = re.compile(str(pattern_raw), re.IGNORECASE)
        except re.error as exc:
            raise caller_error(f"Invalid filter regex {pattern_raw!r}: {exc}", field="filter") from exc

    job_id = str(adapted_native["task_id"])
    found = find_job_record(job_id)
    record_path = Path(found[0]) if found else None
    record = found[1] if found else None

    payload = claude_task_output(profile, spec, adapted_native, normalized, call_canonical)
    result = payload.get("result") or {}
    task_info = result.get("task")
    preview = result.get("preview")
    if not isinstance(task_info, dict) or not isinstance(preview, dict):
        return payload

    stdout_full = str(preview.get("stdout") or "")
    stderr_full = str(preview.get("stderr") or "")
    full_snapshot = native.get("full") is True
    cursor = _load_wb_cursor(job_id, record_path, record)

    stdout_out, next_stdout_off, next_stdout_head = _read_stream_incremental(
        record_path,
        "stdout",
        int(cursor.get("stdout_offset", 0)),
        str(cursor.get("stdout_head", "")),
        full_snapshot=full_snapshot,
        full_preview_text=stdout_full,
    )
    stderr_out, next_stderr_off, next_stderr_head = _read_stream_incremental(
        record_path,
        "stderr",
        int(cursor.get("stderr_offset", 0)),
        str(cursor.get("stderr_head", "")),
        full_snapshot=full_snapshot,
        full_preview_text=stderr_full,
    )

    combined = stdout_out
    if stderr_out:
        if combined and not combined.endswith("\n"):
            combined += "\n"
        combined += stderr_out
    combined = combined.strip("\n")

    if regex is not None:
        filtered_lines = [line for line in combined.splitlines() if regex.search(line)]
        combined = "\n".join(filtered_lines)
        task_info["filter"] = str(pattern_raw)

    preview["stdout"] = stdout_out
    preview["stderr"] = stderr_out
    task_info["output"] = combined
    task_info["full"] = full_snapshot
    payload["text"] = _replace_xml_output(str(payload.get("text") or ""), combined)

    try:
        _save_wb_cursor(
            job_id,
            record_path,
            record,
            {
                "stdout_offset": next_stdout_off,
                "stderr_offset": next_stderr_off,
                "stdout_head": next_stdout_head,
                "stderr_head": next_stderr_head,
            },
        )
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        observed = payload.get("result")
        progress = _cursor_commit_progress(observed)
        if progress == "incomplete":
            raise
        observed["outcome"] = _execution_outcome_from_fsm(observed)
        if not isinstance(observed.get("summary"), str):
            observed["summary"] = ""
        return recording_failure(
            payload,
            exc,
            stage="wb_cursor_commit",
            operation_completed=progress == "terminal",
        )

    return payload


@handler("wb_view_image")
def wb_view_image(
    profile: str,
    spec: NativeToolSpec,
    native: dict[str, Any],
    normalized: dict[str, Any],
    call_canonical: CanonicalCall,
) -> dict[str, Any]:
    payload = _read_media_payload(normalized, spec_wire=spec.wire)
    result = payload.get("result")
    query = str(native.get("query") or "")
    if isinstance(result, dict) and query:
        result["query"] = query
        if result.get("outcome") == "success":
            summary = f"{result.get('summary', '').rstrip('.')} [query: {query}]."
            result["summary"] = summary
            payload["text"] = summary + "\n"
    return payload
