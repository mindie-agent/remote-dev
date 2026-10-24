"""
[Role] Claude Code 2.1.269 native handlers (Read/Write/Edit/NotebookEdit/Bash/TaskOutput/TaskStop).
[Invariants] Native prior-read, TaskOutput block semantics, local PDF/notebook rendering.
[Timeouts & Locks] Canonical job control owns deadlines.
[Side-Effect Policy] Mutations only through canonical core.
"""

from __future__ import annotations

from typing import Any, Mapping

from remote_dev.mcp.harness_profiles import NativeToolSpec
from remote_dev.mcp.native_runtime import CanonicalCall, handler
from remote_dev.mcp.native_common import (
    _IMAGE_EXTENSIONS, _TERMINAL, _assert_visible, _job_file, _job_record,
    _media_failure, _read_media_payload, _refresh_job_output, _tail_text,
)

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
        state = str(status_payload["result"].get("status") or job.get("state") or "unknown")
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
    _refresh_job_output(job_id)
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
    state = str(status_payload["result"].get("status") or job.get("state") or "unknown")
    if state in _TERMINAL:
        # Native Claude TaskStop validateInput rejects a non-running task with
        # result:false / errorCode:3, rather than treating it as a no-op.
        from remote_dev.result import make_result

        message = f"Task {job_id} is not running (status: {state})"
        return {
            "text": message + "\n",
            "result": make_result(
                tool=spec.wire,
                target={"kind": "local_state"},
                outcome="failed",
                status="not_running",
                summary=message,
                preview={"stderr": message},
                extra={"result": False, "errorCode": 3, "task_id": job_id, "task_type": "local_bash", "state": state},
            ),
        }
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

@handler("claude_read")
def claude_read(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    import base64
    from pathlib import Path
    from remote_dev.core.bytes_ops import remote_read_bytes
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.media_ops import read_notebook, render_pdf
    from remote_dev.core.state_store import write_read_ledger
    from remote_dev.result import make_result

    suffix = Path(str(normalized["file_path"])).suffix.lower()
    endpoint = None
    if suffix in _IMAGE_EXTENSIONS or suffix == ".pdf":
        endpoint = resolve_endpoint(dict(normalized))
        max_bytes = 32 * 1024 * 1024 if suffix == ".pdf" else 16 * 1024 * 1024
        fetched = remote_read_bytes(endpoint, file_path=str(normalized["file_path"]), max_bytes=max_bytes)
        if fetched.get("status") != "ok":
            return _media_failure(spec.wire, fetched)
        if suffix == ".pdf":
            from remote_dev.core.media_ops import pdf_page_count

            counted = pdf_page_count(fetched["data"])
            total_pages = counted.get("page_count") if counted.get("status") == "ok" else None
            pages_arg = native.get("pages")
            if pages_arg is None and isinstance(total_pages, int) and total_pages > 10:
                return _media_failure(spec.wire, {
                    "status": "too_many_pages",
                    "error": f"PDF has {total_pages} pages; reads without pages are limited to 10 pages. Use pages to read specific pages.",
                })
            if pages_arg is None and isinstance(total_pages, int) and total_pages <= 10:
                text = f"PDF {normalized['file_path']} ({total_pages} page(s), full document).\n"
                result = make_result(
                    tool=spec.wire,
                    target=endpoint.to_result_target(),
                    outcome="success",
                    status="ok",
                    summary=f"RemoteRead PDF {normalized['file_path']} ({total_pages} page(s)).",
                    preview={"content": text},
                    extra={"path": normalized["file_path"], "page_count": total_pages, "full_pdf": True},
                )
                return {
                    "text": text,
                    "result": result,
                    "content": [{
                        "type": "pdf",
                        "data": base64.b64encode(fetched["data"]).decode("ascii"),
                        "mimeType": "application/pdf",
                    }],
                }
            rendered = render_pdf(
                fetched["data"], pages=pages_arg, max_pages=20, max_edge=1568, image_format="jpeg"
            )
            if rendered.get("status") != "ok":
                return _media_failure(spec.wire, rendered)
            pages = rendered["pages"]
            text = f"PDF {normalized['file_path']} rendered {len(pages)} page(s) locally.\n"
            result = make_result(
                tool=spec.wire,
                target=endpoint.to_result_target(),
                outcome="success",
                status="ok",
                summary=f"RemoteRead PDF {normalized['file_path']} ({len(pages)} page(s)).",
                preview={"content": text},
                extra={
                    "path": normalized["file_path"],
                    "page_count": total_pages,
                    "pages": [{"page": page["page"], "width": page.get("width"), "height": page.get("height")} for page in pages],
                    "renderer": rendered.get("renderer"),
                },
            )
            blocks = [
                {"type": "image", "data": base64.b64encode(page["data"]).decode("ascii"), "mimeType": page["mime_type"]}
                for page in pages
            ]
            return {"text": text, "result": result, "content": blocks}
        return _read_media_payload(normalized, spec_wire=spec.wire)
    if suffix == ".ipynb":
        endpoint = resolve_endpoint(dict(normalized))
        fetched = remote_read_bytes(endpoint, file_path=str(normalized["file_path"]), max_bytes=8 * 1024 * 1024)
        if fetched.get("status") != "ok":
            return _media_failure(spec.wire, fetched)
        parsed = read_notebook(fetched["data"])
        if parsed.get("status") != "ok":
            return _media_failure(spec.wire, parsed)
        try:
            from remote_dev.core.path_policy import join_under_root

            resolved = join_under_root(endpoint.root, endpoint.effective_cwd, str(normalized["file_path"]))
            write_read_ledger(
                endpoint,
                {
                    "path": str(resolved),
                    "sha256": fetched["sha256"],
                    "size": fetched["size"],
                    "mtime_ns": 0,
                    "content_verified": True,
                },
            )
        except (OSError, ValueError, RuntimeError):
            pass
        result = make_result(
            tool=spec.wire,
            target=endpoint.to_result_target(),
            outcome="success",
            status="ok",
            summary=f"RemoteRead notebook {normalized['file_path']} ({parsed['count']} cells).",
            preview={"content": parsed["text"][:4000]},
            extra={"cells": parsed["cells"], "cell_count": parsed["count"], "path": normalized["file_path"], "sha256": fetched["sha256"]},
        )
        return {"text": parsed["text"], "result": result}
    payload = call_canonical("remote.read", normalized)
    payload["result"]["tool"] = spec.wire
    return payload

@handler("claude_notebook_edit")
def claude_notebook_edit(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    from remote_dev.core.bytes_ops import remote_read_bytes
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.media_ops import edit_notebook
    from remote_dev.mcp.tools import _call_canonical
    from remote_dev.result import make_result

    endpoint = resolve_endpoint(dict(normalized))
    fetched = remote_read_bytes(endpoint, file_path=str(normalized["file_path"]), max_bytes=8 * 1024 * 1024)
    if fetched.get("status") != "ok":
        return _media_failure(spec.wire, fetched)
    edited = edit_notebook(
        fetched["data"],
        new_source=str(native["new_source"]),
        cell_id=native.get("cell_id"),
        cell_type=native.get("cell_type"),
        edit_mode=str(native.get("edit_mode") or "replace"),
    )
    if edited.get("status") != "ok":
        return _media_failure(spec.wire, edited)
    write_payload = _call_canonical(
        "remote.write",
        {
            **{key: normalized[key] for key in ("host", "port", "user", "container", "root", "cwd", "runtime_env", "identity_file", "connect_timeout_ms", "runtime_env_file", "alias") if key in normalized},
            "file_path": normalized["file_path"],
            "content": edited["data"].decode("utf-8"),
            "overwrite": True,
            "create_dirs": True,
        },
    )
    if write_payload.get("result", {}).get("outcome") != "success":
        return write_payload
    text = f"Updated notebook {normalized['file_path']} ({edited['count']} cells, {native.get('edit_mode') or 'replace'})."
    result = make_result(
        tool=spec.wire,
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=text,
        preview={"content": text},
        extra={"cells": edited["cells"], "cell_count": edited["count"], "edit_mode": native.get("edit_mode") or "replace", "path": normalized["file_path"]},
    )
    return {"text": text + "\n", "result": result}

@handler("claude_bash")
def claude_bash(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = call_canonical("remote.bash", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    preview = result.get("preview") or {}
    stdout = str(preview.get("stdout") or "")
    stderr = str(preview.get("stderr") or "")
    exit_code = result.get("exit_code")
    job_id = result.get("job_id") or result.get("session_id")
    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += stderr
    if state in {"prepared", "running"} and native.get("run_in_background") is True:
        body = body.rstrip() + f"\nbackgroundTaskId: {job_id}"
    elif exit_code not in (None, 0):
        body = body.rstrip() + f"\n[exit code: {exit_code}]"
    payload["text"] = body if body.endswith("\n") else body + "\n"
    result.update({"tool": spec.wire, "status": state, "backgroundTaskId": job_id if state in {"prepared", "running"} else None, "exit_code": exit_code})
    return payload

