"""
[Role] Bounded text preview, consecutive repeated-line folding, and head/tail truncation helpers (`preview.py`).
[Invariants]
- Enforces `MAX_TEXT_CHARS`, `MAX_READ_LINES`, `MAX_JOB_TAIL_LINES`, `MAX_GREP_MATCHES`, and `MAX_LINE_CHARS` caps on tool responses.
- Folds runs of `> 2` identical consecutive log lines into `[previous line repeated N more times]` before head/tail slicing so verbose compiler/progress spam cannot evict diagnostic head/tail context, while full raw output remains intact in on-disk job logs.
[Timeouts & Locks]
- Pure in-memory string slicing; zero subprocesses, zero locks, and zero I/O.
[Side-Effect Policy]
- Pure functions; mutates no local or remote state.
"""
from __future__ import annotations

MAX_TEXT_CHARS = 12000
MAX_READ_LINES = 20000
MAX_JOB_TAIL_LINES = 500
MAX_GREP_MATCHES = 20000
MAX_LINE_CHARS = 2000

DEFAULT_HEAD_CHARS = 4000
DEFAULT_TAIL_CHARS = 4000


def _tail_slice(value: str, count: int) -> str:
    """Return a true suffix of ``count`` characters.

    ``value[-0:]`` is the whole string; callers that ask for no tail
    must get ``""``.
    """
    if count <= 0:
        return ""
    return value[-count:]


def fold_repeated_lines(value: str) -> str:
    """Collapse runs of >2 identical consecutive non-blank lines when folding strictly shrinks text."""
    if "\n" not in value:
        return value
    trailing_newline = value.endswith("\n")
    lines = value.splitlines()
    deduped: list[str] = []
    index = 0
    total = len(lines)
    any_folded = False
    while index < total:
        next_index = index + 1
        while next_index < total and lines[next_index] == lines[index]:
            next_index += 1
        repeats = next_index - index
        line = lines[index]
        marker = f"[previous line repeated {repeats - 1} more times]"
        if repeats > 2 and line.strip() and (repeats - 1) * (len(line) + 1) > len(marker) + 1:
            deduped.append(line)
            deduped.append(marker)
            any_folded = True
        else:
            deduped.extend(lines[index:next_index])
        index = next_index
    if not any_folded:
        return value
    folded = "\n".join(deduped)
    if trailing_newline and folded:
        folded += "\n"
    return folded


def text_preview(
    value: str,
    *,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
    fold_repeats: bool = True,
) -> dict[str, object]:
    byte_count = len(value.encode("utf-8", errors="replace"))
    compacted = fold_repeated_lines(value) if fold_repeats else value
    repeated_lines_folded = compacted != value
    if len(compacted) <= head_chars + tail_chars:
        return {
            "text": compacted,
            "bytes": byte_count,
            "truncated": False,
            "repeated_lines_folded": repeated_lines_folded,
            "head_chars": head_chars,
            "tail_chars": tail_chars,
        }
    return {
        "head": compacted[:head_chars],
        "tail": _tail_slice(compacted, tail_chars),
        "bytes": byte_count,
        "truncated": True,
        "repeated_lines_folded": repeated_lines_folded,
        "head_chars": head_chars,
        "tail_chars": tail_chars,
    }


def stdout_stderr_preview(stdout: str, stderr: str) -> dict[str, object]:
    out_preview = text_preview(stdout)
    err_preview = text_preview(stderr)
    return {
        "stdout": out_preview,
        "stderr": err_preview,
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
        "truncated": bool(out_preview["truncated"] or err_preview["truncated"]),
        "head_chars": DEFAULT_HEAD_CHARS,
        "tail_chars": DEFAULT_TAIL_CHARS,
    }


def tail_text(value: str, limit: int = DEFAULT_TAIL_CHARS) -> str:
    if limit <= 0:
        return ""
    return value if len(value) <= limit else value[-limit:]


def compact_text(value: str, *, limit: int = MAX_TEXT_CHARS, fold_repeats: bool = True) -> str:
    compacted = fold_repeated_lines(value) if fold_repeats else value
    if len(compacted) <= limit:
        return compacted
    marker = "\n<remote-dev text truncated; full output is available via refs/resources>\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return compacted[:head] + marker + _tail_slice(compacted, tail)
