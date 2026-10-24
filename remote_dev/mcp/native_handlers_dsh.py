"""
[Role] DSH 0.2.0-rc.2 native handlers (read_image/bash/job_output/job_list/job_kill).
[Invariants] Consumptive job_output, native background shape, strict session scope.
[Timeouts & Locks] Canonical job control owns deadlines.
[Side-Effect Policy] Mutations only through canonical core.
"""

from __future__ import annotations

from typing import Any, Mapping

from remote_dev.mcp.harness_profiles import NativeToolSpec
from remote_dev.mcp.native_runtime import CanonicalCall, handler
from remote_dev.mcp.native_common import (
    _TERMINAL, _assert_visible, _job_record, _live_job, _media_failure, _read_media_payload,
)

def _dsh_status(state: str) -> str:
    return {
        "prepared": "running",
        "running": "running",
        "succeeded": "completed",
        "failed": "failed",
        "timeout": "failed",
        "cancelled": "killed",
        "absent": "failed",
    }.get(state, "failed")

def _epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None

def _dsh_job(record: dict[str, Any], *, state: str | None = None, detail: str | None = None) -> dict[str, Any]:
    job_id = str(record.get("job_id") or "")
    status = _dsh_status(str(state or record.get("state") or "unknown"))
    payload: dict[str, Any] = {
        "id": job_id,
        "kind": "bash",
        "label": str(record.get("description") or record.get("command_preview") or job_id),
        "status": status,
    }
    if detail:
        payload["detail"] = detail
    started = _epoch_ms(record.get("started_at"))
    if started is not None:
        payload["startedAt"] = started
    return payload

def _dsh_detail(state: str, result: Mapping[str, Any] | None, stop_reason: str | None = None) -> str | None:
    result = result or {}
    if stop_reason:
        return stop_reason
    if state == "timeout":
        return "timed out"
    if state in {"succeeded", "failed"}:
        exit_code = result.get("exit_code")
        if exit_code is not None:
            return f"exit code: {exit_code}"
        return state
    return None

@handler("dsh_job_output")
def dsh_job_output(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.core.job_ops import remote_job_output_incremental

    job_id = str(native["job_id"])
    record = _job_record(job_id)
    _assert_visible(record)
    wait = native.get("wait") is True
    timeout_ms = int(native["timeout_ms"]) if native.get("timeout_ms") is not None else None
    payload = remote_job_output_incremental(None, job_id=job_id, wait=wait, timeout_ms=timeout_ms)
    state = str(payload["result"].get("state") or "unknown")
    result = payload["result"]
    # remote_job_output_incremental settles on the last supervisor row; expose
    # the recorded terminal detail when available.
    detail = _dsh_detail(state, None)
    body = str(payload.get("text") or "")
    job = _dsh_job(record, state=state, detail=detail)
    status_line = f"[status: {job['status']}" + (f", {detail}]" if detail else "]")
    text = f"{body if body else '(no new output)'}\n{status_line}"
    payload["text"] = text
    result["tool"] = spec.wire
    result["job"] = job
    result["status"] = job["status"]
    result["wait"] = wait
    return payload

@handler("dsh_job_list")
def dsh_job_list(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.core.state_store import list_endpoint_records, list_job_records, resolve_ledger_scope
    from remote_dev.result import make_result

    scope = resolve_ledger_scope()
    jobs = []
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
            jobs.append({**record, "state": state})
    jobs.sort(key=lambda item: str(item.get("started_at") or ""))
    projections = [_dsh_job(record) for record in jobs]
    text = "(no background jobs)" if not projections else "\n".join(
        f"{job['id']} [{job['kind']}] {job['status']} — {job['label']}" for job in projections
    )
    return {
        "text": text + "\n",
        "result": make_result(
            tool=spec.wire,
            target={"kind": "local_state"},
            outcome="success",
            status="ok",
            summary=f"Listed {len(projections)} background job(s).",
            extra={"jobs": projections, "job_count": len(projections)},
        ),
    }

@handler("dsh_job_kill")
def dsh_job_kill(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(native["job_id"])
    reason = str(native.get("reason") or "")
    record = _job_record(job_id)
    _assert_visible(record)
    try:
        state, _live = _live_job(job_id)
    except (ValueError, TimeoutError, OSError):
        state = str(record.get("state") or "unknown")
    if state in _TERMINAL:
        outcome = "already-finished"
        payload = _call_canonical("remote.job_status", {"job_id": job_id})
    else:
        outcome = "cancellation-requested"
        payload = _call_canonical("remote.job_stop", {"job_id": job_id})
        state = str(payload["result"].get("status") or state)
    job = _dsh_job(record, state=state, detail=reason or None)
    payload["text"] = f"{outcome}\n{job['id']} [{job['kind']}] {job['status']} — {job['label']}\n"
    payload["result"]["job"] = job
    payload["result"]["outcome"] = outcome
    if reason:
        payload["result"]["reason"] = reason
    return payload

@handler("dsh_read_image")
def dsh_read_image(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    return _read_media_payload(normalized, spec_wire=spec.wire)

@handler("dsh_bash")
def dsh_bash(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = call_canonical("remote.bash", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    preview = result.get("preview") or {}
    stdout = str(preview.get("stdout") or "")
    stderr = str(preview.get("stderr") or "")
    exit_code = result.get("exit_code")
    background = native.get("run_in_background") is True
    timeout_ms = int(native.get("timeoutMs", 120000))
    if background:
        kind = "background"
    elif state in {"prepared", "running"}:
        kind = "promoted"
    else:
        kind = "foreground"
    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += "[stderr]\n" + stderr
    markers = []
    if state == "timeout":
        markers.append(f"[timed out after {timeout_ms}ms]")
    if state == "cancelled":
        markers.append("[stopped]")
    if exit_code not in (None, 0):
        markers.append(f"[exit code: {exit_code}]")
    text = body.rstrip() if body else "(no output)"
    if kind == "background":
        job_id = result.get("job_id") or result.get("session_id")
        text = f"background job {job_id}"
        for key in ("timeoutMs", "output", "stdout", "stderr", "exitCode", "timedOut", "aborted"):
            result.pop(key, None)
    elif kind == "promoted":
        job_id = result.get("job_id") or result.get("session_id")
        text += f"\n[still running after {timeout_ms}ms; moved to background job {job_id}]"
    if markers:
        text += "\n" + "\n".join(markers)
    payload["text"] = text + "\n"
    native_result: dict[str, Any] = {"tool": spec.wire, "kind": kind}
    if kind == "background":
        native_result["jobId"] = result.get("job_id") or result.get("session_id")
    else:
        native_result.update({
            "timeoutMs": timeout_ms,
            "output": body,
            "stdout": stdout,
            "stderr": stderr,
            "exitCode": exit_code,
            "timedOut": state == "timeout",
            "aborted": state == "cancelled",
        })
        if kind == "promoted":
            native_result["jobId"] = result.get("job_id") or result.get("session_id")
    result.update(native_result)
    return payload

