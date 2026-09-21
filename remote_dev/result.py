from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any, Literal

Outcome = Literal["success", "needs_input", "blocked", "failed", "timeout", "cancelled"]

RESULT_SCHEMA_VERSION = "remote-dev.result.v1"
MAX_FAILURE_FIELD_CHARS = 4000
MAX_FAILURE_TEXT_CHARS = 8000
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")

__all__ = [
    "Outcome",
    "RESULT_SCHEMA_VERSION",
    "dumps",
    "format_failure_text",
    "make_result",
    "new_invocation_id",
    "result_schema_path",
    "tool_text",
    "utc_now_iso",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_invocation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def result_schema_path():
    """Return the packaged ``result.schema.json`` as an importlib resource path."""
    return files("remote_dev.schemas").joinpath("result.schema.json")


def make_result(
    *,
    tool: str,
    target: dict[str, Any],
    outcome: Outcome,
    status: str,
    summary: str,
    invocation_id: str | None = None,
    started_at: str | None = None,
    duration_ms: int | None = None,
    preview: dict[str, Any] | None = None,
    refs: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    changed_files: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    next: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from remote_dev.observability import current_tool
    operation = current_tool()
    payload: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "tool": tool,
        "invocation_id": invocation_id or (operation["op"].operation_id if operation else new_invocation_id()),
        "target": target,
        "outcome": outcome,
        "status": status,
        "summary": summary,
        "started_at": started_at or (operation["started_at"] if operation else utc_now_iso()),
        "duration_ms": duration_ms,
        "preview": preview or {},
        "refs": refs or {},
        "artifacts": artifacts or [],
        "changed_files": changed_files or [],
        "warnings": warnings or [],
        "next": next,
    }
    if extra:
        payload.update(extra)
    return payload


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _bound_text(value: object, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _safe_label(value: object) -> str | None:
    text = str(value or "").strip()
    if _SAFE_LABEL.fullmatch(text):
        return text
    return None


def _phase_hint(diagnostics: dict[str, Any]) -> str | None:
    phases = diagnostics.get("phases")
    if not isinstance(phases, list) or not phases:
        return None
    last = phases[-1]
    if isinstance(last, str):
        return _safe_label(last)
    if isinstance(last, dict):
        for key in ("id", "name", "stage", "phase"):
            label = _safe_label(last.get(key))
            if label:
                return label
    return None


def format_failure_text(payload: dict[str, Any]) -> str:
    """Actionable failure text from explicit public result fields only."""
    details = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    text = str(payload.get("text") or "")
    completed_command = (
        details.get("tool") in {"remote.bash", "remote.job_stdin"}
        and details.get("state") in {"succeeded", "failed", "timeout", "cancelled"}
        and type(details.get("exit_code")) is int
    )
    if details.get("outcome") in {"success", "cancelled"} or completed_command:
        # Exit status is already in the command's text. Its output has its own
        # byte budget/cursor: truncating it here would silently consume data.
        return text
    diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
    operation_id = _safe_label(diagnostics.get("operation_id"))
    phase = _phase_hint(diagnostics)
    location = " ".join(part for part in (f"operation_id={operation_id}" if operation_id else "", f"phase={phase}" if phase else "") if part)
    if text.startswith("Remote tool failed ("):
        # Outer observation may add diagnostics after the payload was formed.
        # Keep formatting idempotent without bypassing the size bound or losing
        # the completed diagnostic phase.
        additions = [part for part in location.split() if part not in text]
        suffix = ("\n" + " ".join(additions)) if additions else ""
        return _bound_text(text.rstrip(), MAX_FAILURE_TEXT_CHARS - len(suffix) - 1) + suffix + "\n"
    status = _safe_label(details.get("status") or details.get("outcome")) or "failed"
    job = details.get("job") if isinstance(details.get("job"), dict) else {}
    error_info = details.get("error_details") if isinstance(details.get("error_details"), dict) else {}
    parts = [f"Remote tool failed ({status})."]
    seen = {text.strip()}

    def add(value: object | None) -> None:
        if value is None:
            return
        piece = _bound_text(value, MAX_FAILURE_FIELD_CHARS).strip()
        if piece and piece not in seen and piece not in text:
            seen.add(piece)
            parts.append(piece)

    add(details.get("summary"))
    job_id = details.get("job_id") or details.get("session_id") or job.get("job_id")
    if job_id is not None and str(job_id).strip():
        add(f"job_id: {job_id}")
    stage_bits = []
    category = _safe_label(error_info.get("category"))
    submission = _safe_label(error_info.get("submission_state"))
    if category:
        stage_bits.append(f"category={category}")
    if submission:
        stage_bits.append(f"submission_state={submission}")
    if stage_bits:
        add(" ".join(stage_bits))
    add(details.get("error") or job.get("error"))
    if location:
        add(location)
    if text.strip():
        parts.append(_bound_text(text.rstrip(), MAX_FAILURE_FIELD_CHARS))
    rendered = "\n".join(parts) + "\n"
    return _bound_text(rendered, MAX_FAILURE_TEXT_CHARS)


def tool_text(payload: dict[str, Any]) -> str:
    """Keep failures actionable for clients that only consume MCP text."""
    return format_failure_text(payload)
