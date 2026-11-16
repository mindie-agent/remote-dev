"""
[Role] Truthful per-stream output accounting for capped remote job logs (`job_output.py`).
[Invariants]
- `CappedLog` publishes `truncated_head_bytes` and `logical_bytes_written`; the retained range is
  always `[truncated_head_bytes, logical_bytes_written)` and can never be reported as zero when the
  ring rotated its head away.
- Public `truncated`/`omitted_bytes` counts combine ring head loss with bytes omitted from the
  bounded preview of the retained window; they are distinct ranges and are added, never merged.
- Stored record metadata is superseded by a newer remote observation, but a missing observation
  never erases a known non-zero truncation offset.
[Timeouts & Locks]
- Pure in-memory arithmetic over caller-supplied mappings; no I/O, locks, or waiting.
[Side-Effect Policy]
- Read-only projection; never writes records, logs, cursors, or remote state.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JOB_OUTPUT_SCHEMA_VERSION = "remote-dev.job.output.v1"
STREAM_NAMES = ("stdout", "stderr")


def _non_negative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def output_log_accounting(
    record: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-stream logical/retained byte accounting for a stored job record."""
    record = record if isinstance(record, Mapping) else {}
    row = row if isinstance(row, Mapping) else {}
    stored = record.get("output_logs")
    stored = stored if isinstance(stored, Mapping) else {}
    cursor_offsets: dict[str, int] = {}
    for key in ("output_cursors", "stdin_cursors"):
        source = record.get(key)
        if not isinstance(source, Mapping):
            continue
        for name in STREAM_NAMES:
            value = _non_negative_int(source.get(name + "_offset"))
            if value is not None and name not in cursor_offsets:
                cursor_offsets[name] = value
    accounting: dict[str, dict[str, Any]] = {}
    for name in STREAM_NAMES:
        entry = stored.get(name)
        entry = entry if isinstance(entry, Mapping) else {}
        logical = _non_negative_int(entry.get("logical_bytes_written"))
        truncated = _non_negative_int(entry.get("truncated_head_bytes"))
        offset = _non_negative_int(entry.get("last_offset"))
        observed_logical = _non_negative_int(row.get(name + "_logical_bytes"))
        observed_truncated = _non_negative_int(row.get(name + "_truncated_head_bytes"))
        observed_offset = _non_negative_int(row.get(name + "_offset"))
        if observed_logical is not None:
            logical = observed_logical
        if observed_truncated is not None:
            truncated = observed_truncated
        if observed_offset is not None:
            offset = observed_offset
        elif offset is None:
            offset = cursor_offsets.get(name)
        logical = 0 if logical is None else logical
        truncated = min(0 if truncated is None else truncated, logical)
        accounting[name] = {
            "logical_bytes_written": logical,
            "truncated_head_bytes": truncated,
            "retained_start": truncated,
            "retained_end": logical,
            "retained_bytes": max(0, logical - truncated),
            "last_offset": offset,
        }
    return accounting


def observed_accounting_warning(row: Mapping[str, Any] | None) -> str | None:
    """Extract an explicit worker log-accounting warning from an observation."""
    row = row if isinstance(row, Mapping) else {}
    result = row.get("result")
    logs = result.get("logs") if isinstance(result, Mapping) else None
    if not isinstance(logs, Mapping):
        return None
    warning = logs.get("accounting_warning")
    if warning:
        return str(warning)
    parts = []
    for name in STREAM_NAMES:
        entry = logs.get(name)
        if isinstance(entry, Mapping) and entry.get("accounting_warning"):
            parts.append(f"{name}: {entry['accounting_warning']}")
    return "; ".join(parts) or None


def merge_output_logs(
    record: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None = None,
    *,
    warning: str | None = None,
) -> dict[str, Any]:
    """Build the persisted `output_logs` projection from stored + observed state."""
    record = record if isinstance(record, Mapping) else {}
    stored = record.get("output_logs")
    stored = stored if isinstance(stored, Mapping) else {}
    result = {
        "schema_version": JOB_OUTPUT_SCHEMA_VERSION,
        **output_log_accounting(record, row),
    }
    resolved = warning or observed_accounting_warning(row) or stored.get("accounting_warning")
    if resolved:
        result["accounting_warning"] = str(resolved)
    return result


def head_truncation(
    record: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report bytes the capped remote ring rotated away before this observation."""
    accounting = output_log_accounting(record, row)
    streams = {
        name: {
            "truncated_head_bytes": entry["truncated_head_bytes"],
            "logical_bytes_written": entry["logical_bytes_written"],
            "retained_range": [entry["retained_start"], entry["retained_end"]],
        }
        for name, entry in accounting.items()
    }
    return {
        "bytes": sum(stream["truncated_head_bytes"] for stream in streams.values()),
        "streams": streams,
    }


def output_truncation(
    record: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None,
    formatted: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine ring head-loss with preview tail-slice loss into public counts.

    `formatted["omitted_bytes"]` only describes bytes omitted from the bounded
    preview of the retained window; head bytes rotated away by `CappedLog` were
    never in that window. Both are distinct omissions and must be added.
    """
    head = head_truncation(record, row)
    preview_streams = formatted.get("streams") if isinstance(formatted, Mapping) else {}
    preview_streams = preview_streams if isinstance(preview_streams, Mapping) else {}
    streams: dict[str, dict[str, Any]] = {}
    omitted_total = 0
    for name in STREAM_NAMES:
        preview = preview_streams.get(name)
        preview = preview if isinstance(preview, Mapping) else {}
        preview_omitted = _non_negative_int(preview.get("omitted_bytes")) or 0
        head_bytes = head["streams"][name]["truncated_head_bytes"]
        omitted_total += head_bytes + preview_omitted
        streams[name] = {
            **head["streams"][name],
            "preview_omitted_bytes": preview_omitted,
            "omitted_bytes": head_bytes + preview_omitted,
        }
    return {
        "truncated": bool(omitted_total or formatted.get("truncated")),
        "omitted_bytes": omitted_total,
        "head_truncated_bytes": head["bytes"],
        "streams": streams,
    }


__all__ = [
    "JOB_OUTPUT_SCHEMA_VERSION",
    "STREAM_NAMES",
    "head_truncation",
    "merge_output_logs",
    "observed_accounting_warning",
    "output_log_accounting",
    "output_truncation",
]
