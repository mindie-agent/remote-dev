"""
[Role] Remote file operations (`remote.read`, `remote.ls`, `remote.write`, `remote.edit`, `remote.multi_edit`) (`file_ops.py`).
[Invariants]
- Loads the standalone `remote_file_worker.py` script via `functools.lru_cache(maxsize=1)` (Iron Rule 4 — no giant inline Python string blobs).
- Enforces `join_under_root` workspace containment and read-ledger SHA-256 optimistic concurrency on mutations.
- Preserves succeeded remote mutations if local read-ledger recording fails (`operation_completed=True` via `recording_failure`).
[Timeouts & Locks]
- Wraps mutating operations (`remote_write`, `remote_edit`, `remote_multi_edit`) in `@serialize_mutation` and passes `timeout_ms` through `run_remote_python`.
[Side-Effect Policy]
- `remote_read` updates the local read ledger on verified reads; mutating operations update remote files atomically and refresh the local read ledger.
"""
from __future__ import annotations

from functools import lru_cache
from remote_dev.observability import observed_tool

import time
from typing import Any
from .locking import serialize_mutation

from .endpoint import Endpoint
from .container_endpoint import pinned_endpoint
from .errors import PathPolicyError
from .path_policy import assert_under_root, join_under_root
from .preview import MAX_LINE_CHARS, MAX_READ_LINES, compact_text
from remote_dev.result import make_result, new_invocation_id, utc_now_iso
from .ssh_transport import run_remote_python
from .state_store import load_write_ledger_guard, resolve_ledger_scope, write_read_ledger

from pathlib import Path


@lru_cache(maxsize=1)
def _remote_file_source() -> str:
    return Path(__file__).with_name("remote_file_worker.py").read_text(encoding="utf-8")


REMOTE_FILE_PY = _remote_file_source()


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _status_to_outcome(status: str) -> str:
    if status in {"ok", "partial", "written", "edited"}:
        return "success"
    if status in {"read_required", "file_changed_since_read", "old_string_not_unique", "path_outside_root", "symlink_not_allowed", "file_exists"}:
        return "blocked"
    if status in {"path_required", "invalid_pagination", "parent_not_found", "invalid_flags"}:
        return "needs_input"
    return "failed"


@observed_tool("remote.read")
@pinned_endpoint
def remote_read(
    endpoint: Endpoint,
    *,
    file_path: str,
    offset: int = 1,
    limit: int = 200,
    start_line: int | None = None,
    end_line: int | None = None,
    content_offset: int | None = None,
    outline: bool = False,
    item_offset: int = 0,
    node_paths: list[str] | None = None,
    allow_symlink: bool = False,
    verify_content: bool = True,
    max_chars: int | None = None,
    column_offset: int = 0,
    pages: str | None = None,
    strict_offset: bool = False,
    client_context_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    warnings = []
    if start_line is not None and end_line is not None and end_line >= start_line >= 1:
        offset = int(start_line)
        limit = int(end_line) - int(start_line) + 1
    elif start_line is not None and start_line >= 1:
        offset = int(start_line)
    elif end_line is not None and end_line >= max(1, offset):
        limit = int(end_line) - max(1, offset) + 1
    if limit > MAX_READ_LINES:
        warnings.append(f"limit clamped from {limit} to {MAX_READ_LINES}")
        limit = MAX_READ_LINES
    if limit < 1:
        limit = 1
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.read", file_path, str(exc), started, start)
    payload_req: dict[str, Any] = {
        "op": "read",
        "root": endpoint.root,
        "cwd": endpoint.effective_cwd,
        "file_path": path,
        "offset": offset,
        "limit": limit,
        "allow_symlink": allow_symlink,
        "verify_content": verify_content,
        "max_line_chars": MAX_LINE_CHARS if max_chars is None else max(MAX_LINE_CHARS, min(int(max_chars), 500000)),
    }
    if max_chars is not None:
        payload_req["max_chars"] = int(max_chars)
    if column_offset:
        payload_req["column_offset"] = int(column_offset)
    if pages is not None:
        payload_req["pages"] = str(pages)
    if strict_offset:
        payload_req["strict_offset"] = True
    if start_line is not None:
        payload_req["start_line"] = int(start_line)
    if end_line is not None:
        payload_req["end_line"] = int(end_line)
    if content_offset is not None:
        payload_req["content_offset"] = int(content_offset)
    if outline:
        payload_req["outline"] = True
        payload_req["item_offset"] = int(item_offset)
    if node_paths:
        payload_req["node_paths"] = [str(item) for item in node_paths]
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        payload_req,
        timeout_ms=timeout_ms,
    )
    status = str(data.get("status", "failed"))
    refs: dict[str, Any] = {}
    ledger_scope = resolve_ledger_scope(client_context_id)
    recording_error = None
    if status in {"ok", "partial"} and isinstance(data.get("file"), dict) and data["file"].get("sha256"):
        try:
            ledger = write_read_ledger(endpoint, data["file"], client_context_id)
            refs["read_ledger"] = str(ledger)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            recording_error = exc
    file_info = data.get("file", {}) if isinstance(data.get("file"), dict) else {}
    warnings.extend(data.get("warnings", []) if isinstance(data.get("warnings"), list) else [])
    result = make_result(
        tool="remote.read",
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"RemoteRead {status} for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"content": compact_text(str(file_info.get("content", ""))), "partial": file_info.get("partial", False)},
        refs=refs,
        warnings=warnings,
        extra={"file": {k: v for k, v in file_info.items() if k != "content"}, "error": data.get("error"), "error_details": data.get("error_details"), "ledger_scope": ledger_scope},
    )
    text = _format_read_text(endpoint, result, file_info, max_chars=max_chars)
    payload = {"text": text, "result": result}
    if recording_error:
        from remote_dev.result import recording_failure
        return recording_failure(payload, recording_error, stage="read_ledger", operation_completed=True)
    return payload


@observed_tool("remote.ls")
@pinned_endpoint
def remote_stat(
    endpoint: Endpoint,
    *,
    file_path: str,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Return remote file existence/metadata without reading content."""
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return {"status": "path_outside_root", "error": str(exc), "exists": False}
    return run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {"op": "stat", "root": endpoint.root, "cwd": endpoint.effective_cwd, "file_path": path},
        timeout_ms=timeout_ms,
    )


def remote_ls(
    endpoint: Endpoint,
    *,
    path: str | None = None,
    limit: int = 200,
    all: bool = False,
    max_depth: int = 1,
    ignore_globs: list[str] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        resolved = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.ls", raw_path, str(exc), started, start)
    ls_req: dict[str, Any] = {
        "op": "ls",
        "root": endpoint.root,
        "cwd": endpoint.effective_cwd,
        "path": resolved,
        "limit": limit,
        "all": all,
    }
    if max_depth and max_depth > 1:
        ls_req["max_depth"] = int(max_depth)
    if ignore_globs:
        ls_req["ignore_globs"] = [str(g) for g in ignore_globs]
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        ls_req,
        timeout_ms=timeout_ms,
    )
    status = str(data.get("status", "failed"))
    entries = data.get("entries", []) if isinstance(data.get("entries"), list) else []
    result = make_result(
        tool="remote.ls",
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"RemoteLS {status} for {resolved}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"entries": entries, "truncated": bool(data.get("truncated", False))},
        extra={"path": data.get("path", resolved), "entries": entries, "truncated": bool(data.get("truncated", False)), "error": data.get("error"), "error_details": data.get("error_details")},
    )
    return {"text": _format_ls_text(endpoint, result), "result": result}


@observed_tool("remote.write")
@serialize_mutation
def remote_write(
    endpoint: Endpoint,
    *,
    file_path: str,
    content: str,
    overwrite: bool = False,
    append: bool = False,
    create_dirs: bool = False,
    client_context_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.write", file_path, str(exc), started, start)
    expected_sha, blocked = _expected_sha256_or_read_required(
        endpoint, path, client_context_id, "remote.write", started, start
    )
    if blocked:
        return blocked
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "write", "_mutation": True,
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "content": content,
            "overwrite": overwrite,
            "append": append,
            "create_dirs": create_dirs,
            "expected_sha256": expected_sha,
        },
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.write", path, data, started, start, client_context_id=client_context_id)


@observed_tool("remote.edit")
@serialize_mutation
def remote_edit(
    endpoint: Endpoint,
    *,
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    normalize_crlf: bool = False,
    start_line: int | None = None,
    end_line: int | None = None,
    client_context_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.edit", file_path, str(exc), started, start)
    expected_sha, blocked = _expected_sha256_or_read_required(
        endpoint, path, client_context_id, "remote.edit", started, start
    )
    if blocked:
        return blocked
    edit_req: dict[str, Any] = {
        "op": "edit", "_mutation": True,
        "root": endpoint.root,
        "cwd": endpoint.effective_cwd,
        "file_path": path,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
        "normalize_crlf": normalize_crlf,
        "expected_sha256": expected_sha,
    }
    if start_line is not None:
        edit_req["start_line"] = int(start_line)
    if end_line is not None:
        edit_req["end_line"] = int(end_line)
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        edit_req,
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.edit", path, data, started, start, client_context_id=client_context_id)


@observed_tool("remote.multi_edit")
@serialize_mutation
def remote_multi_edit(
    endpoint: Endpoint,
    *,
    file_path: str,
    edits: list[dict[str, Any]],
    client_context_id: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.multi_edit", file_path, str(exc), started, start)
    expected_sha, blocked = _expected_sha256_or_read_required(
        endpoint, path, client_context_id, "remote.multi_edit", started, start
    )
    if blocked:
        return blocked
    data = run_remote_python(
        endpoint,
        REMOTE_FILE_PY,
        {
            "op": "multi_edit", "_mutation": True,
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "file_path": path,
            "edits": edits,
            "expected_sha256": expected_sha,
        },
        timeout_ms=timeout_ms,
    )
    return _write_like_result(endpoint, "remote.multi_edit", path, data, started, start, client_context_id=client_context_id)


def _expected_sha256_or_read_required(
    endpoint: Endpoint,
    path: str,
    client_context_id: str | None,
    tool: str,
    started: str,
    start: float,
) -> tuple[str | None, dict[str, Any] | None]:
    guard = load_write_ledger_guard(endpoint, path, client_context_id)
    if guard.read_required:
        return None, _read_required_result(endpoint, tool, path, started, start, guard.scope)
    return (guard.ledger.get("sha256") if guard.ledger else None), None


def _read_required_result(
    endpoint: Endpoint,
    tool: str,
    path: str,
    started: str,
    start: float,
    ledger_scope: str,
) -> dict[str, Any]:
    error = "re-read this file in the same client context before writing; an older ledger scope is not authorization"
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome="blocked",
        status="read_required",
        summary=f"{tool} blocked for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"stderr": error},
        extra={"error": error, "ledger_scope": ledger_scope},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def _write_like_result(
    endpoint: Endpoint,
    tool: str,
    path: str,
    data: dict[str, Any],
    started: str,
    start: float,
    *,
    client_context_id: str | None = None,
) -> dict[str, Any]:
    status = str(data.get("status", "failed"))
    file_info = data.get("file", {}) if isinstance(data.get("file"), dict) else {}
    refs: dict[str, Any] = {}
    ledger_scope = resolve_ledger_scope(client_context_id)
    recording_error = None
    if status in {"written", "edited"} and file_info:
        try:
            refs["read_ledger"] = str(write_read_ledger(endpoint, file_info, client_context_id))
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            recording_error = exc
    changed = []
    if file_info:
        changed.append({
            "path": file_info.get("path", path),
            "before_sha256": data.get("before_sha256"),
            "after_sha256": data.get("after_sha256") or file_info.get("sha256"),
            "size": file_info.get("size"),
        })
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome=_status_to_outcome(status),
        status=status,
        summary=f"{tool} {status} for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"diff": data.get("diff_preview", "")},
        refs=refs,
        changed_files=changed,
        extra={"file": file_info, "error": data.get("error"), "error_details": data.get("error_details"), "ledger_scope": ledger_scope},
    )
    payload = {"text": _format_write_text(endpoint, result, data), "result": result}
    if recording_error:
        from remote_dev.result import recording_failure
        return recording_failure(payload, recording_error, stage="read_ledger", operation_completed=True)
    return payload


def _path_blocked_result(
    endpoint: Endpoint,
    tool: str,
    path: str,
    error: str,
    started: str,
    start: float,
) -> dict[str, Any]:
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome="blocked",
        status="path_outside_root",
        summary=f"{tool} blocked for {path}",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"stderr": error},
        extra={"error": error},
    )
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def _format_endpoint(endpoint: Endpoint) -> str:
    return f"{endpoint.user}@{endpoint.host}:{endpoint.port}"


def _format_read_text(
    endpoint: Endpoint,
    result: dict[str, Any],
    file_info: dict[str, Any],
    *,
    max_chars: int | None = None,
) -> str:
    lines = [
        f"RemoteRead on {_format_endpoint(endpoint)}",
        f"file: {file_info.get('path', '')}",
        f"lines: {file_info.get('line_start', 0)}-{file_info.get('line_end', 0)} of {file_info.get('total_lines', 0)}",
        f"partial: {str(file_info.get('partial', False)).lower()}",
        "",
        str(result.get("preview", {}).get("content", "")),
    ]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    rendered = "\n".join(lines).rstrip() + "\n"
    if max_chars is None:
        return compact_text(rendered)
    # Native Read budgets characters across content plus status. Keep the
    # canonical header but never fold or clip below the requested budget.
    return rendered if len(rendered) <= int(max_chars) else rendered[: int(max_chars)].rstrip() + "\n"


def _format_ls_text(endpoint: Endpoint, result: dict[str, Any]) -> str:
    entries = result.get("entries", [])
    lines = [f"RemoteLS on {_format_endpoint(endpoint)}", f"path: {result.get('path')}", ""]
    for entry in entries:
        lines.append(f"{entry.get('type', 'unknown'):10} {entry.get('size', 0):>10} {entry.get('name', '')}")
    if result.get("truncated"):
        lines.append("<truncated>")
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return compact_text("\n".join(lines).rstrip() + "\n")


def _format_write_text(endpoint: Endpoint, result: dict[str, Any], data: dict[str, Any]) -> str:
    lines = [
        f"{result['tool']} {result['status']} on {_format_endpoint(endpoint)}",
        f"file: {data.get('file', {}).get('path', '')}",
        "",
    ]
    if result.get("changed_files"):
        lines.append("Changed:")
        for item in result["changed_files"]:
            lines.append(f"  M {item.get('path')}")
        lines.extend(["", f"Before sha256: {data.get('before_sha256')}", f"After sha256:  {data.get('after_sha256')}", ""])
    if data.get("diff_preview"):
        lines.append("Diff preview:")
        lines.append(str(data["diff_preview"]))
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return compact_text("\n".join(lines).rstrip() + "\n")
