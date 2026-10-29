"""Job response projections and acknowledged-result preservation.

[Role] Render bounded job output and public receipts for lifecycle operations.
[Invariants] Private authorization never enters responses; local recording failures retain the remote result.
[Timeouts & Locks] No transport or waiting; local log previews run within the caller's operation budget.
[Side-Effect Policy] Read local preview files only; never launch, replay or modify remote jobs.
"""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from remote_dev.core.errors import error_details
from remote_dev.core.job_logs import format_job_log_output, job_log_refs, local_stream_preview
from remote_dev.result import make_result

UNKNOWN_JOB_STATES = frozenset({"unknown", "uncertain", "lost", "lost_outcome"})
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "timeout", "cancelled"})


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


_PUBLIC_JOB_KEYS = (
    "schema_version",
    "job_id",
    "description",
    "target",
    "command_preview",
    "cwd",
    "env_keys",
    "runtime_env",
    "runtime_env_file",
    "remote_dir",
    "started_at",
    "timeout_ms",
    "tty",
    "state",
    "stdin_cursors",
    "local_output_offsets",
    "diagnostics",
)


def _public_job_record(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Project saved job state for tool responses without private start-gate fields."""
    projected = {key: record[key] for key in _PUBLIC_JOB_KEYS if key in record}
    projected.update(extra)
    projected.pop("authorization", None)
    return projected


def _session_result(endpoint, record, path, row, *, tool, started, start, budget, local_cache_error=None,
                    preserve_full_page=False, prefer_cache_edges=False, tail_sources=None,
                    preview_error=None):
    state = str(row.get("state") or "unknown")
    quiet = bool(row.get("quiet"))
    pending = {name: int(row.get(name + "_bytes_remaining") or 0) for name in ("stdout", "stderr")}
    exit_code = (row.get("result") or {}).get("exit_code") if state not in {"prepared", "running"} else None
    accepted = row.get("accepted", True)
    uncertain = state in UNKNOWN_JOB_STATES or bool(row.get("unknown"))
    outcome = ("failed" if not accepted or uncertain or state in {"failed", "absent"}
               else "timeout" if state == "timeout" else "cancelled" if state == "cancelled" or row.get("cancellation_requested") else "success")
    job_id = record["job_id"]
    summary = f"Remote command {state}." + (f" Exit code: {exit_code}." if exit_code is not None else "")
    refs = job_log_refs(
        job_id=job_id,
        record_path=path,
        record=record,
        endpoint_id=endpoint.endpoint_id,
        observed_offsets={name: int(row.get(name + "_offset") or 0) for name in ("stdout", "stderr")},
        unread_bytes=pending,
        local_recording_error=local_cache_error,
    )
    if prefer_cache_edges:
        try:
            edge_chars = max(1, int(budget) // 2)
            stdout_source = local_stream_preview(
                Path(path).with_name(Path(path).stem + ".stdout.log"),
                head_chars=edge_chars, tail_chars=edge_chars,
            )
            stderr_source = local_stream_preview(
                Path(path).with_name(Path(path).stem + ".stderr.log"),
                head_chars=edge_chars, tail_chars=edge_chars,
            )
            formatted = format_job_log_output(
                stdout_source, stderr_source, refs=refs,
            )
        except TimeoutError:
            raise
        except OSError as exc:
            preview_error = preview_error or f"local cache preview unavailable: {type(exc).__name__}"
            prefer_cache_edges = False
    if not prefer_cache_edges and tail_sources:
        from remote_dev.core.job_logs import preview_stream_edges

        heads = tail_sources.get("head") if isinstance(tail_sources, dict) else {}
        tails = tail_sources.get("tail") if isinstance(tail_sources, dict) else {}
        edge_sources = {}
        for name in ("stdout", "stderr"):
            total_bytes = int(row.get(name + "_offset") or 0) + int(row.get(name + "_bytes_remaining") or 0)
            head_text = str((heads or {}).get(name) or row.get(name) or "")
            tail_text = str((tails or {}).get(name) or "")
            edge_chars = max(1, int(budget) // 2)
            edge_sources[name] = preview_stream_edges(
                head_text, tail_text,
                total_bytes=max(total_bytes, len(head_text.encode("utf-8"))),
                head_chars=edge_chars, tail_chars=edge_chars,
            )
        formatted = format_job_log_output(
            edge_sources["stdout"], edge_sources["stderr"], refs=refs,
        )
    if not prefer_cache_edges and not tail_sources:
        formatted = format_job_log_output(
            str(row.get("stdout") or ""), str(row.get("stderr") or ""),
            refs=refs, preserve_full_page=preserve_full_page,
        )
    preview = formatted["preview"]
    warnings = []
    if preview_error:
        warnings.append(f"Preview degraded: {preview_error}. The command result and full raw log refs are preserved.")
    if row.get("stdin_buffer_full"):
        warnings.append("Input partially accepted; resend chars[stdin.written_chars:] after the program consumes input.")
    if row.get("eof_deferred"):
        warnings.append("EOF deferred until all input is accepted; resend the remainder with eof=true.")
    if any(pending.values()):
        warnings.append("Output remains; poll the session to continue from the saved cursor.")
    if not accepted:
        warnings.append(str(row.get("reason") or "stdin rejected"))
    if uncertain:
        warnings.append("Remote execution outcome is uncertain; inspect this job without launching or resending input.")
    result = make_result(
        tool=tool, target={**endpoint.to_result_target(), "cwd": record["cwd"]},
        outcome=outcome, status=state if accepted else "stdin_rejected", summary=summary,
        started_at=started, duration_ms=_duration_ms(start), preview=preview, refs=refs, warnings=warnings,
        extra={"job_id": job_id, "session_id": job_id if not quiet or any(pending.values()) else None,
               "state": state, "quiet": quiet, "exit_code": exit_code,
               "cancellation_requested": bool(row.get("cancellation_requested")),
               "timings": {**row.get("timings", {}), **row.get("transport", {})},
               "cursors": record.get("stdin_cursors", {}), "bytes_remaining": pending,
               "output_budget_bytes": budget,
               "preview_degraded": bool(preview_error),
               "output_preview": {"truncated": formatted["truncated"], "omitted_chars": formatted["omitted_chars"],
                                  "omitted_bytes": formatted["omitted_bytes"], "omitted_chars_exact": formatted["omitted_chars_exact"],
                                  "repeated_lines_folded": formatted["repeated_lines_folded"],
                                  "head_chars": formatted["head_chars"], "tail_chars": formatted["tail_chars"],
                                  "full_page": preserve_full_page,
                                  "local_cache_status": refs.get("local_cache_status")},
               "stdin": {key: row[key] for key in ("accepted", "written", "written_chars", "eof", "eof_deferred", "stdin_buffer_full", "retryable") if key in row},
               "environment": {key: record.get(key) for key in ("runtime_env", "runtime_env_file", "env_keys", "timeout_ms")}},
    )
    if uncertain:
        result["error_details"] = {"category": "job_outcome_unknown", "submission_state": "uncertain", "retryable": False}
        result["unknown"] = row.get("unknown") or [str(row.get("reason") or state)]
    if row.get("supervisor_error"):
        result["supervisor_error"] = row["supervisor_error"]
        warnings.append("The remote supervisor failed; its error is separate from command output.")
    text = summary + "\n"
    if result["session_id"]:
        text += f"session_id: {job_id} (poll with remote_job_status, remote_job_output, or remote_job_tail)\n"
    for warning in warnings:
        text += warning + "\n"
    text += formatted["text"]
    return {"text": text, "result": result}


def _recording_failure(endpoint, record, path, row, error, *, tool, started, start, budget, preserve_full_page=False):
    """An acknowledged remote result survives a failed local output commit."""
    payload = _session_result(
        endpoint, record, path, row, tool=tool, started=started, start=start, budget=budget,
        local_cache_error=f"{type(error).__name__}: {error}",
        preserve_full_page=preserve_full_page,
    )
    result = payload["result"]
    result["execution_outcome"] = result["outcome"]
    result["execution_status"] = result["status"]
    result["outcome"] = "failed"
    result["status"] = "local_recording_failed"
    result["session_id"] = record["job_id"]
    result["local_recording"] = {
        "status": "failed", "error": f"{type(error).__name__}: {error}",
        "error_details": error_details(error),
        "observed_cursors": {name + "_offset": row.get(name + "_offset") for name in ("stdout", "stderr")},
    }
    completed = bool(row.get("quiet")) and row.get("state") in TERMINAL_JOB_STATES
    result["operation_completed"] = completed
    result.setdefault("error_details", {"category": "local_recording", "submission_state": "acknowledged", "retryable": False})
    result["error_details"]["operation_completed"] = completed
    message = "Remote result was received, but local output/cursor recording failed. Do not relaunch the command or resend acknowledged input."
    result["summary"] += " " + message
    result["warnings"].append(message)
    payload["text"] += "\n" + message + "\n" + result["local_recording"]["error"] + "\n"
    return payload


