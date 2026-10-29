"""
[Role] Standalone remote Python helper module executed over one-shot SSH for file read/write/list/stat (`remote_file_worker.py`).
[Invariants]
- Maintained with `remote_file_text.py` as ordinary Python modules; cached transport source bundles both without remote package imports.
- Enforces workspace root containment, symlink escape detection, and optimistic `expected_mtime_ns` concurrency checks.
[Timeouts & Locks]
- Executed within the caller's bounded SSH command timeout.
[Side-Effect Policy]
- `read`, `list`, and `stat` are pure queries; `write` stages to `<target>.remote-dev.tmp.<pid>`, flushes + `fsync`s, and commits via `os.replace()`.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile


# Direct execution with python -I loads only this trusted sibling file. The
# transport bundles its source and removes this import block before dispatch.
if __package__:
    from .remote_file_text import (
        fail,
        _stream_sha256,
        _detect_utf16,
        _seems_binary_utf8,
        _utf16_lines,
        _utf16_width,
        _utf16_index,
        _text_width,
        _take_text_units,
        _page_budget,
        _page_content,
        _page_metadata,
        _tail_budget,
        scan_text_lines,
        extract_symbols,
    )
else:
    import importlib.util

    _text_spec = importlib.util.spec_from_file_location(
        "remote_file_text", pathlib.Path(__file__).with_name("remote_file_text.py"))
    _text = importlib.util.module_from_spec(_text_spec)
    _text_spec.loader.exec_module(_text)
    fail = _text.fail
    _stream_sha256 = _text._stream_sha256
    _detect_utf16 = _text._detect_utf16
    _seems_binary_utf8 = _text._seems_binary_utf8
    _utf16_lines = _text._utf16_lines
    _utf16_width = _text._utf16_width
    _utf16_index = _text._utf16_index
    _text_width = _text._text_width
    _take_text_units = _text._take_text_units
    _page_budget = _text._page_budget
    _page_content = _text._page_content
    _page_metadata = _text._page_metadata
    _tail_budget = _text._tail_budget
    scan_text_lines = _text.scan_text_lines
    extract_symbols = _text.extract_symbols


def resolve_path(
    raw: str,
    root: pathlib.Path,
    cwd: pathlib.Path,
    *,
    existing_parent_ok: bool = False,
) -> tuple[pathlib.Path, pathlib.Path]:
    if not raw:
        fail("path_required", "path is required")
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        if p.exists() or p.is_symlink():
            resolved = p.resolve()
        elif existing_parent_ok:
            resolved = p.parent.resolve() / p.name
        else:
            resolved = p.resolve(strict=True)
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    return p, resolved


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_info(path: pathlib.Path, *, content: bytes | None = None) -> dict[str, object]:
    st = path.stat()
    data = path.read_bytes() if content is None else content
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "sha256": sha256_bytes(data),
        "size": len(data),
        "mtime_ns": st.st_mtime_ns,
    }


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    if path.is_symlink():
        fail("symlink_not_allowed", f"refusing to write symlink: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def unified(before: str, after: str, path: pathlib.Path) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
    )[:12000]


def main() -> None:
    payload = json.loads(sys.stdin.read())
    op = payload["op"]
    root = pathlib.Path(payload["root"]).resolve()
    cwd = pathlib.Path(payload.get("cwd") or payload["root"])

    if op == "stat":
        path, resolved = resolve_path(payload["file_path"], root, cwd)
        if not path.exists() and not path.is_symlink():
            print(json.dumps({"status": "missing", "path": str(path), "exists": False}, sort_keys=True))
            raise SystemExit(0)
        if path.is_symlink():
            print(json.dumps({"status": "symlink", "path": str(path), "exists": True, "is_file": path.is_file()}, sort_keys=True))
            raise SystemExit(0)
        info = file_info(path) if path.is_file() else {"path": str(path)}
        print(json.dumps({"status": "ok", "path": str(path), "exists": True, "is_file": path.is_file(), **info}, sort_keys=True))
        raise SystemExit(0)

    if op == "read":
        path, resolved = resolve_path(payload["file_path"], root, cwd)
        if path.is_dir():
            fail("is_directory", f"RemoteRead reads files, not directories: {path}")
        if path.suffix.lower() == ".pdf":
            fail("pdf_unsupported", "PDF page rendering is not available over remote-dev; use remote_artifact_pull and a local PDF reader")
        start_line = payload.get("start_line")
        end_line = payload.get("end_line")
        offset = int(payload.get("offset") or 1)
        limit = int(payload.get("limit") or 200)
        if start_line is not None and end_line is not None:
            s_ln = int(start_line)
            e_ln = int(end_line)
            if s_ln < 1 or e_ln < s_ln:
                fail("invalid_pagination", f"invalid line range: start_line={s_ln}, end_line={e_ln}")
            offset = s_ln
            limit = e_ln - s_ln + 1
        elif start_line is not None:
            s_ln = int(start_line)
            if s_ln < 1:
                fail("invalid_pagination", f"start_line must be >= 1, got {s_ln}")
            offset = s_ln
        elif end_line is not None:
            e_ln = int(end_line)
            s_ln = max(1, offset)
            if e_ln < s_ln:
                fail("invalid_pagination", f"end_line ({e_ln}) must be >= start_line ({s_ln})")
            offset = s_ln
            limit = e_ln - s_ln + 1
        content_offset = int(payload.get("content_offset") or 0)
        if content_offset < 0:
            fail("invalid_pagination", "content_offset must be >= 0")
        if limit < 1 or offset == 0:
            fail("invalid_pagination", "limit must be positive and offset must not be zero")
        max_line_chars = int(payload.get("max_line_chars") or 2000)
        max_chars = int(payload["max_chars"]) if payload.get("max_chars") is not None else None
        column_offset = int(payload.get("column_offset") or 0)
        utf16_units = bool(payload.get("utf16_units", False))
        if column_offset and offset < 0:
            fail("invalid_pagination", "column_offset is only supported for forward reads")
        if column_offset < 0:
            fail("invalid_pagination", "column_offset must be >= 0")
        verify = bool(payload.get("verify_content", True))
        outline = bool(payload.get("outline", False))
        node_paths = payload.get("node_paths")
        before = path.stat()

        encoding = None if payload.get("utf8_only") else _detect_utf16(path)
        kimi_encoding = bool(payload.get("utf16_units"))
        if kimi_encoding and encoding in {"utf-16-le", "utf-16-be"} and path.stat().st_size > 10485760:
            fail("file_too_large", "Kimi Read rejects UTF-16 files larger than 10485760 bytes")
        if encoding == "binary":
            fail("binary_file", "remote.read detected binary data; use remote.artifact_pull for image/binary files")
        if encoding is not None:
            digest_hex = _stream_sha256(path) if verify else None
            total = 0
            selected_lines = []
            warnings = []
            lossy_total = 0
            forward = offset > 0
            start_index = max(0, offset - 1) if forward else 0
            selected_units = 0
            collecting = True
            for line_no, (line_text, lossy) in enumerate(_utf16_lines(path, encoding), 1):
                if line_no == 1 and line_text.startswith("\ufeff"):
                    line_text = line_text[1:]
                total = line_no
                lossy_total += lossy
                if forward:
                    if start_index < line_no <= start_index + limit and collecting:
                        selected_lines.append((line_no, line_text))
                        selected_units += _utf16_width(line_text) + len(str(line_no)) + 4
                        if max_chars is not None and selected_units > max_chars:
                            collecting = False
                else:
                    selected_lines.append((line_no, line_text))
                    if len(selected_lines) > abs(offset) + limit:
                        selected_lines.pop(0)
            if not forward:
                # Keep the last abs(offset) lines (then the requested count),
                # not the first lines of that rolling window.
                selected_lines = selected_lines[-abs(offset):][:limit]
                effective_offset = max(1, total + offset + 1)
            else:
                effective_offset = offset
            if lossy_total:
                warnings.append(f"{lossy_total} invalid UTF-16 sequence(s) replaced with U+FFFD")
            prepared = []
            for position, (line_no, line_text) in enumerate(selected_lines):
                body_text = line_text
                if position == 0 and column_offset:
                    if utf16_units:
                        cut = _utf16_index(body_text, column_offset)
                    else:
                        if column_offset > len(body_text):
                            fail("invalid_pagination", f"column_offset {column_offset} is past the end of line {line_no}")
                        cut = column_offset
                    body_text = body_text[cut:]
                more_text = _text_width(body_text, utf16_units) > max_line_chars
                if more_text:
                    if utf16_units:
                        body_text = body_text[:_utf16_index(body_text, max_line_chars)]
                    else:
                        body_text = body_text[:max_line_chars]
                prepared.append((line_no, body_text, more_text))
            if max_chars is not None:
                if forward:
                    budgeted, next_offset, next_column = _page_budget(
                        prepared, max_chars, utf16_units, column_offset
                    )
                else:
                    budgeted, next_offset, next_column = _tail_budget(
                        prepared, max_chars, utf16_units
                    ), None, None
            else:
                budgeted, next_offset, next_column = prepared, None, None
            line_start = budgeted[0][0] if budgeted else 0
            line_end = budgeted[-1][0] if budgeted else 0
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                fail("file_changed", "file changed during read; retry to obtain a consistent read")
            if payload.get("strict_offset") and forward and offset > total:
                fail("invalid_pagination", f"offset {offset} is past the end of the file ({total} lines)")
            partial = (
                line_start > 1
                or next_offset is not None
                or line_end < total
            )
            info = {
                "path": str(path), "resolved_path": str(resolved), "size": after.st_size,
                "mtime_ns": after.st_mtime_ns, "sha256": digest_hex,
                "total_lines": total, "offset": effective_offset, "limit": limit,
                "line_start": line_start, "line_end": line_end,
                "partial": partial,
                "content": _page_content(budgeted),
                "truncated_line_count": sum(1 for _i, _b, more in budgeted if more),
                "symlink": path.is_symlink(),
                "content_verified": verify, "encoding": encoding,
            }
            if max_chars is not None:
                info["page_lines"] = _page_metadata(budgeted, utf16_units, column_offset)
            if not forward:
                info["from_tail"] = True
            if next_offset is not None:
                info["next_offset"] = next_offset
                info["next_column_offset"] = next_column or 0
            if column_offset:
                info["column_offset"] = column_offset
            print(json.dumps({"status": "partial" if partial else "ok", "file": info, "warnings": warnings}, sort_keys=True))
            raise SystemExit(0)

        with path.open("rb") as binary_probe:
            head_bytes = binary_probe.read(8192)
        if kimi_encoding and _seems_binary_utf8(head_bytes[:512]):
            fail("binary_file", "remote.read detected a binary/non-UTF-8 file")
        if b"\x00" in head_bytes:
            fail("binary_file", "remote.read returns UTF-8/UTF-16 text; use remote.artifact_pull for binary/image files")

        if outline or (isinstance(node_paths, list) and node_paths):
            raw_bytes = path.read_bytes()
            if b"\x00" in raw_bytes[:8192]:
                fail("binary_file", "remote.read returns UTF-8 text; use remote.artifact_pull for binary/image files")
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                fail("file_changed", "file changed during read; retry to obtain a consistent read")
            digest_hex = sha256_bytes(raw_bytes) if verify else None
            text = raw_bytes.decode("utf-8", errors="replace")
            all_lines = text.splitlines()
            total = len(all_lines)
            symbols = extract_symbols(text, path.suffix.lower())
            warnings = []
            if not verify:
                warnings.append("Content hash not requested; this read does not update the edit concurrency guard.")
            if outline:
                item_offset = max(0, int(payload.get("item_offset") or 0))
                page = symbols[item_offset : item_offset + limit]
                numbered = "\n".join(
                    f"{item['start_line']}-{item['end_line']} | [{item['kind']}] {item['qualified_name']} — {item['signature']}"
                    for item in page
                )
                partial = item_offset > 0 or (item_offset + limit) < len(symbols)
                info = {
                    "path": str(path), "resolved_path": str(resolved), "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns, "sha256": digest_hex,
                    "total_lines": total, "offset": item_offset + 1 if symbols else 1, "limit": limit,
                    "line_start": page[0]["start_line"] if page else 0,
                    "line_end": page[-1]["end_line"] if page else 0,
                    "partial": partial, "content": numbered,
                    "truncated_line_count": 0, "symlink": path.is_symlink(),
                    "content_verified": verify, "outline": page, "total_items": len(symbols),
                }
                print(json.dumps({"status": "partial" if partial else "ok", "file": info, "warnings": warnings}, sort_keys=True))
                raise SystemExit(0)
            else:
                matched_items = []
                missing = []
                for req in node_paths:
                    req_norm = str(req).strip().replace("::", ".")
                    hits = [
                        s for s in symbols
                        if s["qualified_name"] == req_norm or s["name"] == req_norm or str(s["qualified_name"]).endswith("." + req_norm)
                    ]
                    if not hits:
                        missing.append(str(req))
                    else:
                        for h in hits:
                            if h not in matched_items:
                                matched_items.append(h)
                if not matched_items:
                    fail("symbol_not_found", f"Requested symbol(s) not found in {path}: {', '.join(missing)}", available_symbols=[s["qualified_name"] for s in symbols[:50]])
                if missing:
                    warnings.append(f"Symbol(s) not found: {', '.join(missing)}")
                sections = []
                for sym in matched_items:
                    s_idx = max(1, int(sym["start_line"]))
                    e_idx = min(total, int(sym["end_line"]))
                    body_lines = [
                        f"{ln} | {all_lines[ln - 1][:max_line_chars]}"
                        + ("<remote-dev line truncated>" if len(all_lines[ln - 1]) > max_line_chars else "")
                        for ln in range(s_idx, e_idx + 1)
                    ]
                    sections.append(f"# {sym['qualified_name']} ({sym['kind']}, lines {s_idx}-{e_idx})\n" + "\n".join(body_lines))
                numbered = "\n\n".join(sections)
                info = {
                    "path": str(path), "resolved_path": str(resolved), "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns, "sha256": digest_hex,
                    "total_lines": total, "offset": matched_items[0]["start_line"], "limit": limit,
                    "line_start": matched_items[0]["start_line"],
                    "line_end": matched_items[-1]["end_line"],
                    "partial": True, "content": numbered,
                    "truncated_line_count": 0, "symlink": path.is_symlink(),
                    "content_verified": verify, "code_items": matched_items, "missing_symbols": missing,
                }
                print(json.dumps({"status": "partial" if info["partial"] else "ok", "file": info, "warnings": warnings}, sort_keys=True))
                raise SystemExit(0)

        if content_offset > 0 and offset == 1 and start_line is None:
            raw_bytes = path.read_bytes()
            if b"\x00" in raw_bytes[:8192]:
                fail("binary_file", "remote.read returns UTF-8 text; use remote.artifact_pull for binary/image files")
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                fail("file_changed", "file changed during read; retry to obtain a consistent read")
            digest_hex = sha256_bytes(raw_bytes) if verify else None
            total = len(raw_bytes.decode("utf-8", errors="replace").splitlines())
            sliced_bytes = raw_bytes[content_offset:]
            base_line = raw_bytes[:content_offset].count(b"\n")
            sliced_lines = sliced_bytes.decode("utf-8", errors="replace").splitlines()
            selected = []
            for idx, line_text in enumerate(sliced_lines[:limit], base_line + 1):
                clipped = len(line_text) > max_line_chars
                selected.append((idx, line_text[:max_line_chars], clipped))
            truncated_lines = sum(clipped for _, _, clipped in selected)
            numbered = "\n".join(f"{index} | {body}" + ("<remote-dev line truncated>" if clipped else "")
                                 for index, body, clipped in selected)
            line_start = selected[0][0] if selected else 0
            line_end = selected[-1][0] if selected else 0
            partial = content_offset > 0 or len(sliced_lines) > limit
            info = {"path": str(path), "resolved_path": str(resolved), "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns, "sha256": digest_hex,
                    "total_lines": total, "offset": line_start or 1, "limit": limit,
                    "line_start": line_start, "line_end": line_end,
                    "content_offset": content_offset,
                    "partial": partial, "content": numbered,
                    "truncated_line_count": truncated_lines, "symlink": path.is_symlink(),
                    "content_verified": verify}
            warnings = []
            if truncated_lines:
                warnings.append(f"{truncated_lines} line(s) truncated to {max_line_chars} chars")
            if not verify:
                warnings.append("Content hash not requested; this read does not update the edit concurrency guard.")
            print(json.dumps({"status": "partial" if info["partial"] else "ok", "file": info, "warnings": warnings}, sort_keys=True))
            raise SystemExit(0)

        scan_limit = content_offset + max_line_chars if content_offset > 0 else max_line_chars
        digest_hex = _stream_sha256(path) if verify else None
        selected, total, more = [], 0, False
        selected_chars = 0
        collecting = True
        for index, line in enumerate(scan_text_lines(
            path, scan_limit, None,
            skip_line=offset if (offset > 0 and column_offset) else None,
            skip_units=column_offset,
            utf16_units=utf16_units,
        ), 1):
            total = index
            if offset > 0 and offset <= index < offset + limit and collecting:
                body, clipped = line
                if content_offset > 0:
                    body = body[content_offset : content_offset + max_line_chars]
                selected.append((index, body, clipped))
                selected_chars += len(body) + len(str(index)) + 4
                if max_chars is not None and selected_chars > max_chars:
                    collecting = False
            if not verify and offset > 0 and index >= offset + limit:
                more = True
                break
        total_lines = None if more else total
        tail_read = offset < 0
        if tail_read:
            offset = max(1, total + offset + 1)
            for index, line in enumerate(scan_text_lines(path, scan_limit), 1):
                if offset <= index < offset + limit:
                    body, clipped = line
                    if content_offset > 0:
                        body = body[content_offset : content_offset + max_line_chars]
                    selected.append((index, body, clipped))
                if index >= offset + limit:
                    break
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            fail("file_changed", "file changed during read; retry to obtain a consistent read")
        if payload.get("strict_offset") and offset > total:
            fail("invalid_pagination", f"offset {offset} is past the end of the file ({total} lines)")
        next_offset = None
        next_column_offset = None
        if max_chars is not None:
            prepared = []
            for index, body, clipped in selected:
                if utf16_units and _utf16_width(body) > max_line_chars:
                    body, _consumed = _take_text_units(body, max_line_chars, True)
                    clipped = True
                prepared.append((index, body, clipped))
            if tail_read:
                selected = _tail_budget(prepared, max_chars, utf16_units)
            else:
                selected, next_offset, next_column_offset = _page_budget(
                    prepared, max_chars, utf16_units, column_offset
                )
        truncated_lines = sum(clipped for _, _, clipped in selected)
        if truncated_lines and next_offset is None and selected and not tail_read:
            last_index, last_body, _last_clipped = selected[-1]
            next_offset = last_index
            next_column_offset = _text_width(last_body, utf16_units)
        if selected:
            line_start = selected[0][0]
            line_end = selected[-1][0]
        elif total:
            line_start = min(offset, total + 1)
            line_end = min(offset - 1 + limit, total)
        else:
            line_start = line_end = 0
        total_known = total_lines if total_lines is not None else total
        info = {"path": str(path), "resolved_path": str(resolved), "size": after.st_size,
                "mtime_ns": after.st_mtime_ns, "sha256": digest_hex,
                "total_lines": total_lines, "offset": offset, "limit": limit,
                "line_start": line_start, "line_end": line_end,
                "partial": line_start > 1 or more or line_end < total_known or content_offset > 0 or (max_chars is not None and next_offset is not None),
                "content": _page_content(selected),
                "truncated_line_count": truncated_lines, "symlink": path.is_symlink(),
                "content_verified": verify}
        if column_offset:
            info["column_offset"] = column_offset
        if tail_read:
            info["from_tail"] = True
        if max_chars is not None:
            info["page_lines"] = _page_metadata(selected, utf16_units, column_offset)
        if next_offset is not None:
            info["next_offset"] = next_offset
            info["next_column_offset"] = next_column_offset or 0
        if content_offset > 0:
            info["content_offset"] = content_offset
        warnings = []
        if truncated_lines:
            warnings.append(f"{truncated_lines} line(s) truncated to {max_line_chars} chars")
        if not verify:
            warnings.append("Content hash not requested; this read does not update the edit concurrency guard.")
        print(json.dumps({"status": "partial" if info["partial"] else "ok", "file": info, "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)

    if op == "ls":
        path, resolved = resolve_path(payload["path"], root, cwd)
        if not path.is_dir():
            fail("not_directory", f"RemoteLS lists directories, not files: {path}")
        limit = int(payload.get("limit") or 200)
        show_all = bool(payload.get("all", False))
        max_depth = int(payload.get("max_depth") or 1)
        ignore_globs = payload.get("ignore_globs") or []
        if isinstance(ignore_globs, str):
            ignore_globs = [ignore_globs]
        entries = []

        def should_ignore(rel_name, base_name):
            for pat in ignore_globs:
                if fnmatch.fnmatch(base_name, pat) or fnmatch.fnmatch(rel_name, pat):
                    return True
            return False

        def scan_dir(current_dir, depth, rel_prefix=""):
            for child in sorted(current_dir.iterdir(), key=lambda item: item.name):
                if not show_all and child.name.startswith("."):
                    continue
                rel_name = f"{rel_prefix}{child.name}" if rel_prefix else child.name
                if ignore_globs and should_ignore(rel_name, child.name):
                    continue
                try:
                    st = child.lstat()
                    if child.is_symlink():
                        kind = "symlink"
                    elif child.is_dir():
                        kind = "directory"
                    elif child.is_file():
                        kind = "file"
                    else:
                        kind = "other"
                    entry = {
                        "name": rel_name,
                        "path": str(child),
                        "type": kind,
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "is_symlink": child.is_symlink(),
                    }
                    entries.append(entry)
                    if kind == "directory" and not child.is_symlink() and depth < max_depth and len(entries) <= limit:
                        scan_dir(child, depth + 1, rel_name + "/")
                except TimeoutError:
                    raise
                except OSError as exc:
                    entries.append({"name": rel_name, "path": str(child), "type": "error", "error": str(exc)})

        scan_dir(path, 1)
        truncated = len(entries) > limit
        print(json.dumps({"status": "ok", "path": str(path), "resolved_path": str(resolved), "entries": entries[:limit], "truncated": truncated}, sort_keys=True))
        raise SystemExit(0)

    if op == "write":
        path, resolved = resolve_path(payload["file_path"], root, cwd, existing_parent_ok=True)
        content = payload.get("content", "")
        raw = content.encode("utf-8")
        create_dirs = bool(payload.get("create_dirs", False))
        overwrite = bool(payload.get("overwrite", False))
        append = bool(payload.get("append", False))
        if append and overwrite:
            fail("invalid_flags", "append and overwrite are mutually exclusive")
        existed = path.exists() or path.is_symlink()
        if existed and path.is_symlink():
            fail("symlink_not_allowed", f"refusing to overwrite symlink: {path}")
        if existed and not path.is_file():
            fail("not_file", f"refusing to overwrite non-file path: {path}")
        if existed and not overwrite and not append:
            fail("file_exists", f"remote file already exists: {path}")
        before = path.read_bytes() if existed else None
        before_sha = sha256_bytes(before) if before is not None else None
        expected_sha = payload.get("expected_sha256")
        if existed and expected_sha and before_sha != expected_sha:
            fail("file_changed_since_read", "remote file changed since last RemoteRead", before_sha256=before_sha, expected_sha256=expected_sha)
        if not path.parent.exists():
            if create_dirs:
                path.parent.mkdir(parents=True, exist_ok=True)
            else:
                fail("parent_not_found", f"parent directory does not exist: {path.parent}")
        if append and before is not None:
            raw = before + raw
        atomic_write(path, raw)
        after_info = file_info(path)
        before_text = before.decode("utf-8", errors="replace") if before is not None else ""
        after_text = raw.decode("utf-8", errors="replace")
        print(json.dumps({
            "status": "written",
            "file": after_info,
            "before_sha256": before_sha,
            "after_sha256": after_info["sha256"],
            "appended": append,
            "diff_preview": unified(before_text, after_text, path),
        }, sort_keys=True))
        raise SystemExit(0)

    if op in {"edit", "multi_edit"}:
        path, resolved = resolve_path(payload["file_path"], root, cwd)
        if path.is_symlink():
            fail("symlink_not_allowed", f"refusing to edit symlink: {path}")
        if not path.is_file():
            fail("not_file", f"remote path is not a file: {path}")
        before_raw = path.read_bytes()
        before_sha = sha256_bytes(before_raw)
        expected_sha = payload.get("expected_sha256")
        if expected_sha and before_sha != expected_sha:
            fail("file_changed_since_read", "remote file changed since last RemoteRead", before_sha256=before_sha, expected_sha256=expected_sha)
        pure_crlf = b"\r\n" in before_raw and before_raw.count(b"\n") == before_raw.count(b"\r\n")
        normalize_crlf = bool(payload.get("normalize_crlf")) and pure_crlf
        before = before_raw.decode("utf-8", errors="replace")
        if normalize_crlf:
            before = before.replace("\r\n", "\n")
        after = before
        edits = payload.get("edits") if op == "multi_edit" else [{
            "old_string": payload.get("old_string", ""),
            "new_string": payload.get("new_string", ""),
            "replace_all": bool(payload.get("replace_all", False)),
            "start_line": payload.get("start_line"),
            "end_line": payload.get("end_line"),
        }]
        if not isinstance(edits, list) or not edits:
            fail("invalid_edits", "edits must be a non-empty list")
        line_delta = 0
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                fail("invalid_edits", f"edit {index} must be an object")
            old = edit.get("old_string")
            if old is None:
                for k in ("TargetContent", "target_content", "old_str"):
                    if k in edit:
                        old = edit[k]
                        break
            old = "" if old is None else str(old)
            new = edit.get("new_string")
            if new is None:
                for k in ("ReplacementContent", "replacement_content", "new_str"):
                    if k in edit:
                        new = edit[k]
                        break
            new = "" if new is None else str(new)
            replace_all = bool(edit.get("replace_all", edit.get("AllowMultiple", edit.get("allow_multiple", False))))
            s_line = edit.get("start_line") if edit.get("start_line") is not None else edit.get("StartLine")
            e_line = edit.get("end_line") if edit.get("end_line") is not None else edit.get("EndLine")
            if old == "":
                fail("empty_old_string", f"edit {index} has an empty old_string")
            if s_line is not None or e_line is not None:
                lines = after.splitlines(keepends=True)
                s_idx = int(s_line) if s_line is not None else 1
                e_idx = int(e_line) if e_line is not None else len(lines)
                if s_idx < 1 or (e_line is not None and e_idx < s_idx):
                    fail("invalid_pagination", f"edit {index} has invalid line range {s_idx}..{e_idx}")
                slice_start = s_idx - 1
                slice_end = min(len(lines), e_idx)
                segment = "".join(lines[slice_start:slice_end])
                if segment.count(old) == 0 and line_delta != 0:
                    adj_start = max(0, slice_start + line_delta)
                    adj_end = min(len(lines), max(adj_start, e_idx + line_delta))
                    adj_segment = "".join(lines[adj_start:adj_end])
                    if adj_segment.count(old) > 0:
                        slice_start, slice_end, segment = adj_start, adj_end, adj_segment
                count = segment.count(old)
                if count == 0:
                    fail("old_string_not_found", f"edit {index} old_string was not found in lines {s_idx}-{e_idx}")
                if count > 1 and not replace_all:
                    fail("old_string_not_unique", f"edit {index} old_string matched {count} times in lines {s_idx}-{e_idx}")
                replaced = segment.replace(old, new, -1 if replace_all else 1)
                line_delta += replaced.count("\n") - segment.count("\n")
                after = "".join(lines[:slice_start]) + replaced + "".join(lines[slice_end:])
            else:
                count = after.count(old)
                if count == 0:
                    fail("old_string_not_found", f"edit {index} old_string was not found")
                if count > 1 and not replace_all:
                    fail("old_string_not_unique", f"edit {index} old_string matched {count} times")
                after = after.replace(old, new, -1 if replace_all else 1)
        if after == before:
            fail("no_change", "edit produced no file changes")
        materialized = after.replace("\n", "\r\n") if normalize_crlf else after
        atomic_write(path, materialized.encode("utf-8"))
        after_info = file_info(path)
        print(json.dumps({
            "status": "edited",
            "file": after_info,
            "before_sha256": before_sha,
            "after_sha256": after_info["sha256"],
            "diff_preview": unified(before, after, path),
        }, sort_keys=True))
        raise SystemExit(0)

    fail("unsupported_op", f"unsupported file op: {op}")


if __name__ in {"__main__", "__remote_dev_script__"}:
    main()
