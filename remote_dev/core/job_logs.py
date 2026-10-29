"""
[Role] One bounded head+tail formatter and truthful full-log reference builder for background job output (`job_logs.py`).
[Invariants]
- Command/Bash/TaskOutput previews show a bounded UTF-8-safe head+tail with explicit omitted char/byte counts and a clear note.
- Repeated consecutive-line folding is reused from `preview.py` and only appears when it genuinely shrinks text.
- Structured refs distinguish the authoritative remote raw logs from the local cached output. A partial or failed local cache is never described as the full raw log.
- Formatting is presentation only: it never advances a cursor, consumes remote bytes, or rewrites a local record.
[Timeouts & Locks]
- Pure string formatting plus local `stat()` of cache paths; no SSH, subprocesses, locks, or long local I/O.
[Side-Effect Policy]
- Read-only local inspection. Never writes logs, records, cursors, or remote state.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from remote_dev.core.preview import DEFAULT_HEAD_CHARS, DEFAULT_TAIL_CHARS, _tail_slice, fold_repeated_lines

DEFAULT_JOB_HEAD_CHARS = DEFAULT_HEAD_CHARS
DEFAULT_JOB_TAIL_CHARS = DEFAULT_TAIL_CHARS

_STREAM_NAMES = ("stdout", "stderr")

__all__ = [
    "DEFAULT_JOB_HEAD_CHARS",
    "DEFAULT_JOB_TAIL_CHARS",
    "format_job_log_output",
    "job_log_refs",
    "local_stream_preview",
    "preview_stream_edges",
    "preview_stream_exact",
    "preview_stream_text",
    "render_job_refs_text",
]


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def _omission_note(
    omitted_chars: int,
    omitted_bytes: int,
    *,
    chars_exact: bool,
    folded_chars: int = 0,
    folded_bytes: int = 0,
) -> str:
    if chars_exact:
        counts = f"{omitted_chars:,} characters ({omitted_bytes:,} UTF-8 bytes)"
    else:
        # A bounded file-edge read knows the exact byte gap but cannot count the
        # skipped UTF-8 characters without reading the whole cache. One UTF-8
        # byte can encode at most one character, so this is a truthful lower
        # bound rather than a fabricated exact count.
        counts = f"{omitted_bytes:,} UTF-8 bytes (at least {omitted_chars:,} characters)"
    folded = ""
    if folded_chars:
        folded = (
            f" Repeated-line folding also condensed {folded_chars:,} characters "
            f"({folded_bytes:,} UTF-8 bytes)."
        )
    return (
        f"\n<remote-dev preview: omitted {counts} from the middle of this stream; "
        f"showing a bounded head+tail only, not the full log."
        f"{folded} Full raw log references are listed below.>\n"
    )


def _stream_payload(
    *,
    text: str,
    head: str,
    tail: str,
    total_bytes: int,
    total_chars: int | None,
    shown_chars: int,
    shown_bytes: int,
    omitted_chars: int,
    omitted_bytes: int,
    omitted_chars_exact: bool,
    folded_chars: int,
    folded_bytes: int,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "text": text,
        "head": head,
        "tail": tail,
        "bytes": total_bytes,
        "chars": total_chars,
        "shown_chars": shown_chars,
        "shown_bytes": shown_bytes,
        "omitted_chars": omitted_chars,
        "omitted_bytes": omitted_bytes,
        "omitted_chars_exact": bool(omitted_chars_exact),
        "repeated_lines_folded": bool(folded_chars),
        "folded_chars": max(0, folded_chars),
        "folded_bytes": max(0, folded_bytes),
        "truncated": bool(truncated),
    }


def preview_stream_text(
    value: str,
    *,
    head_chars: int = DEFAULT_JOB_HEAD_CHARS,
    tail_chars: int = DEFAULT_JOB_TAIL_CHARS,
) -> dict[str, Any]:
    """Return a bounded head+tail preview of one already-materialized stream."""
    value = value or ""
    head_chars = max(0, int(head_chars))
    tail_chars = max(0, int(tail_chars))
    total_bytes = _byte_len(value)
    folded = fold_repeated_lines(value)
    folded_chars = max(0, len(value) - len(folded))
    folded_bytes = max(0, total_bytes - _byte_len(folded))
    if len(folded) <= head_chars + tail_chars:
        return _stream_payload(
            text=folded,
            head=folded,
            tail="",
            total_bytes=total_bytes,
            total_chars=len(value),
            shown_chars=len(folded),
            shown_bytes=_byte_len(folded),
            omitted_chars=0,
            omitted_bytes=0,
            omitted_chars_exact=True,
            folded_chars=folded_chars,
            folded_bytes=folded_bytes,
            truncated=False,
        )
    head = folded[:head_chars]
    tail = _tail_slice(folded, tail_chars)
    omitted_end = len(folded) - len(tail)
    omitted = folded[head_chars:omitted_end]
    note = _omission_note(
        len(omitted),
        _byte_len(omitted),
        chars_exact=True,
        folded_chars=folded_chars,
        folded_bytes=folded_bytes,
    )
    return _stream_payload(
        text=head + note + tail,
        head=head,
        tail=tail,
        total_bytes=total_bytes,
        total_chars=len(value),
        shown_chars=len(head) + len(tail),
        shown_bytes=_byte_len(head) + _byte_len(tail),
        omitted_chars=len(omitted),
        omitted_bytes=_byte_len(omitted),
        omitted_chars_exact=True,
        folded_chars=folded_chars,
        folded_bytes=folded_bytes,
        truncated=True,
    )


def preview_stream_exact(value: str) -> dict[str, Any]:
    """Return one complete already-bounded page without folding or head/tail cuts.

    Incremental readers advance their cursor over the whole page, so omitting
    any middle bytes (or folding repeated lines) would make that page
    unreconstructable through the cursor. Callers pass a page already bounded
    by ``max_output_tokens`` / ``max_bytes``.
    """
    value = value or ""
    total_bytes = _byte_len(value)
    return _stream_payload(
        text=value,
        head=value,
        tail="",
        total_bytes=total_bytes,
        total_chars=len(value),
        shown_chars=len(value),
        shown_bytes=total_bytes,
        omitted_chars=0,
        omitted_bytes=0,
        omitted_chars_exact=True,
        folded_chars=0,
        folded_bytes=0,
        truncated=False,
    )


def preview_stream_edges(
    head: str,
    tail: str,
    *,
    total_bytes: int,
    head_chars: int = DEFAULT_JOB_HEAD_CHARS,
    tail_chars: int = DEFAULT_JOB_TAIL_CHARS,
) -> dict[str, Any]:
    """Format already-bounded head/tail edges of a large local log file.

    ``head`` and ``tail`` are decoded byte edges, not the whole stream. The
    omitted byte count is exact for the displayed source edges; the omitted
    character count is a lower bound because counting the skipped UTF-8
    characters would require reading the omitted region.
    """
    head_chars = max(0, int(head_chars))
    tail_chars = max(0, int(tail_chars))
    head_source = (head or "")[:head_chars]
    tail_source = _tail_slice(tail or "", tail_chars)
    head_folded = fold_repeated_lines(head_source)
    tail_folded = fold_repeated_lines(tail_source)
    head_source_bytes = _byte_len(head_source)
    tail_source_bytes = _byte_len(tail_source)
    folded_chars = (
        max(0, len(head_source) - len(head_folded))
        + max(0, len(tail_source) - len(tail_folded))
    )
    folded_bytes = (
        max(0, head_source_bytes - _byte_len(head_folded))
        + max(0, tail_source_bytes - _byte_len(tail_folded))
    )
    total_bytes = max(0, int(total_bytes))
    omitted_bytes = max(0, total_bytes - head_source_bytes - tail_source_bytes)
    # A UTF-8 character occupies at least one byte, so this is a lower bound.
    omitted_chars = (omitted_bytes + 3) // 4
    note = _omission_note(
        omitted_chars,
        omitted_bytes,
        chars_exact=False,
        folded_chars=folded_chars,
        folded_bytes=folded_bytes,
    )
    return _stream_payload(
        text=head_folded + note + tail_folded,
        head=head_folded,
        tail=tail_folded,
        total_bytes=total_bytes,
        total_chars=None,
        shown_chars=len(head_folded) + len(tail_folded),
        shown_bytes=_byte_len(head_folded) + _byte_len(tail_folded),
        omitted_chars=omitted_chars,
        omitted_bytes=omitted_bytes,
        omitted_chars_exact=False,
        folded_chars=folded_chars,
        folded_bytes=folded_bytes,
        truncated=bool(omitted_bytes or folded_chars),
    )


def local_stream_preview(
    path: str | Path,
    *,
    head_chars: int = DEFAULT_JOB_HEAD_CHARS,
    tail_chars: int = DEFAULT_JOB_TAIL_CHARS,
) -> dict[str, Any]:
    """Bounded head+tail view of a local cached stream file.

    Small files are read once so omitted character counts stay exact. Large
    files use bounded first/last reads; the omitted character count is a
    truthful lower bound and the omitted byte count is exact from ``stat``.
    Never reads a >4 MiB cache or a >50 MB log in full.
    """
    import codecs

    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        return preview_stream_text("", head_chars=head_chars, tail_chars=tail_chars)
    if size <= 4 * 1024 * 1024:
        data = path.read_bytes()
        return preview_stream_text(data.decode("utf-8", errors="replace"),
                                   head_chars=head_chars, tail_chars=tail_chars)
    head_limit = max(0, min(16384, size))
    with path.open("rb") as stream:
        head_data = stream.read(head_limit)
    head_decoder = codecs.getincrementaldecoder("utf-8")("replace")
    head = head_decoder.decode(head_data, final=False)
    tail_limit = max(0, min(32768, size))
    with path.open("rb") as stream:
        stream.seek(size - tail_limit)
        tail_data = stream.read(tail_limit)
    offset = 0
    while offset < len(tail_data) and (tail_data[offset] & 0xC0) == 0x80:
        offset += 1
    tail = tail_data[offset:].decode("utf-8", errors="replace")
    return preview_stream_edges(
        head, tail, total_bytes=size,
        head_chars=head_chars, tail_chars=tail_chars,
    )


def _as_stream(
    value: str | Mapping[str, Any],
    *,
    head_chars: int,
    tail_chars: int,
    preserve_full_page: bool = False,
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        stream = dict(value)
        stream.setdefault("text", "")
        stream.setdefault("head", "")
        stream.setdefault("tail", "")
        stream.setdefault("bytes", _byte_len(str(stream["text"])))
        stream.setdefault("chars", None)
        stream.setdefault("shown_chars", len(str(stream["text"])))
        stream.setdefault("shown_bytes", _byte_len(str(stream["text"])))
        stream.setdefault("omitted_chars", 0)
        stream.setdefault("omitted_bytes", 0)
        stream.setdefault("omitted_chars_exact", True)
        stream.setdefault("repeated_lines_folded", False)
        stream.setdefault("folded_chars", 0)
        stream.setdefault("folded_bytes", 0)
        stream.setdefault("truncated", False)
        return stream
    if preserve_full_page:
        return preview_stream_exact(str(value or ""))
    return preview_stream_text(str(value or ""), head_chars=head_chars, tail_chars=tail_chars)


def _cache_phrase(stream: Mapping[str, Any]) -> str:
    status = str(stream.get("status") or "unknown")
    cached = stream.get("cached_bytes")
    expected = stream.get("expected_bytes")
    missing = int(stream.get("missing_bytes") or 0)
    if status == "complete":
        return f"complete: {cached} bytes cached"
    if status == "partial":
        detail = f"cached {cached} of {expected} observed remote bytes; {missing} not yet cached"
        return f"partial: {detail}"
    if status == "unreliable":
        return "unreliable: local recording failed; may miss observed bytes"
    if status == "missing":
        return "missing: the remote raw log is authoritative"
    return status


def render_job_refs_text(refs: Mapping[str, Any]) -> str:
    """Render readable retrieval instructions from structured job refs."""
    refs = refs or {}
    lines: list[str] = []
    remote_stdout = refs.get("remote_raw_stdout")
    remote_stderr = refs.get("remote_raw_stderr")
    if remote_stdout or remote_stderr:
        lines.append("Full raw logs (authoritative; use remote.read with offset/limit for pagination):")
        if remote_stdout:
            lines.append(f"  remote raw stdout: {remote_stdout}")
        if remote_stderr:
            lines.append(f"  remote raw stderr: {remote_stderr}")
    local_record = refs.get("job_record")
    local_stdout = refs.get("local_cached_stdout")
    local_stderr = refs.get("local_cached_stderr")
    if local_record or local_stdout or local_stderr:
        lines.append("Local cached output (not authoritative; it can lag the remote raw log):")
        if local_record:
            lines.append(f"  local job record: {local_record}")
        status = refs.get("local_cache_status")
        if status:
            lines.append(f"  local cache status: {status}")
        streams = refs.get("local_cache_streams") or {}
        for name, path in (("stdout", local_stdout), ("stderr", local_stderr)):
            if not path:
                continue
            lines.append(f"  local cached {name}: {path} [{_cache_phrase(streams.get(name) or {})}]")
    resources = [
        refs.get("stdout_resource"),
        refs.get("stderr_resource"),
    ]
    if any(resources):
        lines.append("MCP job-log resources (readable bounded view):")
        for name, uri in zip(_STREAM_NAMES, resources):
            if uri:
                lines.append(f"  {name} resource: {uri}")
    controls = [
        refs.get("job_output"),
        refs.get("job_tail"),
    ]
    if any(controls):
        lines.append("Continue: " + " | ".join(str(item) for item in controls if item))
    hint = refs.get("remote_read_hint")
    if hint:
        lines.append(str(hint))
    return "\n".join(lines)


def format_job_log_output(
    stdout: str | Mapping[str, Any] = "",
    stderr: str | Mapping[str, Any] = "",
    *,
    refs: Mapping[str, Any] | None = None,
    head_chars: int = DEFAULT_JOB_HEAD_CHARS,
    tail_chars: int = DEFAULT_JOB_TAIL_CHARS,
    preserve_full_page: bool = False,
) -> dict[str, Any]:
    """The single formatter for job command logs used by core and native paths.

    ``preserve_full_page`` is for consuming/incremental readers whose cursor
    advances over the whole page. Those pages are already bounded by the
    caller's byte budget, so this mode never cuts or folds them.
    """
    head_chars = max(0, int(head_chars))
    tail_chars = max(0, int(tail_chars))
    out_stream = _as_stream(
        stdout, head_chars=head_chars, tail_chars=tail_chars,
        preserve_full_page=preserve_full_page,
    )
    err_stream = _as_stream(
        stderr, head_chars=head_chars, tail_chars=tail_chars,
        preserve_full_page=preserve_full_page,
    )
    structured_refs = dict(refs or {})
    refs_text = render_job_refs_text(structured_refs)
    parts: list[str] = []
    if out_stream["text"]:
        parts.append(f"__STDOUT__\n{out_stream['text']}")
    if err_stream["text"]:
        parts.append(f"__STDERR__\n{err_stream['text']}")
    if refs_text:
        parts.append(refs_text)
    text = "\n".join(part.rstrip("\n") for part in parts)
    if text:
        text += "\n"
    omitted_chars_exact = bool(
        out_stream.get("omitted_chars_exact", True)
        and err_stream.get("omitted_chars_exact", True)
    )
    return {
        "preview": {"stdout": out_stream["text"], "stderr": err_stream["text"]},
        "text": text,
        "refs": structured_refs,
        "refs_text": refs_text,
        "streams": {"stdout": out_stream, "stderr": err_stream},
        "truncated": bool(out_stream["truncated"] or err_stream["truncated"]),
        "repeated_lines_folded": bool(
            out_stream["repeated_lines_folded"] or err_stream["repeated_lines_folded"]
        ),
        "omitted_chars": int(out_stream["omitted_chars"]) + int(err_stream["omitted_chars"]),
        "omitted_bytes": int(out_stream["omitted_bytes"]) + int(err_stream["omitted_bytes"]),
        "omitted_chars_exact": omitted_chars_exact,
        "folded_chars": int(out_stream["folded_chars"]) + int(err_stream["folded_chars"]),
        "folded_bytes": int(out_stream["folded_bytes"]) + int(err_stream["folded_bytes"]),
        "head_chars": head_chars,
        "tail_chars": tail_chars,
    }


def _record_cursors(record: Mapping[str, Any]) -> dict[str, int]:
    cursors: dict[str, int] = {}
    for key in ("output_cursors", "stdin_cursors"):
        source = record.get(key)
        if isinstance(source, Mapping):
            for name in _STREAM_NAMES:
                value = source.get(name + "_offset")
                if value is not None and name not in cursors:
                    cursors[name] = int(value)
    return cursors


def _cache_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except TimeoutError:
        raise
    except OSError:
        return None


def job_log_refs(
    *,
    job_id: str,
    record_path: str | Path,
    record: Mapping[str, Any],
    endpoint_id: str | None = None,
    observed_offsets: Mapping[str, int] | None = None,
    unread_bytes: Mapping[str, int] | None = None,
    local_recording_error: str | None = None,
) -> dict[str, Any]:
    """Build truthful refs for a job's full remote logs and local cache."""
    record = record or {}
    record_path = Path(record_path)
    job_id = str(job_id or record.get("job_id") or "")
    remote_dir = str(record.get("remote_dir") or "")
    target = record.get("target") if isinstance(record.get("target"), Mapping) else {}
    if endpoint_id is None:
        endpoint_id = target.get("endpoint_id") if isinstance(target, Mapping) else None
    local_paths = {
        name: record_path.with_name(record_path.stem + f".{name}.log")
        for name in _STREAM_NAMES
    }
    observed = dict(_record_cursors(record))
    if isinstance(observed_offsets, Mapping):
        for name in _STREAM_NAMES:
            if observed_offsets.get(name) is not None:
                observed[name] = int(observed_offsets[name])
    unread = {
        name: max(0, int((unread_bytes or {}).get(name) or 0))
        for name in _STREAM_NAMES
    }
    streams: dict[str, dict[str, Any]] = {}
    for name in _STREAM_NAMES:
        size = _cache_size(local_paths[name])
        cursor = max(0, int(observed.get(name) or 0))
        remaining = unread[name]
        expected = cursor + remaining
        if local_recording_error:
            status = "unreliable"
            missing = expected if size is None else max(0, expected - size)
        elif size is None:
            status = "missing"
            missing = expected
        elif size < expected:
            status = "partial"
            missing = expected - size
        elif remaining:
            status = "partial"
            missing = remaining
        else:
            status = "complete"
            missing = 0
        streams[name] = {
            "status": status,
            "path": str(local_paths[name]),
            "cached_bytes": size,
            "remote_observed_bytes": cursor,
            "unread_bytes": remaining,
            "expected_bytes": expected,
            "missing_bytes": max(0, missing),
        }
    statuses = {stream["status"] for stream in streams.values()}
    if local_recording_error:
        overall_status = "unreliable"
    elif statuses == {"complete"}:
        overall_status = "complete"
    elif statuses == {"missing"}:
        overall_status = "missing"
    elif statuses & {"partial", "missing"}:
        overall_status = "partial"
    else:
        overall_status = "unknown"
    if local_recording_error:
        note = (
            "Local cache is unreliable because local recording failed; it may be "
            "incomplete or stale. The remote raw logs remain authoritative."
        )
    elif overall_status == "complete":
        note = "Local cache contains all bytes observed through the saved cursor; remote raw logs remain authoritative."
    else:
        note = (
            "Local cache is partial (it may only contain the first polled pages). "
            "The remote raw logs remain authoritative; poll the job or read the raw log to continue."
        )
    refs: dict[str, Any] = {
        "job_record": str(record_path),
        "remote_dir": remote_dir or None,
        "remote_raw_stdout": f"{remote_dir}/stdout.log" if remote_dir else None,
        "remote_raw_stderr": f"{remote_dir}/stderr.log" if remote_dir else None,
        "local_cached_stdout": str(local_paths["stdout"]),
        "local_cached_stderr": str(local_paths["stderr"]),
        # Compatibility aliases: these are local cache paths, never the remote raw logs.
        "stdout": str(local_paths["stdout"]),
        "stderr": str(local_paths["stderr"]),
        "local_cache_status": overall_status,
        "local_cache_streams": streams,
        "local_cache_note": note,
        "remote_read_hint": (
            "Use remote.read on remote_raw_stdout/remote_raw_stderr with offset/limit for "
            "paginated access; local cached output is not the raw remote log."
        ),
    }
    if endpoint_id and job_id:
        refs["stdout_resource"] = f"remote://endpoint/{endpoint_id}/job/{job_id}/stdout"
        refs["stderr_resource"] = f"remote://endpoint/{endpoint_id}/job/{job_id}/stderr"
    if job_id:
        refs["job_output"] = f'remote.job_output(job_id="{job_id}")'
        refs["job_tail"] = f'remote.job_tail(job_id="{job_id}", lines=500)'
    if local_recording_error:
        refs["local_recording_error"] = local_recording_error
    return refs
