"""
[Role] Remote workspace search operations (`remote.glob` and `remote.grep`) (`search_ops.py`).
[Invariants]
- Loads the standalone `search_worker.py` script via `functools.lru_cache(maxsize=1)` (Iron Rule 4 — no giant inline Python string blobs).
- Enforces `join_under_root` containment and bounded line/match pagination (`MAX_GREP_MATCHES`, `MAX_LINE_CHARS`).
- Uses `rg` when available on the remote endpoint and falls back to POSIX `grep -E` with explicit capability/regex translation checks.
[Timeouts & Locks]
- Read-only RPC execution bounded by caller `timeout_ms`; subprocess streams in `command_lines` terminate cleanly on early pagination exit.
[Side-Effect Policy]
- Pure remote read/search operations; mutates no local or remote files.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from remote_dev.observability import observed_tool

import time
from typing import Any

from .endpoint import Endpoint
from .container_endpoint import pinned_endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from .preview import MAX_GREP_MATCHES, MAX_LINE_CHARS, MAX_TEXT_CHARS, compact_text
from remote_dev.result import make_result, utc_now_iso
from .ssh_transport import run_remote_python


@lru_cache(maxsize=1)
def _remote_search_source() -> str:
    return Path(__file__).with_name("search_worker.py").read_text(encoding="utf-8")


REMOTE_SEARCH_PY = _remote_search_source()


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _compact_matches(matches: list[Any]) -> tuple[list[str], bool]:
    """Fit matches into the text budget, reserving continuation/header room.

    The final text adds one newline per match plus a ``<continue ...>`` header
    or ``<truncated>`` footer. Budget those bytes here so the joined text is
    never shortened later without a continuation offset.
    """
    visible: list[str] = []
    used = 0
    truncated = False
    budget = MAX_TEXT_CHARS - 64
    for item in matches:
        text = str(item)
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "<remote-dev line truncated>"
            truncated = True
        if used + len(text) + (1 if visible else 0) > budget:
            truncated = True
            break
        visible.append(text)
        used += len(text) + 1
    return visible, truncated or len(visible) < len(matches)


def _effective_next_offset(offset: int, visible_count: int, match_count: int, worker_next: Any) -> int | None:
    """Advance only when this formatter actually dropped matches.

    The worker computes a continuation offset before ``MAX_TEXT_CHARS`` is
    applied. Re-budget against the delivered page so a caller following the
    offset never skips matches that were cut from the text.
    """
    if visible_count >= match_count:
        return int(worker_next) if worker_next is not None else None
    candidate = offset + visible_count
    if worker_next is None:
        return candidate
    return min(candidate, int(worker_next))


@observed_tool("remote.glob")
@pinned_endpoint
def remote_glob(
    endpoint: Endpoint,
    *,
    pattern: str = "*",
    path: str | None = None,
    limit: int = 100,
    offset: int = 0,
    respect_gitignore: bool = False,
    include_ignored: bool = False,
    extensions: list[str] | None = None,
    excludes: list[str] | None = None,
    entry_type: str | None = None,
    max_depth: int | None = None,
    full_path: bool = False,
    hidden: bool = False,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.glob", raw_path, str(exc), started, start)
    glob_req: dict[str, Any] = {
        "op": "glob",
        "root": endpoint.root,
        "cwd": endpoint.effective_cwd,
        "path": base,
        "pattern": pattern or "*",
        "limit": limit,
        "offset": offset,
        "respect_gitignore": False if include_ignored else respect_gitignore,
        "hidden": bool(hidden),
    }
    if extensions:
        glob_req["extensions"] = [str(e) for e in extensions]
    if excludes:
        glob_req["excludes"] = [str(e) for e in excludes]
    if entry_type:
        glob_req["entry_type"] = str(entry_type)
    if max_depth is not None:
        glob_req["max_depth"] = int(max_depth)
    if full_path:
        glob_req["full_path"] = True
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        glob_req,
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = data.get("warnings", []) if isinstance(data.get("warnings"), list) else []
    visible_matches, text_truncated = _compact_matches([str(item.get("path", item)) for item in matches])
    next_offset = _effective_next_offset(offset, len(visible_matches), len(matches), data.get("next_offset"))
    result = make_result(
        tool="remote.glob",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGlob found {len(matches)} paths." if status == "ok" else f"RemoteGlob failed ({status}).",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated},
        warnings=warnings,
        extra={"matches": visible_matches, "next_offset": next_offset, "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error"), "error_details": data.get("error_details")},
    )
    if next_offset is not None:
        text = f"<continue offset={next_offset}>\n" + compact_text("\n".join(visible_matches))
    else:
        footer = "\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"
        text = compact_text("\n".join(visible_matches) + footer)
    if status != "ok":
        text = result["summary"] + "\n" + str(data.get("error") or "Search failed.") + "\n" + text.lstrip()
    caveats = [
        warning for warning in warnings
        if "grep fallback" in warning and ("gitignore" in warning or "approximate" in warning)
    ]
    if data.get("engine") == "grep" and caveats:
        text = text.rstrip("\n") + "\n" + "\n".join(caveats) + "\n"
    return {"text": text, "result": result}


@observed_tool("remote.grep")
@pinned_endpoint
def remote_grep(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    type: str | None = None,
    output_mode: str = "files_with_matches",
    multiline: bool = False,
    case_insensitive: bool = False,
    fixed_strings: bool = False,
    only_matching: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    context_lines: int = 0,
    line_numbers: bool | None = None,
    include_ignored: bool = False,
    hidden: bool = False,
    offset: int = 0,
    limit: int = 100,
    sort_by_mtime: bool = False,
    require_rg: bool = False,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    if output_mode not in {"files_with_matches", "content", "count", "count_matches"}:
        local_warnings = [f"unknown output_mode {output_mode!r}; using files_with_matches"]
        output_mode = "files_with_matches"
    else:
        local_warnings = []
    if limit == 0:
        limit = MAX_GREP_MATCHES
    if limit > MAX_GREP_MATCHES:
        local_warnings.append(f"limit clamped from {limit} to {MAX_GREP_MATCHES}")
        limit = MAX_GREP_MATCHES
    if limit < 1:
        limit = 1
    if offset < 0:
        offset = 0
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.grep", raw_path, str(exc), started, start)
    grep_req: dict[str, Any] = {
        "op": "grep",
        "root": endpoint.root,
        "cwd": endpoint.effective_cwd,
        "path": base,
        "pattern": pattern,
        "glob": glob,
        "type": type,
        "output_mode": output_mode,
        "multiline": multiline,
        "case_insensitive": case_insensitive,
        "before_context": before_context,
        "after_context": after_context,
        "context_lines": context_lines,
        "line_numbers": line_numbers,
        "include_ignored": include_ignored,
        "hidden": hidden,
        "offset": offset,
        "limit": limit,
        "max_line_chars": MAX_LINE_CHARS,
        "sort_by_mtime": bool(sort_by_mtime),
        "require_rg": bool(require_rg),
    }
    if fixed_strings:
        grep_req["fixed_strings"] = True
    if includes:
        grep_req["includes"] = [str(i) for i in includes]
    if excludes:
        grep_req["excludes"] = [str(e) for e in excludes]
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        grep_req,
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = local_warnings + (data.get("warnings", []) if isinstance(data.get("warnings"), list) else [])
    visible_matches, text_truncated = _compact_matches(matches)
    next_offset = _effective_next_offset(offset, len(visible_matches), len(matches), data.get("next_offset"))
    result = make_result(
        tool="remote.grep",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGrep found {len(matches)} matches." if status == "ok" else f"RemoteGrep failed ({status}).",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated},
        warnings=warnings,
        extra={"matches": visible_matches, "engine": data.get("engine"), "output_mode": output_mode, "offset": offset, "next_offset": next_offset, "total_matches": data.get("total_matches"), "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error"), "error_details": data.get("error_details")},
    )
    if next_offset is not None:
        text = f"<continue offset={next_offset}>\n" + compact_text("\n".join(visible_matches))
    else:
        footer = "\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"
        text = compact_text("\n".join(visible_matches) + footer)
    if status != "ok":
        text = result["summary"] + "\n" + str(data.get("error") or "Search failed.") + "\n" + text.lstrip()
    caveats = [
        warning for warning in warnings
        if "grep fallback" in warning and ("gitignore" in warning or "approximate" in warning)
    ]
    if data.get("engine") == "grep" and caveats:
        text = text.rstrip("\n") + "\n" + "\n".join(caveats) + "\n"
    return {"text": text, "result": result}


def _path_blocked_result(endpoint: Endpoint, tool: str, path: str, error: str, started: str, start: float) -> dict[str, Any]:
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
