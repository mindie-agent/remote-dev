from __future__ import annotations

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

REMOTE_SEARCH_PY = r'''
import fnmatch
import glob as glob_mod
import json
import os
import pathlib
import shutil
import subprocess
import sys

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

def resolve_path(raw):
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        resolved = p.resolve()
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    return p, resolved

def git_root(start):
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None

def parse_gitignore_file(path):
    rules = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return rules
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        rules.append((line.replace("\\", "/"), negated, dir_only))
    return rules

def collect_gitignore_rules(base):
    root = git_root(base) or base.resolve()
    rules = []
    seen = set()
    candidates = [root / ".gitignore"]
    candidates.extend(sorted(root.rglob(".gitignore")))
    for gi in candidates:
        resolved = gi.resolve()
        if resolved in seen or not gi.is_file():
            continue
        seen.add(resolved)
        try:
            rel_dir = "" if gi.parent.resolve() == root else str(gi.parent.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        for pattern, negated, dir_only in parse_gitignore_file(gi):
            rules.append((rel_dir, pattern, negated, dir_only))
    return root, rules

def gitignore_fnmatch(pattern, path):
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if "**" in pattern:
        regex_parts = []
        i = 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                regex_parts.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                regex_parts.append(".*")
                i += 2
                continue
            ch = pattern[i]
            if ch in ".^$+{}[]|()\\":
                regex_parts.append("\\" + ch)
            elif ch == "*":
                regex_parts.append("[^/]*")
            elif ch == "?":
                regex_parts.append("[^/]")
            else:
                regex_parts.append(ch)
            i += 1
        import re
        return re.fullmatch("".join(regex_parts), path) is not None
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.split("/")[-1], pattern)

def gitignore_rule_hits(anchor, pattern, path, is_dir, dir_only):
    path = path.replace("\\", "/")
    if anchor:
        prefix = anchor + "/"
        if path != anchor and not path.startswith(prefix):
            return False
        candidate = "" if path == anchor else path[len(prefix):]
        if not candidate:
            return False
    else:
        candidate = path

    def matches(rel, rel_is_dir):
        if dir_only and not rel_is_dir:
            return False
        if "/" not in pattern.strip("/"):
            return any(gitignore_fnmatch(pattern, part) for part in rel.split("/")) or gitignore_fnmatch(pattern, rel)
        return gitignore_fnmatch(pattern, rel)

    if matches(candidate, is_dir):
        return True
    if dir_only:
        parts = candidate.split("/")
        for index in range(1, len(parts)):
            if matches("/".join(parts[:index]), True):
                return True
    return False

def is_gitignored(rel_from_root, is_dir, rules):
    ignored = False
    path = rel_from_root.replace("\\", "/")
    for anchor, pattern, negated, dir_only in rules:
        if gitignore_rule_hits(anchor, pattern, path, is_dir, dir_only):
            ignored = not negated
    return ignored

def apply_gitignore(base, matches):
    warnings = []
    git = shutil.which("git")
    if git:
        probe = subprocess.run(
            [git, "-C", str(base), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            rels = [item["relpath"] for item in matches]
            chk = subprocess.run(
                [git, "-C", str(base), "check-ignore", "-z", "--stdin"],
                input="".join(rel + "\0" for rel in rels).encode("utf-8"),
                capture_output=True,
            )
            if chk.returncode not in (0, 1):
                fail("failed", chk.stderr.decode("utf-8", "replace"), exit_code=chk.returncode)
            ignored = {part.decode("utf-8", "replace") for part in chk.stdout.split(b"\0") if part}
            return [item for item in matches if item["relpath"] not in ignored], warnings
        elif git_root(base) is not None:
            fail("failed", probe.stderr, exit_code=probe.returncode)
    root, rules = collect_gitignore_rules(base)
    if git is None:
        warnings.append("git is unavailable; .gitignore matching used the documented in-process rules.")
    if not rules:
        return matches, warnings
    try:
        base_from_root = "" if base.resolve() == root else str(base.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return matches, warnings
    kept = []
    for item in matches:
        rel = item["relpath"].replace("\\", "/")
        rel_from_root = rel if not base_from_root else (base_from_root + "/" + rel)
        if not is_gitignored(rel_from_root, item.get("type") == "directory", rules):
            kept.append(item)
    return kept, warnings

def command_lines(cmd, max_chars=32768):
    """Yield bounded line prefixes; close the process when the caller stops."""
    import contextlib
    import threading
    tail = bytearray()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=False)
    def drain():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            tail.extend(chunk)
            del tail[:-4000]
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    import io
    output = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
    try:
        prefix, length = "", 0
        while True:
            fragment = output.readline(65536)
            if not fragment:
                if length:
                    yield prefix, length > max_chars
                break
            ended = fragment.endswith("\n")
            piece = fragment[:-1] if ended else fragment
            prefix += piece[:max(0, max_chars-len(prefix))]
            length += len(piece)
            if ended:
                yield prefix, length > max_chars
                prefix, length = "", 0
        code = proc.wait()
        reader.join()
        if code not in (0, 1):
            fail("failed", tail.decode("utf-8", "replace"))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        reader.join()
        output.close()
        proc.stderr.close()


def collect_page(cmd, output_mode, offset, limit, max_line_chars, *, skip_zeros=False):
    import contextlib
    matches, count, truncated_lines = [], 0, 0
    total_matches = 0 if output_mode == "count_matches" else None
    cap = max_line_chars if output_mode == "content" else 32768
    with contextlib.closing(command_lines(cmd, cap)) as rows:
        for index, (line, clipped) in enumerate(rows):
            if index < offset or (skip_zeros and line.endswith(":0")):
                continue
            count += 1
            if total_matches is not None and line.rsplit(":", 1)[-1].isdigit():
                total_matches += int(line.rsplit(":", 1)[-1])
            if len(matches) < limit:
                if clipped:
                    line += "<remote-dev line truncated>"
                    truncated_lines += 1
                matches.append(line)
            elif total_matches is None:
                break
    return matches, count > limit, total_matches, truncated_lines


if op == "glob":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.is_dir():
        fail("not_directory", f"RemoteGlob path is not a directory: {base}")
    pattern = payload.get("pattern") or "*"
    limit = int(payload.get("limit") or 100)
    respect_gitignore = bool(payload.get("respect_gitignore"))
    matches = []
    warnings = []
    import heapq
    seen = 0
    # One-shot helper: chdir so glob(pattern, recursive=True) works on Python 3.9 (no root_dir).
    os.chdir(str(base))
    for item in glob_mod.iglob(pattern, recursive=True):
        path = base / item
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        row = {"path": str(path), "relpath": item, "type": "directory" if path.is_dir() else "file", "mtime_ns": st.st_mtime_ns, "size": st.st_size}
        seen += 1
        if respect_gitignore:
            matches.append(row)
        else:
            heapq.heappush(matches, (st.st_mtime_ns, -seen, row))
            if len(matches) > limit + 1:
                heapq.heappop(matches)
    if respect_gitignore:
        matches, gi_warnings = apply_gitignore(base, matches)
        warnings.extend(gi_warnings)
    if respect_gitignore:
        matches.sort(key=lambda row: row["mtime_ns"], reverse=True)
    else:
        matches = [row for _, _, row in sorted(matches, reverse=True)]
    print(json.dumps({"status": "ok", "matches": matches[:limit], "truncated": len(matches) > limit, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

if op == "grep":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.exists():
        fail("not_found", f"RemoteGrep path does not exist: {base}")
    pattern = payload.get("pattern")
    if not pattern:
        fail("pattern_required", "RemoteGrep requires pattern")
    limit = int(payload.get("limit") or 100)
    offset = int(payload.get("offset") or 0)
    if offset < 0:
        fail("invalid_pagination", "offset must be >= 0")
    max_line_chars = int(payload.get("max_line_chars") or 2000)
    output_mode = payload.get("output_mode") or "files_with_matches"
    glob_pattern = payload.get("glob")
    type_name = payload.get("type")
    multiline = bool(payload.get("multiline", False))
    case_insensitive = bool(payload.get("case_insensitive", False))
    before_context = int(payload.get("before_context") or 0)
    after_context = int(payload.get("after_context") or 0)
    context_lines = int(payload.get("context_lines") or 0)
    include_ignored = bool(payload.get("include_ignored", False))
    line_numbers = payload.get("line_numbers")
    line_numbers = True if line_numbers is None else bool(line_numbers)
    if output_mode != "content" and (before_context or after_context or context_lines):
        warnings_context = "context line options only apply to output_mode=content; ignoring them"
    else:
        warnings_context = ""
    warnings = []
    if warnings_context:
        warnings.append(warnings_context)
    rg_path = shutil.which("rg")
    if rg_path:
        cmd = [rg_path, "--color", "never"]
        if multiline:
            cmd.append("-U")
        if case_insensitive:
            cmd.append("-i")
        if include_ignored:
            cmd.extend(["--no-ignore", "--hidden"])
        if glob_pattern:
            cmd.extend(["--glob", glob_pattern])
        if type_name:
            cmd.extend(["--type", type_name])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")
        elif output_mode == "count_matches":
            # Kimi native Grep habit: per-file total match counts, which
            # differ from -c (matching *lines*) whenever one line holds
            # several matches.
            cmd.extend(["--count-matches", "--with-filename"])
        else:
            cmd.append("-n" if line_numbers else "--no-line-number")
            if context_lines:
                cmd.extend(["-C", str(context_lines)])
            if before_context:
                cmd.extend(["-B", str(before_context)])
            if after_context:
                cmd.extend(["-A", str(after_context)])
        cmd.extend([pattern, str(base)])
        lines, truncated, total_matches, clipped = collect_page(cmd, output_mode, offset, limit, max_line_chars)
        if clipped:
            warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
        print(json.dumps({"status": "ok", "engine": "rg", "output_mode": output_mode, "offset": offset,
                          "matches": lines, "total_matches": total_matches, "truncated": truncated,
                          "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)

    # rg is unavailable: fall back to POSIX `grep -E`, which preserves regex
    # semantics. Never silently degrade to substring matching, and fail fast on
    # features grep cannot honor instead of returning semantically wrong "ok".
    if multiline:
        fail("rg_required", "multiline grep requires ripgrep (rg) on the remote host; install rg or drop multiline")
    if type_name:
        fail("rg_required", f"grep fallback cannot honor --type {type_name}; install ripgrep (rg) or use --glob")
    if glob_pattern and "/" in glob_pattern:
        # grep --include matches file basenames only; a path-anchored glob
        # like "src/**/*.py" cannot be honored faithfully.
        fail("rg_required", f"grep fallback cannot honor path-anchored --glob {glob_pattern}; install ripgrep (rg) or use a basename glob")
    grep_path = shutil.which("grep")
    if not grep_path:
        fail("grep_unavailable", "neither rg nor grep found on the remote host")
    warnings.append("rg not found; used grep -E fallback (POSIX ERE semantics)")
    cmd = [grep_path, "-r", "-E", "-I"]
    if case_insensitive:
        cmd.append("-i")
    # Align with rg defaults, which skip .git and hidden directories while
    # descending. grep applies --exclude-dir to the base operand itself, so
    # skip a pattern that matches the explicitly requested base (rg searches
    # an explicitly named hidden or .git path). grep cannot evaluate
    # .gitignore rules at all: include_ignored only lifts these default
    # directory excludes, which is an approximation and is reported as such.
    if include_ignored:
        warnings.append("grep fallback cannot evaluate .gitignore; include_ignored only re-enables hidden and .git directories")
    else:
        for exclude_dir in (".git", ".*"):
            if fnmatch.fnmatch(base.name, exclude_dir):
                continue
            cmd.append(f"--exclude-dir={exclude_dir}")
    if glob_pattern:
        cmd.append(f"--include={glob_pattern}")
    if output_mode == "count_matches":
        # POSIX grep has no per-file match count. Find candidate files first,
        # then count -o matches per file. Never degrade to line counts: with
        # several matches on one line that would silently change semantics.
        import contextlib
        counts, total_matches, count = [], 0, 0
        count_cmd = [grep_path, "-o", "-E"]
        if case_insensitive:
            count_cmd.append("-i")
        with contextlib.closing(command_lines([*cmd, "-l", "--", pattern, str(base)])) as candidates:
            for index, (candidate, _) in enumerate(candidates):
                amount = sum(1 for _ in command_lines([*count_cmd, "--", pattern, candidate]))
                total_matches += amount
                if index >= offset:
                    count += 1
                    if len(counts) < limit:
                        counts.append(f"{candidate}:{amount}")
        print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "offset": offset,
                          "matches": counts, "total_matches": total_matches, "truncated": count > limit,
                          "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        if line_numbers:
            cmd.append("-n")
        if context_lines:
            cmd.append(f"-C{context_lines}")
        if before_context:
            cmd.append(f"-B{before_context}")
        if after_context:
            cmd.append(f"-A{after_context}")
    cmd.extend(["--", pattern, str(base)])
    lines, truncated, _, clipped = collect_page(cmd, output_mode, offset, limit, max_line_chars,
                                                skip_zeros=output_mode == "count")
    if clipped:
        warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
    print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "offset": offset,
                      "matches": lines, "total_matches": None, "truncated": truncated, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

fail("unsupported_op", f"unsupported search op: {op}")
'''


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _compact_matches(matches: list[Any]) -> tuple[list[str], bool]:
    visible: list[str] = []
    total = 0
    truncated = False
    for item in matches:
        text = str(item)
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + "<remote-dev line truncated>"
            truncated = True
        if total + len(text) > MAX_TEXT_CHARS:
            truncated = True
            break
        visible.append(text)
        total += len(text)
    return visible, truncated or len(visible) < len(matches)


@observed_tool("remote.glob")
@pinned_endpoint
def remote_glob(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    limit: int = 100,
    respect_gitignore: bool = False,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    raw_path = path or endpoint.effective_cwd
    try:
        base = join_under_root(endpoint.root, endpoint.effective_cwd, raw_path)
    except PathPolicyError as exc:
        return _path_blocked_result(endpoint, "remote.glob", raw_path, str(exc), started, start)
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
            "op": "glob",
            "root": endpoint.root,
            "cwd": endpoint.effective_cwd,
            "path": base,
            "pattern": pattern,
            "limit": limit,
            "respect_gitignore": respect_gitignore,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = data.get("warnings", []) if isinstance(data.get("warnings"), list) else []
    visible_matches, text_truncated = _compact_matches([str(item.get("path", item)) for item in matches])
    result = make_result(
        tool="remote.glob",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else "failed",
        status=status,
        summary=f"RemoteGlob found {len(matches)} paths.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated},
        warnings=warnings,
        extra={"matches": visible_matches, "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error"), "error_details": data.get("error_details")},
    )
    text = compact_text("\n".join(visible_matches) + ("\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"))
    return {"text": text, "result": result}


@observed_tool("remote.grep")
@pinned_endpoint
def remote_grep(
    endpoint: Endpoint,
    *,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: str = "files_with_matches",
    multiline: bool = False,
    case_insensitive: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    context_lines: int = 0,
    line_numbers: bool | None = None,
    include_ignored: bool = False,
    offset: int = 0,
    limit: int = 100,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    if output_mode not in {"files_with_matches", "content", "count", "count_matches"}:
        local_warnings = [f"unknown output_mode {output_mode!r}; using files_with_matches"]
        output_mode = "files_with_matches"
    else:
        local_warnings = []
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
    data = run_remote_python(
        endpoint,
        REMOTE_SEARCH_PY,
        {
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
            "offset": offset,
            "limit": limit,
            "max_line_chars": MAX_LINE_CHARS,
        },
        timeout_ms=timeout_ms,
    )
    matches = data.get("matches", []) if isinstance(data.get("matches"), list) else []
    status = str(data.get("status", "failed"))
    warnings = local_warnings + (data.get("warnings", []) if isinstance(data.get("warnings"), list) else [])
    visible_matches, text_truncated = _compact_matches(matches)
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
        extra={"matches": visible_matches, "engine": data.get("engine"), "output_mode": output_mode, "offset": offset, "total_matches": data.get("total_matches"), "truncated": bool(data.get("truncated", False)) or text_truncated, "error": data.get("error"), "error_details": data.get("error_details")},
    )
    text = compact_text("\n".join(visible_matches) + ("\n<truncated>\n" if data.get("truncated") or text_truncated else "\n"))
    if status != "ok":
        text = result["summary"] + "\n" + str(data.get("error") or "Search failed.") + "\n" + text.lstrip()
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
