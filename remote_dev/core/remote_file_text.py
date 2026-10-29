"""Remote file text decoding, bounded paging and symbol extraction.

[Role] Provide text-read helpers shared by the standalone file worker.
[Invariants] Paging preserves Unicode boundaries and streamed reads retain bounded prefixes.
[Timeouts & Locks] Reads run under the caller's remote command deadline; no locks or subprocesses.
[Side-Effect Policy] Read file content and emit protocol failures; never mutate files.
"""
from __future__ import annotations

import ast
import codecs
import hashlib
import json
import pathlib
import re


def fail(status: str, error: str | None = None, **extra: object) -> None:
    data: dict[str, object] = {"status": status}
    if error:
        data["error"] = error
    data.update(extra)
    print(json.dumps(data, sort_keys=True))
    raise SystemExit(0)


def _stream_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _detect_utf16(path):
    with path.open("rb") as stream:
        sample = stream.read(512)
    if len(sample) >= 2:
        if sample[0:2] == b"\xff\xfe":
            return "utf-16-le"
        if sample[0:2] == b"\xfe\xff":
            return "utf-16-be"
    zeros_at_odd = 0
    zeros_at_even = 0
    for index, byte in enumerate(sample[:512]):
        if byte != 0:
            continue
        if index % 2:
            zeros_at_odd += 1
        else:
            zeros_at_even += 1
    if zeros_at_odd == 0 and zeros_at_even == 0:
        return None
    if zeros_at_even == 0 and zeros_at_odd >= 2:
        return "utf-16-le"
    if zeros_at_odd == 0 and zeros_at_even >= 2:
        return "utf-16-be"
    # Mixed/insufficient NUL padding is binary by the native sniffTextEncoding.
    return "binary"


def _seems_binary_utf8(sample):
    if b"\x00" in sample:
        return True
    end = len(sample)
    for index in range(max(0, len(sample) - 3), len(sample)):
        byte = sample[index]
        expected = 2 if 194 <= byte <= 223 else 3 if 224 <= byte <= 239 else 4 if 240 <= byte <= 244 else 0
        if expected == 0 or index + expected <= len(sample):
            continue
        if all(128 <= cb <= 191 for cb in sample[index + 1 :]):
            end = index
            break
    try:
        text = sample[:end].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return True
    total = 0
    non_printable = 0
    for char in text:
        total += 1
        code = ord(char)
        if code in (9, 10, 13):
            continue
        if code < 32 or 127 <= code <= 159:
            non_printable += 1
    return total > 0 and non_printable / total > 0.3

def _utf16_lines(path, encoding):
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending = ""
    lossy = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                lossy += text.count("\ufffd")
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.endswith("\r"):
                        line = line[:-1]
                    yield line, lossy
                    lossy = 0
            if not chunk:
                break
    if pending:
        if pending.endswith("\r"):
            pending = pending[:-1]
        yield pending, lossy


def _utf16_width(text):
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _utf16_index(text, units, label="column_offset"):
    consumed = 0
    for index, ch in enumerate(text):
        if consumed == units:
            return index
        width = 2 if ord(ch) > 0xFFFF else 1
        if consumed + width > units:
            fail("invalid_pagination", f"{label} {units} falls inside a Unicode surrogate pair")
        consumed += width
    if consumed == units:
        return len(text)
    fail("invalid_pagination", f"{label} {units} is past the end of the line")


READ_LINE_MARKER = "<remote-dev line truncated>"


def _text_width(text, utf16_units):
    return _utf16_width(text) if utf16_units else len(text)


def _take_text_units(text, units, utf16_units):
    """Return (prefix, consumed) without splitting a surrogate pair."""
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


def _page_budget(selected, max_chars, utf16_units, column_offset):
    """Bound candidate lines and expose a cursor that follows the emitted body.

    ``selected`` is a list of ``(line_no, body, more_text)`` where ``body``
    already reflects ``column_offset`` and the per-line display cap.  The local
    formatter re-budgets against the final header/footer, so this function only
    needs to keep enough candidates and never advertise a cursor past the body
    it emits.
    """
    budgeted = []
    used = 0
    next_offset = None
    next_column = None
    first_line = selected[0][0] if selected else 0
    for index, body, more_text in selected:
        column_start = column_offset if index == first_line else 0
        prefix_units = len(str(index)) + 3
        body_units = _text_width(body, utf16_units)
        separator = 1 if budgeted else 0
        if used + separator + prefix_units + body_units > max_chars:
            if budgeted:
                next_offset = index
                next_column = column_start
            else:
                room = max_chars - prefix_units - 1
                if room <= 0:
                    fail(
                        "max_chars_too_small",
                        f"max_chars={max_chars} is too small for file text and the Read status",
                    )
                part, consumed = _take_text_units(body, room, utf16_units)
                if consumed <= 0:
                    fail(
                        "max_chars_too_small",
                        f"max_chars={max_chars} is too small for file text and the Read status",
                    )
                budgeted.append((index, part, True))
                next_offset = index
                next_column = column_start + consumed
            break
        budgeted.append((index, body, more_text))
        used += separator + prefix_units + body_units
    else:
        if budgeted and budgeted[-1][2]:
            index, body, _more = budgeted[-1]
            column_start = column_offset if index == first_line else 0
            next_offset = index
            next_column = column_start + _text_width(body, utf16_units)
    return budgeted, next_offset, next_column


def _page_content(budgeted):
    return "\n".join(
        f"{index} | {body}" + (READ_LINE_MARKER if more_text else "")
        for index, body, more_text in budgeted
    )


def _page_metadata(budgeted, utf16_units, column_offset):
    first_line = budgeted[0][0] if budgeted else 0
    return [
        {
            "line": index,
            "column_start": column_offset if index == first_line else 0,
            "body_units": _text_width(body, utf16_units),
            "clipped": bool(more_text),
        }
        for index, body, more_text in budgeted
    ]


def _tail_budget(selected, max_chars, utf16_units):
    """Keep the trailing whole lines that fit; never cut the end of a tail read."""
    kept = []
    used = 0
    for entry in reversed(selected):
        index, body, more_text = entry
        width = len(str(index)) + 3 + _text_width(body, utf16_units) + 1
        if kept and used + width > max_chars:
            break
        kept.append(entry)
        used += width
    kept.reverse()
    return kept


def scan_text_lines(path: pathlib.Path, max_chars: int, digest=None, *,
                    skip_line=None, skip_units=0, utf16_units=False):
    # Split like str.splitlines(), including CRLF across chunk boundaries.
    # Keep only a bounded prefix even if a file contains one enormous line.
    # ``skip_line``/``skip_units`` discard a leading column without buffering
    # it, so continuing a very long line stays bounded by ``max_chars``.
    breaks = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    prefix, length, skip_lf, first = "", 0, False, True
    line_no = 1
    remaining_skip = skip_units if skip_line == 1 else 0

    def consume(piece):
        nonlocal prefix, length, remaining_skip
        if remaining_skip and line_no == skip_line:
            if utf16_units:
                index = 0
                while index < len(piece) and remaining_skip:
                    width = 2 if ord(piece[index]) > 0xFFFF else 1
                    if width > remaining_skip:
                        fail(
                            "invalid_pagination",
                            f"column_offset {skip_units} falls inside a Unicode surrogate pair",
                        )
                    remaining_skip -= width
                    index += 1
                piece = piece[index:]
            else:
                drop = min(len(piece), remaining_skip)
                remaining_skip -= drop
                piece = piece[drop:]
        prefix += piece[:max(0, max_chars - len(prefix))]
        length += len(piece)

    def finish():
        nonlocal prefix, length, line_no, remaining_skip
        if line_no == skip_line and remaining_skip:
            fail(
                "invalid_pagination",
                f"column_offset {skip_units} is past the end of line {line_no}",
            )
        result = (prefix, length > max_chars)
        prefix, length = "", 0
        line_no += 1
        remaining_skip = skip_units if skip_line == line_no else 0
        return result

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
                consume(text[start:match.start()])
                yield finish()
                skip_lf = match.group() == "\r" and match.end() == len(text)
                start = match.end()
            consume(text[start:])
            if not raw:
                if length:
                    yield finish()
                return


def extract_symbols(text: str, suffix: str) -> list[dict[str, object]]:
    lines = text.splitlines()
    symbols: list[dict[str, object]] = []
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
    class_stack: list[tuple[int, str]] = []
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
        prefix = [item[1] for item in class_stack]
        if "::" in name:
            qual = name.replace("::", ".")
            short_name = name.split("::")[-1]
        else:
            short_name = name
            qual = ".".join([*prefix, short_name]) if prefix else short_name
        kind = (
            "class"
            if kind_raw in {"class", "struct", "interface", "enum", "trait", "impl"}
            else ("method" if prefix or "::" in name else "function")
        )
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


