"""
[Role] Shared native job/media helpers used by host handler modules.
[Invariants] Job scope checks, live status projection, bounded log tails, local media blocks.
[Timeouts & Locks] Delegates to canonical job control; no long local locks.
[Side-Effect Policy] Read-only helpers except explicit stop handlers in host modules.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from remote_dev.core.errors import caller_error
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

def _status_query_failed(result: Mapping[str, Any]) -> bool:
    """True when the status *query* failed, not when the job itself failed.

    ``remote.job_status`` keeps the last-known ``job.state`` even when its
    control connection fails (and adds ``job.error``/``error_details``).  A
    normal failed/absent job has a failed/absent state but no query error.
    Never treat the query status ``failed`` as the process state.
    """
    if result.get("outcome") != "failed":
        return False
    job = result.get("job") or {}
    return (
        job.get("error") is not None
        or result.get("error_details") is not None
        or not job.get("state")
    )


def _live_job(job_id: str, *, strict: bool = False) -> tuple[str, dict[str, Any]]:
    from remote_dev.mcp.tools import _call_canonical

    payload = _call_canonical("remote.job_status", {"job_id": job_id})
    result = payload.get("result") or {}
    job = result.get("job") or {}
    if _status_query_failed(result):
        detail = job.get("error") or result.get("error") or result.get("summary") or result.get("status")
        raise caller_error(f"remote job {job_id} status query failed: {detail}")
    state = str(job.get("state") or result.get("status") or "unknown")
    return state, job


def _job_result(job_id: str) -> dict[str, Any]:
    from remote_dev.mcp.tools import _call_canonical

    payload = _call_canonical("remote.job_status", {"job_id": job_id})
    payload["result"].setdefault("job", {})
    return payload

def _refresh_job_output(job_id: str) -> dict[str, Any] | None:
    """Pull pending supervisor output into the local job logs once.

    Native TaskOutput/TaskList read the task's current output without taking
    over the DSH-style consuming cursor. The canonical stdin poll with empty
    chars is the non-mutating observation path that commits new bytes to the
    local log files.  A failed pull is returned to the caller as the actual
    canonical failure; it must not be silently replaced by stale cached text.
    """
    from remote_dev.mcp.tools import _call_canonical

    try:
        payload = _call_canonical(
            "remote.job_stdin",
            {"job_id": job_id, "chars": "", "yield_time_ms": 0, "max_output_tokens": 8192},
        )
    except (ValueError, TimeoutError, OSError) as exc:
        from remote_dev.result import make_result

        message = f"task output unavailable: {type(exc).__name__}: {exc}"
        return {
            "text": message + "\n",
            "result": make_result(
                tool="remote.job_stdin",
                target={"kind": "local_state"},
                outcome="failed",
                status="output_unavailable",
                summary=message,
                preview={"stderr": message},
                extra={"error": str(exc)},
            ),
        }
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return payload if isinstance(payload, dict) else None
    if result.get("status") == "local_recording_failed":
        # The remote command was observed, but saving its output failed. Preserve
        # the canonical acknowledged-result envelope (execution_outcome,
        # operation_completed, real exit code); do not fall through to stale
        # cached text or a generic local OSError.
        return payload
    completed_command = (
        str(result.get("tool") or "").endswith(("bash", "job_stdin", "exec_command", "write_stdin", "shell"))
        and result.get("state") in {"succeeded", "failed", "timeout", "cancelled"}
        and type(result.get("exit_code")) is int
    )
    if result.get("outcome") == "failed" and not completed_command:
        return payload
    return None

def _job_file(path: Any, stream: str) -> Any:
    from pathlib import Path

    path = Path(path)
    return path.with_name(path.stem + f".{stream}.log")

def _tail_text(path: Any, limit: int = 32000) -> tuple[str, bool]:
    from pathlib import Path

    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "", False
    if size <= limit:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return "", False
        return data.decode("utf-8", errors="replace"), False
    try:
        with path.open("rb") as stream:
            stream.seek(size - limit)
            data = stream.read(limit)
    except FileNotFoundError:
        return "", False
    # The slice may begin inside a UTF-8 code point. Drop leading continuation
    # bytes so the retained tail decodes without a synthetic replacement char.
    offset = 0
    while offset < len(data) and (data[offset] & 0xC0) == 0x80:
        offset += 1
    return data[offset:].decode("utf-8", errors="replace"), True

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

def _media_failure(spec_wire: str, fetched: Mapping[str, Any]) -> dict[str, Any]:
    from remote_dev.result import make_result

    return {
        "text": f"{spec_wire}: {fetched.get('status')}: {fetched.get('error')}\n",
        "result": make_result(
            tool=spec_wire,
            target={"kind": "remote-file"},
            outcome="failed",
            status=str(fetched.get("status") or "failed"),
            summary=f"{spec_wire} failed.",
            preview={"stderr": str(fetched.get("error") or fetched.get("status"))},
            extra={"error": fetched.get("error")},
        ),
    }

def _read_media_payload(
    normalized: Mapping[str, Any],
    *,
    spec_wire: str,
    region: dict[str, Any] | None = None,
    max_edge: int | None = 1568,
) -> dict[str, Any]:
    from remote_dev.core.bytes_ops import remote_read_bytes
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.media_ops import render_image, render_video_thumbnail
    from remote_dev.result import make_result

    endpoint = resolve_endpoint(dict(normalized))
    from remote_dev.core.image_ops import remote_read_image

    fetched = {"status": "failed", "error": "not an image"}
    direct = remote_read_image(endpoint, file_path=str(normalized["file_path"]), max_bytes=16 * 1024 * 1024)
    if direct.get("result", {}).get("status") == "ok" and direct.get("content"):
        fetched = {
            "status": "ok",
            "data": base64.b64decode(direct["content"][0]["data"]),
            "mime_type": direct["result"].get("mime_type"),
            "path": direct["result"].get("path"),
        }
    else:
        fetched = remote_read_bytes(endpoint, file_path=str(normalized["file_path"]), max_bytes=16 * 1024 * 1024)
    if fetched.get("status") != "ok":
        return _media_failure(spec_wire, fetched)
    mime = str(fetched.get("mime_type") or "application/octet-stream")
    if mime.startswith("video/"):
        rendered = render_video_thumbnail(fetched["data"], max_edge=max_edge or 1280)
        rendered["video"] = True
    else:
        rendered = render_image(fetched["data"], mime_type=mime, region=region, max_edge=max_edge)
    if rendered.get("status") != "ok":
        return _media_failure(spec_wire, rendered)
    width = rendered.get("width")
    height = rendered.get("height")
    result = make_result(
        tool=spec_wire,
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=f"{spec_wire} {normalized['file_path']} ({rendered.get('mime_type')}, {width}x{height}).",
        preview={"content": f"media: {normalized['file_path']} ({rendered.get('mime_type')}, {width}x{height})"},
        extra={
            "path": str(fetched.get("path") or normalized["file_path"]),
            "mime_type": rendered.get("mime_type"),
            "size": len(rendered["data"]),
            "width": width,
            "height": height,
            "original_width": rendered.get("original_width"),
            "original_height": rendered.get("original_height"),
            "resized": bool(rendered.get("resized")),
            "cropped": bool(rendered.get("cropped")),
            "video": bool(rendered.get("video")),
        },
    )
    return {
        "text": result["summary"] + "\n",
        "result": result,
        "content": [{"type": "image", "data": base64.b64encode(rendered["data"]).decode("ascii"), "mimeType": rendered["mime_type"]}],
    }

