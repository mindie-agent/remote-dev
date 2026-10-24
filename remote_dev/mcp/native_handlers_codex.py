"""
[Role] Codex 0.153.4 native handlers (exec_command, write_stdin, view_image).
[Invariants] Numeric session ids, native output shapes, real content blocks.
[Timeouts & Locks] Canonical job control owns deadlines.
[Side-Effect Policy] Commands are remote effects; no automatic replay.
"""

from __future__ import annotations

from typing import Any, Mapping

from remote_dev.mcp.harness_profiles import NativeToolSpec
from remote_dev.mcp.native_runtime import CanonicalCall, handler
from remote_dev.mcp.native_common import _media_failure, _read_media_payload

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
    import time

    # Codex models sessions with a numeric id. Allocate one at launch so
    # write_stdin can round-trip the exact native type instead of inventing a
    # string alias.
    normalized = dict(normalized)
    normalized.setdefault("job_id", str(time.time_ns()))
    payload = call_canonical("remote.bash", normalized)
    result = payload.get("result") or {}
    state = str(result.get("state") or result.get("status") or "unknown")
    session_id = result.get("session_id") or result.get("job_id")
    native_session_id = int(session_id) if str(session_id).isdigit() else session_id
    exit_code = result.get("exit_code")
    output = _codex_output(result)
    duration = float(result.get("duration_ms") or 0) / 1000.0
    lines = [f"Chunk ID: {session_id or 'unknown'}", f"Wall time: {duration:.4f} seconds"]
    if state in {"prepared", "running"}:
        lines.append(f"Process running with session ID {native_session_id}")
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
        **({"session_id": native_session_id} if state in {"prepared", "running"} else {}),
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

@handler("codex_view_image")
def codex_view_image(profile: str, spec: NativeToolSpec, native: dict[str, Any], normalized: dict[str, Any], call_canonical: CanonicalCall) -> dict[str, Any]:
    payload = _read_media_payload(
        normalized, spec_wire=spec.wire, max_edge=None if native.get("detail") == "original" else 1568
    )
    if payload.get("content"):
        mime = payload["result"].get("mime_type") or "image/png"
        data = payload["content"][0]["data"]
        payload["result"].update({
            "image_url": f"data:{mime};base64,{data}",
            "detail": native.get("detail") or "high",
        })
    return payload

