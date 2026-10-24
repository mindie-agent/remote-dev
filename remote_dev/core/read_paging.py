"""
[Role] Read response/pagination renderer for `remote.read` (`read_paging.py`).
[Invariants]
- Positions and budgets use JavaScript UTF-16 units for Kimi; text, Next Read, and structured continuation describe the same delivered body.
- Long-line continuations advance by the delivered body and never split a surrogate pair; a body that cannot fit the status is an explicit `max_chars_too_small` result.
- Does not perform I/O; caller supplies the worker's numbered content and `page_lines` metadata.
[Timeouts & Locks] No I/O, locks, or subprocesses.
[Side-Effect Policy] Read-only rendering; mutates only the supplied result/file_info dictionaries.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .preview import compact_text


_READ_LINE_MARKER = "<remote-dev line truncated>"


def _read_text_units(text: str, utf16_units: bool) -> int:
    if not utf16_units:
        return len(text)
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _take_read_units(text: str, units: int, utf16_units: bool) -> tuple[str, int]:
    """Take at most ``units`` JavaScript UTF-16 units without splitting a pair."""
    if units <= 0:
        return "", 0
    if not utf16_units:
        part = text[:units]
        return part, len(part)
    consumed = 0
    end = 0
    for index, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if consumed + width > units:
            return text[:index], consumed
        consumed += width
        end = index + 1
    return text[:end], consumed


def _read_header(
    endpoint_label: str,
    path: str,
    line_start: int,
    line_end: int,
    total_lines: object,
    partial: bool,
) -> str:
    return "\n".join([
        f"RemoteRead on {endpoint_label}",
        f"file: {path}",
        f"lines: {line_start}-{line_end} of {total_lines}",
        f"partial: {str(partial).lower()}",
        "",
    ])


def _next_read_arguments(
    path: str,
    line_offset: int,
    column_offset: int,
    max_chars: int,
    n_lines: int | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "path": path,
        "line_offset": int(line_offset),
        "column_offset": int(column_offset),
        "max_chars": int(max_chars),
    }
    if n_lines is not None:
        arguments["n_lines"] = int(n_lines)
    return arguments


def _next_read_footer(arguments: dict[str, Any]) -> str:
    return "Next Read: " + json.dumps(arguments, separators=(",", ":"))


def _read_page_entries(
    body: str,
    file_info: dict[str, Any],
    utf16_units: bool,
) -> list[dict[str, Any]] | None:
    """Map the numbered body back to the exact source lines it displays.

    The worker supplies ``page_lines`` metadata (line, absolute column, units,
    clipped).  Older/unusual renderings fall back to a conservative parse of
    the numbered lines; non-numbered shapes return ``None`` for the legacy
    formatter.
    """
    if not body:
        return []
    raw_lines = body.split("\n")
    metas = file_info.get("page_lines")
    if not isinstance(metas, list) or len(metas) != len(raw_lines):
        metas = None
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines):
        match = re.match(r"^(\d+) \| ", raw)
        if match is None:
            return None
        line_no = int(match.group(1))
        text_full = raw[match.end():]
        if metas is not None:
            meta = metas[index] if isinstance(metas[index], dict) else {}
            if int(meta.get("line", line_no)) != line_no:
                return None
            clipped = bool(meta.get("clipped", False))
            column_start = int(meta.get("column_start") or 0)
            if "body_units" in meta:
                body_units = int(meta.get("body_units") or 0)
                text, consumed = _take_read_units(text_full, body_units, utf16_units)
                # ``body_units`` is authoritative for stripping the marker.
                body_units = consumed
            else:
                clipped = clipped or text_full.endswith(_READ_LINE_MARKER)
                if clipped and text_full.endswith(_READ_LINE_MARKER):
                    text_full = text_full[: -len(_READ_LINE_MARKER)]
                text = text_full
                body_units = _read_text_units(text, utf16_units)
        else:
            clipped = text_full.endswith(_READ_LINE_MARKER)
            if clipped:
                text_full = text_full[: -len(_READ_LINE_MARKER)]
            text = text_full
            body_units = _read_text_units(text, utf16_units)
            column_start = int(file_info.get("column_offset") or 0) if index == 0 else 0
        entries.append({
            "index": index,
            "line": line_no,
            "column_start": column_start,
            "text": text,
            "full_text": text,
            "units": body_units,
            "full_units": body_units,
            "clipped": clipped,
        })
    return entries


def _read_range_end(file_info: dict[str, Any]) -> int:
    offset = int(file_info.get("offset") or 1)
    limit = max(1, int(file_info.get("limit") or 1))
    end = offset + limit - 1
    total = file_info.get("total_lines")
    if isinstance(total, int) and total >= 0:
        end = min(end, total)
    return end


def _read_next_cursor(
    delivered: list[dict[str, Any]],
    all_entries: list[dict[str, Any]],
    file_info: dict[str, Any],
    range_end: int,
) -> tuple[int, int, int | None] | None:
    if not delivered:
        return None
    if file_info.get("from_tail"):
        first = delivered[0]
        offset = int(file_info.get("offset") or 1)
        if first["line"] > offset:
            # Native tail pagination asks for the earlier lines that were
            # dropped from the front of the window.
            return (offset, 0, first["line"] - offset)
        last = delivered[-1]
        if last["units"] < last["full_units"]:
            return (last["line"], last["column_start"] + last["units"], None)
        if last["clipped"]:
            return (last["line"], last["column_start"] + last["full_units"], None)
        return None
    last = delivered[-1]
    if last["units"] < last["full_units"]:
        return (last["line"], last["column_start"] + last["units"], None)
    if last["clipped"]:
        return (last["line"], last["column_start"] + last["full_units"], None)
    index = int(last["index"])
    if index + 1 < len(all_entries):
        return (int(all_entries[index + 1]["line"]), 0, None)
    if last["line"] < range_end:
        return (int(last["line"]) + 1, 0, None)
    next_offset = file_info.get("next_offset")
    if isinstance(next_offset, int) and next_offset > last["line"]:
        return (next_offset, int(file_info.get("next_column_offset") or 0), None)
    return None


def _render_read_page(
    endpoint_label: str,
    path: str,
    total_lines: object,
    delivered: list[dict[str, Any]],
    next_cursor: tuple[int, int, int | None] | None,
    content_offset: int,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    line_start = int(delivered[0]["line"]) if delivered else 0
    line_end = int(delivered[-1]["line"]) if delivered else 0
    partial = bool(next_cursor) or bool(content_offset) or line_start > 1
    header = _read_header(endpoint_label, path, line_start, line_end, total_lines, partial)
    content = "\n".join(f"{entry['line']} | {entry['text']}" for entry in delivered)
    text = header + "\n" + content
    arguments = None
    if next_cursor is not None:
        arguments = _next_read_arguments(
            path, next_cursor[0], next_cursor[1], max_chars, next_cursor[2],
        )
        text += "\n" + _next_read_footer(arguments)
    text += "\n"
    return text, {
        "line_start": line_start,
        "line_end": line_end,
        "partial": partial,
        "content": content,
        "next": arguments,
        "truncated_line_count": sum(1 for entry in delivered if entry["clipped"]),
    }


def _read_too_small(
    result: dict[str, Any],
    file_info: dict[str, Any],
    max_chars: int,
) -> str:
    message = f"max_chars={max_chars} is too small for file text and the Read status"
    result["outcome"] = "needs_input"
    result["status"] = "max_chars_too_small"
    result["summary"] = f"RemoteRead needs_input for {file_info.get('path', '')}"
    result["error"] = message
    result["file"] = {k: v for k, v in file_info.items() if k != "page_lines"}
    result["preview"] = {}
    result["next"] = None
    return result["summary"] + "\n" + message + "\n"


def _format_tail_read(
    endpoint_label: str,
    result: dict[str, Any],
    file_info: dict[str, Any],
    body: str,
    *,
    max_chars: int,
    utf16_units: bool,
) -> str:
    """Tail reads keep trailing whole lines; drop front lines when over budget."""
    path = str(file_info.get("path", ""))
    entries = _read_page_entries(body, file_info, utf16_units)
    if entries is None:
        return _format_legacy_read(
            endpoint_label, result, file_info, body,
            max_chars=max_chars, utf16_units=utf16_units,
        )
    if not entries:
        return _read_too_small(result, file_info, max_chars)
    if entries[-1]["clipped"]:
        return _read_too_small(result, file_info, max_chars)
    total = file_info.get("total_lines")
    range_end = _read_range_end(file_info)
    content_offset = int(file_info.get("content_offset") or 0)
    header_probe = _read_header(
        endpoint_label, path, int(entries[-1]["line"]), int(entries[-1]["line"]), total, True,
    )
    footer_probe = _next_read_footer(_next_read_arguments(path, 10**9, 10**9, max_chars))
    body_budget = int(max_chars) - _read_text_units(header_probe, utf16_units) - 3 - _read_text_units(footer_probe, utf16_units)
    if body_budget <= 0:
        return _read_too_small(result, file_info, max_chars)
    delivered: list[dict[str, Any]] = []
    remaining = body_budget
    for entry in reversed(entries):
        prefix_width = len(str(entry["line"])) + 3
        separator = 1 if delivered else 0
        if separator + prefix_width + entry["full_units"] > remaining:
            break
        delivered.append({**entry, "text": entry["full_text"], "units": entry["full_units"]})
        remaining -= separator + prefix_width + entry["full_units"]
    delivered.reverse()
    if not delivered or any(entry["clipped"] for entry in delivered):
        return _read_too_small(result, file_info, max_chars)
    next_cursor = _read_next_cursor(delivered, entries, file_info, range_end)
    text, page = _render_read_page(
        endpoint_label, path, total, delivered, next_cursor, content_offset, max_chars,
    )
    for _ in range(16):
        if _read_text_units(text, utf16_units) <= max_chars:
            return _finalize_read_page(result, file_info, text, page, utf16_units)
        if len(delivered) <= 1 or any(entry["clipped"] for entry in delivered):
            return _read_too_small(result, file_info, max_chars)
        delivered.pop(0)
        next_cursor = _read_next_cursor(delivered, entries, file_info, range_end)
        text, page = _render_read_page(
            endpoint_label, path, total, delivered, next_cursor, content_offset, max_chars,
        )
    return _read_too_small(result, file_info, max_chars)


def _format_paged_read(
    endpoint_label: str,
    result: dict[str, Any],
    file_info: dict[str, Any],
    body: str,
    *,
    max_chars: int,
    utf16_units: bool,
) -> str:
    path = str(file_info.get("path", ""))
    entries = _read_page_entries(body, file_info, utf16_units)
    if entries is None:
        return _format_legacy_read(endpoint_label, result, file_info, body, max_chars=max_chars, utf16_units=utf16_units)
    if not entries and file_info.get("next_offset") is not None:
        return _read_too_small(result, file_info, max_chars)
    total = file_info.get("total_lines")
    if file_info.get("from_tail"):
        return _format_tail_read(
            endpoint_label, result, file_info, body,
            max_chars=max_chars, utf16_units=utf16_units,
        )
    if not entries:
        line_start = int(file_info.get("line_start", 0))
        line_end = int(file_info.get("line_end", 0))
        partial = bool(file_info.get("partial", False))
        header = _read_header(endpoint_label, path, line_start, line_end, total, partial)
        page = {
            "line_start": line_start,
            "line_end": line_end,
            "partial": partial,
            "content": "",
            "next": None,
            "truncated_line_count": 0,
        }
        return _finalize_read_page(result, file_info, header + "\n" + "\n", page, utf16_units)
    range_end = _read_range_end(file_info)
    content_offset = int(file_info.get("content_offset") or 0)

    # If the whole page fits without dropping content, deliver it unchanged.
    full_next = _read_next_cursor(entries, entries, file_info, range_end)
    full_text, full_page = _render_read_page(
        endpoint_label, path, total, entries, full_next, content_offset, max_chars,
    )
    if _read_text_units(full_text, utf16_units) <= max_chars:
        return _finalize_read_page(result, file_info, full_text, full_page, utf16_units)

    # Reserve the longest possible Next Read footer.  The formatter may then
    # spend the rest of the budget on body content without ever cutting the
    # status text or advertising a cursor that does not match the body.
    header_width = _read_text_units(
        _read_header(endpoint_label, path, 0, 0, total, True), utf16_units
    )
    footer_probe = _next_read_footer(_next_read_arguments(path, 10**9, 10**9, max_chars))
    body_budget = int(max_chars) - header_width - 1 - 1 - _read_text_units(footer_probe, utf16_units) - 1
    if body_budget <= 0:
        return _read_too_small(result, file_info, max_chars)

    delivered: list[dict[str, Any]] = []
    remaining = body_budget
    for entry in entries:
        prefix_width = len(str(entry["line"])) + 3
        separator = 1 if delivered else 0
        if remaining < separator + prefix_width:
            break
        room = remaining - separator - prefix_width
        if room >= entry["full_units"]:
            delivered.append({**entry, "text": entry["full_text"], "units": entry["full_units"]})
            remaining -= separator + prefix_width + entry["full_units"]
            continue
        part, consumed = _take_read_units(entry["full_text"], room, utf16_units)
        if consumed <= 0:
            if not delivered:
                return _read_too_small(result, file_info, max_chars)
            break
        delivered.append({**entry, "text": part, "units": consumed})
        break
    if not delivered:
        return _read_too_small(result, file_info, max_chars)

    next_cursor = _read_next_cursor(delivered, entries, file_info, range_end)
    text, page = _render_read_page(
        endpoint_label, path, total, delivered, next_cursor, content_offset, max_chars,
    )
    # The footer probe is an upper bound, but keep this loop as a hard guard
    # against any future status/header shape change.
    for _ in range(16):
        if _read_text_units(text, utf16_units) <= max_chars:
            break
        last = delivered[-1]
        if last["units"] > 0:
            part, consumed = _take_read_units(last["text"], last["units"] - 1, utf16_units)
            if consumed > 0:
                last["text"] = part
                last["units"] = consumed
            else:
                delivered.pop()
        else:
            delivered.pop()
        if not delivered:
            return _read_too_small(result, file_info, max_chars)
        next_cursor = _read_next_cursor(delivered, entries, file_info, range_end)
        text, page = _render_read_page(
            endpoint_label, path, total, delivered, next_cursor, content_offset, max_chars,
        )
    return _finalize_read_page(result, file_info, text, page, utf16_units)


def _finalize_read_page(
    result: dict[str, Any],
    file_info: dict[str, Any],
    text: str,
    page: dict[str, Any],
    utf16_units: bool,
) -> str:
    _ = utf16_units
    content = str(page["content"])
    file_info["content"] = content
    file_info["line_start"] = int(page["line_start"])
    file_info["line_end"] = int(page["line_end"])
    file_info["partial"] = bool(page["partial"])
    file_info["truncated_line_count"] = int(page.get("truncated_line_count") or 0)
    if page["next"] is not None:
        file_info["next_offset"] = int(page["next"]["line_offset"])
        file_info["next_column_offset"] = int(page["next"]["column_offset"])
    else:
        file_info.pop("next_offset", None)
        file_info.pop("next_column_offset", None)
    file_info.pop("page_lines", None)
    result["file"] = dict(file_info)
    result["preview"] = {"content": compact_text(content), "partial": bool(page["partial"])}
    result["status"] = "partial" if page["partial"] else "ok"
    result["summary"] = f"RemoteRead {result['status']} for {file_info.get('path', '')}"
    result["next"] = page["next"]
    return text


def _format_legacy_read(
    endpoint_label: str,
    result: dict[str, Any],
    file_info: dict[str, Any],
    body: str,
    *,
    max_chars: int,
    utf16_units: bool,
) -> str:
    """Fallback for non-numbered shapes (outline/symbol reads).

    These shapes have no per-line continuation contract; when the budget cuts
    them, the stale cursor is removed rather than advertised as valid.
    """
    path = str(file_info.get("path", ""))
    header = _read_header(
        endpoint_label, path,
        int(file_info.get("line_start", 0)),
        int(file_info.get("line_end", 0)),
        file_info.get("total_lines", 0),
        True,
    )
    footer = ""
    arguments = None
    if file_info.get("next_offset") is not None:
        arguments = _next_read_arguments(
            path,
            int(file_info["next_offset"]),
            int(file_info.get("next_column_offset") or 0),
            max_chars,
        )
        footer = "\n" + _next_read_footer(arguments)
    # text = header + "\n" + body + footer + "\n"; header already ends in \n.
    room = int(max_chars) - _read_text_units(header, utf16_units) - 1 - _read_text_units(footer, utf16_units) - 1
    if room < 0 or (body and room == 0):
        return _read_too_small(result, file_info, max_chars)
    cut, consumed = _take_read_units(body, room, utf16_units)
    if body and consumed <= 0:
        return _read_too_small(result, file_info, max_chars)
    trimmed = consumed < _read_text_units(body, utf16_units)
    if trimmed:
        arguments = None
        footer = ""
        file_info["partial"] = True
        file_info.pop("next_offset", None)
        file_info.pop("next_column_offset", None)
        warnings = result.get("warnings")
        if isinstance(warnings, list):
            warnings.append("continuation cursor omitted for a non-numbered read that hit max_chars")
    file_info["content"] = cut
    result["file"] = {k: v for k, v in file_info.items() if k != "page_lines"}
    result["preview"] = {"content": compact_text(cut), "partial": True if trimmed else bool(file_info.get("partial"))}
    result["status"] = "partial" if result["preview"]["partial"] else "ok"
    result["summary"] = f"RemoteRead {result['status']} for {path}"
    result["next"] = arguments
    rendered = header + "\n" + cut + footer + "\n"
    if _read_text_units(rendered, utf16_units) > max_chars:
        return _read_too_small(result, file_info, max_chars)
    return rendered


def format_read_text(
    endpoint_label: str,
    result: dict[str, Any],
    file_info: dict[str, Any],
    *,
    max_chars: int | None = None,
    utf16_units: bool = False,
) -> str:
    body = (
        str(file_info.get("content", ""))
        if max_chars is not None
        else str(result.get("preview", {}).get("content", ""))
    )
    if max_chars is None:
        lines = [
            f"RemoteRead on {endpoint_label}",
            f"file: {file_info.get('path', '')}",
            f"lines: {file_info.get('line_start', 0)}-{file_info.get('line_end', 0)} of {file_info.get('total_lines', 0)}",
            f"partial: {str(file_info.get('partial', False)).lower()}",
            "",
            body,
        ]
        if result.get("error"):
            lines.append(f"error: {result['error']}")
        return compact_text("\n".join(lines).rstrip() + "\n")
    return _format_paged_read(
        endpoint_label, result, file_info, body,
        max_chars=int(max_chars), utf16_units=utf16_units,
    )


