"""
[Role] Kimi Code 2.3.1 native handlers (ReadMediaFile/TaskOutput/TaskStop/TaskList/Bash).
[Invariants] Non-blocking TaskOutput, 32 KiB preview+path, native task_id text, media region.
[Timeouts & Locks] Canonical job control owns deadlines.
[Side-Effect Policy] Mutations only through canonical core.
"""

from __future__ import annotations

from typing import Any, Mapping

from remote_dev.mcp.harness_profiles import NativeToolSpec
from remote_dev.mcp.native_runtime import CanonicalCall, handler
from remote_dev.mcp.native_common import (
    _IMAGE_EXTENSIONS, _assert_visible, _job_record, _media_failure, _native_job_status,
    _job_file, _live_job, _read_media_payload, _record_output_path,
    _refresh_job_output, _tail_text,
)

@handler("kimi_task_output")
def kimi_task_output(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(normalized["job_id"])
    record = _job_record(job_id)
    _assert_visible(record)
    status_payload = _call_canonical("remote.job_status", {"job_id": job_id})
    job = status_payload["result"].get("job") or {}
    state = str(status_payload["result"].get("status") or job.get("state") or "unknown")
    native_status = _native_job_status(state)
    from remote_dev.core.state_store import find_job_record

    _refresh_job_output(job_id)
    found = find_job_record(job_id)
    preview, truncated = ("", False)
    output_path = _record_output_path(record)
    if found:
        stdout, out_truncated = _tail_text(_job_file(found[0], "stdout"), 32768)
        stderr, err_truncated = _tail_text(_job_file(found[0], "stderr"), 32768)
        preview = stdout
        if stderr:
            if preview and not preview.endswith("\n"):
                preview += "\n"
            preview += stderr
        truncated = out_truncated or err_truncated
    remote_status = job.get("remote_status") or {}
    result = job.get("result") or {}
    lines = [
        f"retrieval_status: {'found' if state != 'absent' else 'lost'}",
        f"task_id: {job_id}",
        f"status: {native_status}",
        f"description: {record.get('description') or ''}",
        f"command: {record.get('command_preview') or ''}",
        f"started_at: {record.get('started_at') or ''}",
    ]
    pid = remote_status.get("pid") or result.get("pid")
    if pid is not None:
        lines.append(f"pid: {pid}")
    exit_code = remote_status.get("exit_code")
    if exit_code is None:
        exit_code = result.get("exit_code")
    if exit_code is not None:
        lines.append(f"exit_code: {exit_code}")
    if native_status in {"killed", "failed", "timed_out"}:
        reason = remote_status.get("stop_reason") or result.get("stop_reason")
        if reason:
            lines.append(f"stop_reason: {reason}")
    if output_path:
        lines.append(f"output_path: {output_path}")
    lines.append(f"output_truncated: {str(truncated).lower()}")
    lines.extend(["", "[output]", preview.strip() or "[no output available]"])
    text = "\n".join(lines)
    result_payload = dict(status_payload)
    result_payload["text"] = text
    result_payload["result"] = {
        **status_payload.get("result", {}),
        "tool": spec.wire,
        "status": native_status,
        "job_id": job_id,
        "output_path": output_path,
    }
    result_payload["result"].setdefault("extra", {})["native_task"] = {
        "task_id": job_id, "status": native_status, "output_path": output_path,
    }
    return result_payload

@handler("kimi_task_stop")
def kimi_task_stop(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(normalized["job_id"])
    reason = str(normalized.get("reason") or "Stopped by TaskStop")
    record = _job_record(job_id)
    _assert_visible(record)
    status_payload = _call_canonical("remote.job_status", {"job_id": job_id})
    job = status_payload["result"].get("job") or {}
    state = str(status_payload["result"].get("status") or job.get("state") or "unknown")
    if state in _TERMINAL:
        payload = dict(status_payload)
        payload["text"] = f"task_id: {job_id}\nstatus: {_native_job_status(state)}\nreason: {reason}\n"
        return payload
    payload = _call_canonical("remote.job_stop", {"job_id": job_id})
    final_state = str(payload["result"].get("status") or "unknown")
    payload["text"] = f"task_id: {job_id}\nstatus: {_native_job_status(final_state)}\nreason: {reason}\n"
    payload["result"]["reason"] = reason
    return payload

@handler("kimi_task_list")
def kimi_task_list(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.core.state_store import list_job_records
    from remote_dev.mcp.schemas import TOOL_SCHEMAS
    from remote_dev.result import make_result
    from remote_dev.mcp.tools import _call_canonical

    _ = TOOL_SCHEMAS
    active_only = bool(normalized.get("active_only", True))
    limit = int(normalized.get("limit", 20))
    jobs: list[dict[str, Any]] = []
    from remote_dev.core.state_store import list_endpoint_records
    from remote_dev.core.state_store import resolve_ledger_scope

    scope = resolve_ledger_scope()
    for endpoint in list_endpoint_records():
        endpoint_id = str(endpoint.get("endpoint_id") or "")
        if not endpoint_id:
            continue
        for record in list_job_records(endpoint_id):
            if record.get("session_scope") != scope:
                continue
            try:
                state, _job = _live_job(str(record.get("job_id")))
            except (ValueError, TimeoutError, OSError):
                state = str(record.get("state") or "unknown")
            record = {**record, "state": state}
            if active_only and state in _TERMINAL:
                continue
            jobs.append(record)
    jobs = jobs[:limit]
    lines = [f"{'active_background_tasks' if active_only else 'background_tasks'}: {len(jobs)}"]
    if not jobs:
        lines.append("No background tasks found.")
    for record in jobs:
        job_id = str(record.get("job_id"))
        lines.extend([
            f"task_id: {job_id}",
            f"status: {_native_job_status(str(record.get('state') or 'unknown'))}",
            f"description: {record.get('description') or ''}",
            f"command: {record.get('command_preview') or ''}",
            "---",
        ])
    text = "\n".join(lines).rstrip("-\n ") + "\n"
    return {
        "text": text,
        "result": make_result(
            tool=spec.wire,
            target={"kind": "local_state"},
            outcome="success",
            status="ok",
            summary=f"Listed {len(jobs)} background task(s).",
            extra={"task_count": len(jobs), "jobs": jobs},
        ),
    }

@handler("kimi_read_media")
def kimi_read_media(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = _read_media_payload(
        normalized,
        spec_wire=spec.wire,
        region=native.get("region"),
        max_edge=None if native.get("full_resolution") is True else 1568,
    )
    payload["result"]["full_resolution"] = bool(native.get("full_resolution", False))
    return payload

@handler("kimi_bash")
def kimi_bash(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = call_canonical("remote.bash", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    preview = result.get("preview") or {}
    stdout = str(preview.get("stdout") or "")
    stderr = str(preview.get("stderr") or "")
    exit_code = result.get("exit_code")
    job_id = result.get("job_id") or result.get("session_id")
    body = stdout + stderr
    if state in {"prepared", "running"}:
        body = f"task_id: {job_id}\nstatus: running\n" + body
    elif exit_code not in (None, 0):
        body = body.rstrip() + f"\nCommand failed with exit code: {exit_code}."
    payload["text"] = body if body.endswith("\n") else body + "\n"
    result.update({"tool": spec.wire, "status": state, "task_id": job_id, "exit_code": exit_code})
    return payload

