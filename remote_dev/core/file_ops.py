from __future__ import annotations

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

REMOTE_FILE_PY = r'''
import difflib
import hashlib
import json
import os
import pathlib
import sys
import tempfile

payload = json.loads(sys.stdin.read())
op = payload["op"]
root = pathlib.Path(payload["root"]).resolve()
cwd = pathlib.Path(payload.get("cwd") or payload["root"])

def fail(status, error=None, **extra):
    data = {"status": status}
    if error:
        data["error"] = error
    data.update(extra)
    print(json.dumps(data, sort_keys=True))
    raise SystemExit(0)

def resolve_path(raw, *, existing_parent_ok=False):
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

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def file_info(path, *, content=None):
    st = path.stat()
    data = path.read_bytes() if content is None else content
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "sha256": sha256_bytes(data),
        "size": len(data),
        "mtime_ns": st.st_mtime_ns,
    }

def atomic_write(path, data):
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

def unified(before, after, path):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
        n=3,
    ))[:12000]

def scan_text_lines(path, max_chars, digest=None):
    # Split like str.splitlines(), including CRLF across chunk boundaries.
    # Keep only a bounded prefix even if a file contains one enormous line.
    import codecs
    import re
    breaks = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    prefix, length, skip_lf, first = "", 0, False, True
    with path.open("rb") as stream:
        while True:
            raw = stream.read(65536)
            if first:
                first = False
                if b"\x00" in raw[:8192]:
                    fail("binary_file", "remote.read returns UTF-8 text; use remote.artifact_pull for binary/image files")
            if digest is not None:
                digest.update(raw)
            text = decoder.decode(raw, final=not raw)
            if skip_lf and text:
                if text.startswith("\n"):
                    text = text[1:]
                skip_lf = False
            start = 0
            for match in breaks.finditer(text):
                piece = text[start:match.start()]
                prefix += piece[:max(0, max_chars - len(prefix))]
                length += len(piece)
                yield prefix, length > max_chars
                prefix, length = "", 0
                skip_lf = match.group() == "\r" and match.end() == len(text)
                start = match.end()
            piece = text[start:]
            prefix += piece[:max(0, max_chars - len(prefix))]
            length += len(piece)
            if not raw:
                if length:
                    yield prefix, length > max_chars
                return


def extract_symbols(text, suffix):
    import ast
    import re
    lines = text.splitlines()
    symbols = []
    if suffix in {".py", ".pyi", ""}:
        try:
            tree = ast.parse(text)
            def visit_nodes(nodes, prefix):
                for node in nodes:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        qual = ".".join([*prefix, node.name])
                        kind = "class" if isinstance(node, ast.ClassDef) else ("method" if prefix else "function")
                        dec_lines = [getattr(d, "lineno", node.lineno) for d in getattr(node, "decorator_list", [])]
                        start_ln = min([node.lineno, *dec_lines]) if dec_lines else node.lineno
                        end_ln = getattr(node, "end_lineno", None) or node.lineno
                        sig = lines[node.lineno - 1].strip() if 1 <= node.lineno <= len(lines) else node.name
                        symbols.append({
                            "name": node.name,
                            "qualified_name": qual,
                            "kind": kind,
                            "start_line": int(start_ln),
                            "end_line": int(end_ln),
                            "signature": sig[:240],
                        })
                        if isinstance(node, ast.ClassDef):
                            visit_nodes(node.body, [*prefix, node.name])
            visit_nodes(tree.body, [])
            return symbols
        except SyntaxError:
            pass
    pattern = re.compile(
        r"^(?P<indent>\s*)(?:(?:pub(?:\([^)]*\))?\s+|export\s+|default\s+|async\s+|static\s+|inline\s+|virtual\s+|__global__\s+|__device__\s+|__host__\s+|__aicore__\s+)*)"
        r"(?:(?P<kind>class|struct|interface|enum|trait|impl|def|func|fn|function)\s+(?P<name>[A-Za-z_~][A-Za-z0-9_:]*)"
        r"|(?P<ret>[A-Za-z_][A-Za-z0-9_:<>,\s\*&]+?)\s+(?P<cname>[A-Za-z_~][A-Za-z0-9_:]*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{?\s*$)"
    )
    class_stack = []
    for idx, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*")):
            continue
        m = pattern.match(raw_line)
        if not m:
            continue
        indent = len(m.group("indent") or "")
        kind_raw = m.group("kind") or "function"
        name = m.group("name") or m.group("cname") or ""
        if name in {"if", "for", "while", "switch", "catch", "return", "sizeof", "else"}:
            continue
        while class_stack and class_stack[-1][0] >= indent:
            class_stack.pop()
        prefix = [ item[1] for item in class_stack ]
        if "::" in name:
            qual = name.replace("::", ".")
            short_name = name.split("::")[-1]
        else:
            short_name = name
            qual = ".".join([*prefix, short_name]) if prefix else short_name
        kind = "class" if kind_raw in {"class", "struct", "interface", "enum", "trait", "impl"} else ("method" if prefix or "::" in name else "function")
        end_ln = idx
        if "{" in raw_line or (idx < len(lines) and lines[idx].strip().startswith("{")):
            depth = 0
            started_brace = False
            for scan_idx in range(idx - 1, min(len(lines), idx + 2000)):
                for ch in lines[scan_idx]:
                    if ch == "{":
                        depth += 1
                        started_brace = True
                    elif ch == "}":
                        depth -= 1
                if started_brace and depth <= 0:
                    end_ln = scan_idx + 1
                    break
        else:
            for scan_idx in range(idx, min(len(lines), idx + 2000)):
                s_line = lines[scan_idx]
                if not s_line.strip() or s_line.strip().startswith("#"):
                    continue
                cur_indent = len(s_line) - len(s_line.lstrip())
                if cur_indent <= indent:
                    break
                end_ln = scan_idx + 1
        if kind == "class":
            class_stack.append((indent, short_name))
        symbols.append({
            "name": short_name,
            "qualified_name": qual,
            "kind": kind,
            "start_line": idx,
            "end_line": end_ln,
            "signature": stripped[:240],
        })
    return symbols


if op == "read":
    path, resolved = resolve_path(payload["file_path"])
    if path.is_dir():
        fail("is_directory", f"RemoteRead reads files, not directories: {path}")
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
    verify = bool(payload.get("verify_content", True))
    outline = bool(payload.get("outline", False))
    node_paths = payload.get("node_paths")
    before = path.stat()

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
                    if s["qualified_name"] == req_norm or s["name"] == req_norm or s["qualified_name"].endswith("." + req_norm)
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
    digest = hashlib.sha256() if verify else None
    selected, total, more = [], 0, False
    for index, line in enumerate(scan_text_lines(path, scan_limit, digest), 1):
        total = index
        if offset > 0 and offset <= index < offset + limit:
            body, clipped = line
            if content_offset > 0:
                body = body[content_offset : content_offset + max_line_chars]
            selected.append((index, body, clipped))
        if not verify and offset > 0 and index >= offset + limit:
            more = True
            break
    total_lines = None if more else total
    if offset < 0:
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
    start = min(offset - 1, total)
    end = min(start + limit, total)
    truncated_lines = sum(clipped for _, _, clipped in selected)
    numbered = "\n".join(f"{index} | {body}" + ("<remote-dev line truncated>" if clipped else "")
                         for index, body, clipped in selected)
    info = {"path": str(path), "resolved_path": str(resolved), "size": after.st_size,
            "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest() if digest else None,
            "total_lines": total_lines, "offset": offset, "limit": limit,
            "line_start": start + 1 if total else 0, "line_end": end,
            "partial": start > 0 or more or end < total or content_offset > 0, "content": numbered,
            "truncated_line_count": truncated_lines, "symlink": path.is_symlink(),
            "content_verified": verify}
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
    import fnmatch
    path, resolved = resolve_path(payload["path"])
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
            except OSError as exc:
                entries.append({"name": rel_name, "path": str(child), "type": "error", "error": str(exc)})

    scan_dir(path, 1)
    truncated = len(entries) > limit
    print(json.dumps({"status": "ok", "path": str(path), "resolved_path": str(resolved), "entries": entries[:limit], "truncated": truncated}, sort_keys=True))
    raise SystemExit(0)

if op == "write":
    path, resolved = resolve_path(payload["file_path"], existing_parent_ok=True)
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
    path, resolved = resolve_path(payload["file_path"])
    if path.is_symlink():
        fail("symlink_not_allowed", f"refusing to edit symlink: {path}")
    if not path.is_file():
        fail("not_file", f"remote path is not a file: {path}")
    before_raw = path.read_bytes()
    before_sha = sha256_bytes(before_raw)
    expected_sha = payload.get("expected_sha256")
    if expected_sha and before_sha != expected_sha:
        fail("file_changed_since_read", "remote file changed since last RemoteRead", before_sha256=before_sha, expected_sha256=expected_sha)
    before = before_raw.decode("utf-8", errors="replace")
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
    atomic_write(path, after.encode("utf-8"))
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
'''


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
        "max_line_chars": MAX_LINE_CHARS,
    }
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
    text = _format_read_text(endpoint, result, file_info)
    payload = {"text": text, "result": result}
    if recording_error:
        from remote_dev.result import recording_failure
        return recording_failure(payload, recording_error, stage="read_ledger", operation_completed=True)
    return payload


@observed_tool("remote.ls")
@pinned_endpoint
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


def _format_read_text(endpoint: Endpoint, result: dict[str, Any], file_info: dict[str, Any]) -> str:
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
    return compact_text("\n".join(lines).rstrip() + "\n")


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
