"""
[Role] Dispatcher for profile-bound native basic tools (`mcp/harness_native.py`).
[Invariants]
- Validates the native schema before endpoint resolution, applies native
  defaults/units, and delegates to the canonical implementation only after
  removing every native-only field.
- Native job tools enforce the caller's session scope; a job created by one
  session is never listed, polled, written to, or stopped by another.
- A native result is shaped from the real canonical result. Nothing is
  synthesized as success when the underlying operation failed or is uncertain.
[Timeouts & Locks]
- Forwards the caller's native timeout after converting it to canonical
  milliseconds; never rewrites a unit based on the numeric value.
[Side-Effect Policy]
- Same remote effects as the canonical implementation; no automatic retry.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from remote_dev.core.endpoint import selector_fields
from remote_dev.core.errors import caller_error
from remote_dev.mcp.harness_profiles import (
    NativeToolSpec,
    validate_native_arguments,
)
from remote_dev.mcp.schemas import ENDPOINT_PROPS

CanonicalCall = Callable[..., dict[str, Any]]
Handler = Callable[[str, NativeToolSpec, dict[str, Any], dict[str, Any], CanonicalCall], dict[str, Any]]
_HANDLERS: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    def register(function: Handler) -> Handler:
        _HANDLERS[name] = function
        return function

    return register


def _allowed_endpoint_fields() -> set[str]:
    return set(ENDPOINT_PROPS) | set(selector_fields())


def _native_defaults(spec: NativeToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(spec.defaults)
    merged.update(args)
    return merged


def dispatch_native(
    profile: str,
    spec: NativeToolSpec,
    args: dict[str, Any],
    call_canonical: CanonicalCall,
) -> dict[str, Any]:
    # A call that names a documented canonical-compatibility field is served
    # by the canonical contract (for example ``lines`` on DSH job_output or
    # ``start_line`` on DSH read). The canonical dispatcher performs its own
    # required/unknown/type validation, including canonical path aliases.
    if spec.compat_properties and set(args) & set(spec.compat_properties):
        return call_canonical(spec.canonical, dict(args))
    validate_native_arguments(spec, args, _allowed_endpoint_fields())
    native_args = _native_defaults(spec, args)
    normalized = spec.normalize(native_args) if spec.normalize is not None else native_args
    if not isinstance(normalized, dict):
        raise caller_error(f"{spec.wire} internal normalization did not produce an object")
    if spec.handler == "canonical":
        return call_canonical(spec.canonical, normalized)
    try:
        implementation = _HANDLERS[spec.handler]
    except KeyError as exc:
        raise caller_error(f"{spec.wire} has no registered native handler {spec.handler!r}") from exc
    return implementation(profile, spec, native_args, normalized, call_canonical)


# ---------------------------------------------------------------------------
# Shared job helpers
# ---------------------------------------------------------------------------

_TERMINAL = {"cancelled", "succeeded", "failed", "timeout", "absent"}


def _native_job_status(state: str) -> str:
    return {
        "prepared": "running",
        "running": "running",
        "succeeded": "completed",
        "cancelled": "killed",
        "failed": "failed",
        "timeout": "timed_out",
        "absent": "lost",
    }.get(state, "lost")


def _job_record(job_id: str) -> dict[str, Any]:
    from remote_dev.core.state_store import find_job_record

    found = find_job_record(job_id)
    if not found:
        raise caller_error(f"unknown remote job id: {job_id}")
    _path, record = found
    return record


def _assert_visible(record: dict[str, Any]) -> None:
    from remote_dev.core.errors import caller_error as _caller_error
    from remote_dev.core.state_store import resolve_ledger_scope

    scope = resolve_ledger_scope()
    recorded = record.get("session_scope")
    if not recorded:
        raise _caller_error(
            f"job {record.get('job_id')} predates trusted session scoping; "
            "refusing native cross-session visibility",
            PermissionError,
        )
    if recorded != scope:
        raise _caller_error(
            f"job {record.get('job_id')} belongs to a different session; refusing cross-session control",
            PermissionError,
        )


def _record_output_path(record: dict[str, Any], stream: str = "stdout") -> str | None:
    remote_dir = record.get("remote_dir")
    if not remote_dir:
        return None
    return f"{remote_dir}/{stream}.log"


def _job_result(job_id: str) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    payload = _call_canonical("remote.job_status", {"job_id": job_id})
    payload["result"].setdefault("job", {})
    return payload


@handler("kimi_task_output")
def kimi_task_output(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(normalized["job_id"])
    record = _job_record(job_id)
    _assert_visible(record)
    status_payload = _call_canonical("remote.job_status", {"job_id": job_id})
    job = status_payload["result"].get("job") or {}
    state = str(job.get("state") or status_payload["result"].get("status") or "unknown")
    native_status = _native_job_status(state)
    from remote_dev.core.state_store import find_job_record

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
    state = str(job.get("state") or status_payload["result"].get("status") or "unknown")
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
            state = str(record.get("state") or "unknown")
            # The local record is not authoritative for live state; the
            # supervisor status is. Only use it for ordering here.
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


# ---------------------------------------------------------------------------
# DSH native job tools
# ---------------------------------------------------------------------------

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
            jobs.append(record)
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


# ---------------------------------------------------------------------------
# Claude Code native task tools
# ---------------------------------------------------------------------------

def _job_file(path: Any, stream: str) -> Any:
    from pathlib import Path

    path = Path(path)
    return path.with_name(path.stem + f".{stream}.log")


def _tail_text(path: Any, limit: int = 32000) -> tuple[str, bool]:
    from pathlib import Path

    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "", False
    if len(data) > limit:
        return data[-limit:].decode("utf-8", errors="replace"), True
    return data.decode("utf-8", errors="replace"), False


@handler("claude_task_output")
def claude_task_output(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    import time
    from remote_dev.core.state_store import find_job_record
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(native["task_id"])
    found = find_job_record(job_id)
    if not found:
        raise caller_error(f"No task found with ID: {job_id}")
    record_path, record = found
    _assert_visible(record)
    block = native.get("block") is not False
    timeout_ms = int(native.get("timeout", 30000))
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    retrieval = "not_ready"
    status_payload: dict[str, Any] | None = None
    state = str(record.get("state") or "unknown")
    while True:
        status_payload = _call_canonical("remote.job_status", {"job_id": job_id})
        job = status_payload["result"].get("job") or {}
        state = str(job.get("state") or status_payload["result"].get("status") or "unknown")
        if state in _TERMINAL:
            retrieval = "success"
            break
        if not block or time.monotonic() >= deadline:
            retrieval = "timeout" if block else "not_ready"
            break
        time.sleep(0.1)
    remote_status = (job or {}).get("remote_status") or {}
    exit_code = remote_status.get("exit_code")
    if exit_code is None:
        exit_code = ((remote_status.get("result") or {}).get("exit_code"))
    stdout, stdout_truncated = _tail_text(_job_file(record_path, "stdout"))
    stderr, stderr_truncated = _tail_text(_job_file(record_path, "stderr"))
    output = stdout
    if stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += stderr
    if stdout_truncated or stderr_truncated:
        output = "[Truncated to the last 32000 characters]\n" + output
    native_status = {
        "prepared": "running", "running": "running", "succeeded": "completed",
        "cancelled": "killed", "failed": "failed", "timeout": "timeout", "absent": "failed",
    }.get(state, "failed")
    lines = [
        f"<retrieval_status>{retrieval}</retrieval_status>",
        "",
        f"<task_id>{job_id}</task_id>",
        "<task_type>local_bash</task_type>",
        f"<status>{native_status}</status>",
    ]
    if exit_code is not None:
        lines.append(f"<exit_code>{exit_code}</exit_code>")
    if output.strip():
        lines.extend(["<output>", output.rstrip(), "</output>"])
    text = "\n".join(lines)
    result = dict(status_payload["result"])
    result.update({
        "tool": spec.wire,
        "retrieval_status": retrieval,
        "task": {
            "task_id": job_id,
            "task_type": "local_bash",
            "status": native_status,
            "output": output,
            **({"exitCode": exit_code} if exit_code is not None else {}),
        },
    })
    return {"text": text, "result": result}


@handler("claude_task_stop")
def claude_task_stop(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    job_id = str(normalized["job_id"])
    record = _job_record(job_id)
    _assert_visible(record)
    status_payload = _call_canonical("remote.job_status", {"job_id": job_id})
    job = status_payload["result"].get("job") or {}
    state = str(job.get("state") or status_payload["result"].get("status") or "unknown")
    if state in _TERMINAL:
        # Claude TaskStop treats an already-terminal task as a successful
        # no-op and returns its current status.
        payload = status_payload
        payload["text"] = f"Task {job_id} is already {state}." + "\n"
        payload["result"].update({"tool": spec.wire, "task_id": job_id, "task_type": "local_bash"})
        return payload
    payload = _call_canonical("remote.job_stop", {"job_id": job_id})
    message = f"Successfully stopped task: {job_id} ({record.get('command_preview') or record.get('description') or ''})"
    payload["text"] = message + "\n"
    payload["result"].update({
        "tool": spec.wire,
        "message": message,
        "task_id": job_id,
        "task_type": "local_bash",
        "command": record.get("command_preview"),
    })
    return payload


# ---------------------------------------------------------------------------
# Codex unified-exec tools
# ---------------------------------------------------------------------------

def _codex_output(result: Mapping[str, Any]) -> str:
    preview = result.get("preview") or {}
    stdout = str(preview.get("stdout") or "")
    stderr = str(preview.get("stderr") or "")
    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += stderr
    return body


@handler("codex_exec")
def codex_exec(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = call_canonical("remote.bash", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    session_id = result.get("session_id") or result.get("job_id")
    exit_code = result.get("exit_code")
    output = _codex_output(result)
    duration = float(result.get("duration_ms") or 0) / 1000.0
    lines = [f"Chunk ID: {session_id or 'unknown'}", f"Wall time: {duration:.4f} seconds"]
    if state in {"prepared", "running"}:
        lines.append(f"Process running with session ID {session_id}")
    else:
        lines.append(f"Process exited with code {exit_code if exit_code is not None else 'unknown'}")
    if output:
        lines.extend(["Final output:", output.rstrip()])
    payload["text"] = "\n".join(lines) + "\n"
    result.update({
        "tool": spec.wire,
        "chunk_id": str(session_id) if session_id is not None else None,
        "wall_time_seconds": duration,
        "output": output,
        **({"session_id": session_id} if state in {"prepared", "running"} else {}),
        **({"exit_code": exit_code} if exit_code is not None else {}),
    })
    return payload


@handler("codex_write_stdin")
def codex_write_stdin(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = call_canonical("remote.job_stdin", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    exit_code = result.get("exit_code")
    output = _codex_output(result)
    duration = float(result.get("duration_ms") or 0) / 1000.0
    session_id = str(normalized.get("job_id"))
    lines = [f"Chunk ID: {session_id}", f"Wall time: {duration:.4f} seconds"]
    if state in {"prepared", "running"}:
        lines.append(f"Process running with session ID {session_id}")
    else:
        lines.append(f"Process exited with code {exit_code if exit_code is not None else 'unknown'}")
    if output:
        lines.extend(["Final output:", output.rstrip()])
    payload["text"] = "\n".join(lines) + "\n"
    result.update({
        "tool": spec.wire,
        "chunk_id": session_id,
        "wall_time_seconds": duration,
        "output": output,
        "session_id": int(session_id) if session_id.isdigit() else session_id,
    })
    return payload


# ---------------------------------------------------------------------------
# Native image reads
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _read_image_payload(normalized: Mapping[str, Any]) -> dict[str, Any]:
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.image_ops import remote_read_image

    endpoint = resolve_endpoint(dict(normalized))
    return remote_read_image(endpoint, file_path=str(normalized["file_path"]))


@handler("dsh_read_image")
def dsh_read_image(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = _read_image_payload(normalized)
    payload["result"]["tool"] = spec.wire
    return payload


@handler("codex_view_image")
def codex_view_image(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = _read_image_payload(normalized)
    if payload.get("content"):
        mime = payload["result"].get("extra", {}).get("mime_type") or "image/png"
        data = payload["content"][0]["data"]
        payload["result"].update({
            "tool": spec.wire,
            "image_url": f"data:{mime};base64,{data}",
            "detail": native.get("detail") or "high",
        })
    return payload


@handler("claude_read")
def claude_read(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from pathlib import Path

    suffix = Path(str(normalized["file_path"])).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        payload = _read_image_payload(normalized)
        payload["result"]["tool"] = spec.wire
        return payload
    if suffix == ".pdf":
        raise caller_error(
            "Claude Read PDF page rendering is not available over remote-dev; "
            "use remote_artifact_pull to fetch the PDF and read it locally"
        )
    if suffix == ".ipynb":
        from remote_dev.core.endpoint import resolve_endpoint
        from remote_dev.core.text_ops import remote_read_utf8_text
        from remote_dev.result import make_result

        endpoint = resolve_endpoint(dict(normalized))
        fetched = remote_read_utf8_text(endpoint, file_path=str(normalized["file_path"]))
        if fetched["result"].get("status") != "ok":
            return fetched
        import json

        try:
            notebook = json.loads(fetched["raw_text"])
            cells = notebook.get("cells") or []
        except (ValueError, AttributeError) as exc:
            raise caller_error(f"remote notebook is not valid .ipynb JSON: {exc}") from exc
        sections = [f"Notebook {normalized['file_path']} ({len(cells)} cells)"]
        for index, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            source = cell.get("source") or []
            if isinstance(source, list):
                source = "".join(str(part) for part in source)
            sections.extend(["", f"# Cell {index} [{cell.get('cell_type', 'unknown')}]", str(source)])
            outputs = cell.get("outputs") or []
            if outputs:
                sections.append("Outputs:")
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    text = output.get("text")
                    if isinstance(text, list):
                        text = "".join(str(part) for part in text)
                    if text:
                        sections.append(str(text).rstrip())
        text = "\n".join(sections)
        result = make_result(
            tool=spec.wire,
            target=endpoint.to_result_target(),
            outcome="success",
            status="ok",
            summary=f"RemoteRead notebook {normalized['file_path']} ({len(cells)} cells).",
            preview={"content": text[:4000]},
            extra={"cells": len(cells), "path": normalized["file_path"]},
        )
        result["file"] = {"path": normalized["file_path"], "cells": len(cells)}
        return {"text": text, "result": result}
    payload = call_canonical("remote.read", normalized)
    payload["result"]["tool"] = spec.wire
    return payload


@handler("kimi_read_media")
def kimi_read_media(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    if native.get("region") is not None:
        raise caller_error(
            "remote_ReadMediaFile does not crop a remote image region; read the full image "
            "or pull the artifact and crop locally"
        )
    payload = _read_image_payload(normalized)
    payload["result"]["tool"] = spec.wire
    payload["result"]["full_resolution"] = bool(native.get("full_resolution", False))
    return payload
