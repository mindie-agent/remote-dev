"""
[Role] Remote workspace search worker executed on the target host (`search_worker.py`).
[Invariants]
- Enforces workspace root containment (`resolve_path`) and bounded line/match pagination.
- Uses `rg` when available on the remote endpoint and falls back to POSIX `grep -E`/`grep -F` with explicit capability checks.
[Timeouts & Locks]
- Subprocess `git` ignore probes use explicit 10s/30s timeouts; `command_lines` terminates child processes cleanly on early pagination exit.
[Side-Effect Policy]
- Pure remote read/search execution; mutates no files on the remote host.
"""
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

def bounded_glob(base, pattern):
    # Expand one path component at a time: a final-result check alone lets
    # recursive glob scan an escaping directory before its matches are checked.
    start = ""
    directory_only = pattern.endswith("/")
    if os.path.isabs(pattern):
        absolute = pathlib.Path(pattern)
        for allowed in (pathlib.Path(payload["root"]), root):
            try:
                absolute.relative_to(allowed)
            except ValueError:
                continue
            pattern = pattern[len(str(allowed).rstrip("/")):].lstrip("/")
            start = str(allowed)
            break
        else:
            fail("path_outside_root", "absolute glob pattern is outside root")
    parts = [part for part in pattern.split("/") if part]

    def walk(directory, remaining, ancestors):
        _, resolved = resolve_path(base / directory)
        if not remaining:
            if not directory_only or (base / directory).is_dir():
                yield directory + ("/" if directory_only and directory else "")
            return
        part, rest = remaining[0], remaining[1:]
        if part == "**":
            yield from walk(directory, rest, ancestors)
            for child in glob_mod.iglob(os.path.join(glob_mod.escape(directory), "*")):
                _, real_child = resolve_path(base / child)
                if (base / child).is_dir():
                    if real_child not in ancestors:
                        yield from walk(child, remaining, ancestors | {resolved})
                elif not rest and not directory_only:
                    yield child
            return
        for child in glob_mod.iglob(os.path.join(glob_mod.escape(directory), part)):
            resolve_path(base / child)
            if not rest or (base / child).is_dir():
                yield from walk(child, rest, ancestors)

    yield from walk(start, parts, set())

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
    candidates = []
    def scan_error(error):
        raise error
    for directory, _dirs, files in os.walk(root, onerror=scan_error):
        if ".gitignore" in files:
            candidates.append(pathlib.Path(directory) / ".gitignore")
    candidates.sort()
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
        try:
            probe = subprocess.run(
                [git, "-C", str(base), "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            fail("failed", "git ignore probe timed out")
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            rels = [item["relpath"] for item in matches]
            try:
                chk = subprocess.run(
                    [git, "-C", str(base), "check-ignore", "-z", "--stdin"],
                    input="".join(rel + "\0" for rel in rels).encode("utf-8"),
                    capture_output=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                fail("failed", "git check-ignore timed out")
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
        try:
            code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=5.0)
        reader.join(timeout=1.0)
        if code not in (0, 1):
            fail("failed", tail.decode("utf-8", "replace"))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5.0)
        reader.join(timeout=1.0)
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
    limit = int(payload["limit"]) if payload.get("limit") is not None else 100
    if limit == 0:
        limit = 100000
    offset = int(payload.get("offset") or 0)
    if offset < 0:
        fail("invalid_pagination", "offset must be >= 0")
    respect_gitignore = bool(payload.get("respect_gitignore"))
    extensions = payload.get("extensions") or []
    if isinstance(extensions, str):
        extensions = [extensions]
    norm_exts = {str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()}
    excludes = payload.get("excludes") or []
    if isinstance(excludes, str):
        excludes = [excludes]
    entry_type = payload.get("entry_type")
    if entry_type in {"dir", "d"}:
        entry_type = "directory"
    elif entry_type == "f":
        entry_type = "file"
    max_depth = int(payload["max_depth"]) if payload.get("max_depth") is not None else None
    full_path = bool(payload.get("full_path", False))
    has_extra_filters = bool(norm_exts or excludes or entry_type in {"file", "directory"} or max_depth is not None or full_path)
    eff_pattern = pattern
    if has_extra_filters and "/" not in eff_pattern and "**" not in eff_pattern and (max_depth is None or max_depth > 1):
        eff_pattern = f"**/{eff_pattern}"
    matches = []
    warnings = []
    import heapq
    seen = 0
    # One-shot helper: chdir so glob(pattern, recursive=True) works on Python 3.9 (no root_dir).
    os.chdir(str(base))
    for item in bounded_glob(base, eff_pattern):
        rel_norm = item.replace("\\", "/").lstrip("./")
        if not rel_norm:
            continue
        if max_depth is not None:
            depth = len([p for p in rel_norm.split("/") if p])
            if depth > max_depth:
                continue
        if full_path and not fnmatch.fnmatch(rel_norm, pattern):
            continue
        if excludes:
            parts = rel_norm.split("/")
            if any(
                fnmatch.fnmatch(rel_norm, ex)
                or fnmatch.fnmatch(parts[-1], ex)
                or any(fnmatch.fnmatch(part, ex.rstrip("/")) for part in parts)
                for ex in excludes
            ):
                continue
        path = base / item
        try:
            st = path.lstat()
        except FileNotFoundError:
            continue
        kind = "directory" if path.is_dir() else "file"
        if entry_type in {"file", "directory"} and kind != entry_type:
            continue
        if norm_exts:
            suffix = path.suffix.lstrip(".").lower()
            if suffix not in norm_exts:
                continue
        row = {"path": str(path), "relpath": item, "type": kind, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
        seen += 1
        if respect_gitignore:
            matches.append(row)
        else:
            heapq.heappush(matches, (st.st_mtime_ns, -seen, row))
            if len(matches) > limit + offset + 1:
                heapq.heappop(matches)
    if respect_gitignore:
        matches, gi_warnings = apply_gitignore(base, matches)
        warnings.extend(gi_warnings)
    if respect_gitignore:
        matches.sort(key=lambda row: row["mtime_ns"], reverse=True)
    else:
        matches = [row for _, _, row in sorted(matches, reverse=True)]
    page = matches[offset:offset + limit]
    print(json.dumps({"status": "ok", "matches": page, "offset": offset, "truncated": len(matches) > offset + limit, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

if op == "grep":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.exists():
        fail("not_found", f"RemoteGrep path does not exist: {base}")
    pattern = payload.get("pattern")
    if not pattern:
        fail("pattern_required", "RemoteGrep requires pattern")
    limit = int(payload["limit"]) if payload.get("limit") is not None else 100
    if limit == 0:
        limit = 5000
    offset = int(payload.get("offset") or 0)
    if offset < 0:
        fail("invalid_pagination", "offset must be >= 0")
    max_line_chars = int(payload.get("max_line_chars") or 2000)
    output_mode = payload.get("output_mode") or "files_with_matches"
    glob_pattern = payload.get("glob")
    includes = payload.get("includes") or []
    if isinstance(includes, str):
        includes = [includes]
    excludes = payload.get("excludes") or []
    if isinstance(excludes, str):
        excludes = [excludes]
    type_name = payload.get("type")
    multiline = bool(payload.get("multiline", False))
    case_insensitive = bool(payload.get("case_insensitive", False))
    fixed_strings = bool(payload.get("fixed_strings", False))
    only_matching = bool(payload.get("only_matching", False))
    before_context = int(payload.get("before_context") or 0)
    after_context = int(payload.get("after_context") or 0)
    context_lines = int(payload.get("context_lines") or 0)
    include_ignored = bool(payload.get("include_ignored", False))
    line_numbers = payload.get("line_numbers")
    line_numbers = True if line_numbers is None else bool(line_numbers)
    if output_mode != "content" and (before_context or after_context or context_lines or only_matching):
        warnings_context = "context/only-matching options only apply to output_mode=content; ignoring them"
    else:
        warnings_context = ""
    warnings = []
    if warnings_context:
        warnings.append(warnings_context)
    rg_path = shutil.which("rg")
    if rg_path:
        cmd = [rg_path, "--color", "never"]
        if fixed_strings:
            cmd.append("-F")
        if multiline:
            cmd.append("-U")
        if case_insensitive:
            cmd.append("-i")
        if include_ignored:
            cmd.extend(["--no-ignore", "--hidden"])
        if glob_pattern:
            cmd.extend(["--glob", glob_pattern])
        for inc in includes:
            if inc:
                cmd.extend(["--glob", str(inc)])
        for exc in excludes:
            if exc:
                clean_exc = str(exc).lstrip("!")
                cmd.extend(["--glob", f"!{clean_exc}"])
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
            if only_matching:
                cmd.append("-o")
            cmd.append("-n" if line_numbers else "--no-line-number")
            if context_lines:
                cmd.extend(["-C", str(context_lines)])
            if before_context:
                cmd.extend(["-B", str(before_context)])
            if after_context:
                cmd.extend(["-A", str(after_context)])
        cmd.extend(["--", pattern, str(base)])
        lines, truncated, total_matches, clipped = collect_page(cmd, output_mode, offset, limit, max_line_chars)
        if clipped:
            warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
        print(json.dumps({"status": "ok", "engine": "rg", "output_mode": output_mode, "offset": offset,
                          "matches": lines, "total_matches": total_matches, "truncated": truncated,
                          "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)

    # rg is unavailable: fall back to POSIX `grep -E` (or `grep -F` when fixed_strings=True).
    if multiline:
        fail("rg_required", "multiline grep requires ripgrep (rg) on the remote host; install rg or drop multiline")
    if type_name:
        fail("rg_required", f"grep fallback cannot honor --type {type_name}; install ripgrep (rg) or use --glob")
    for pat in ([glob_pattern] if glob_pattern else []) + list(includes) + list(excludes):
        if pat and "/" in str(pat):
            fail("rg_required", f"grep fallback cannot honor path-anchored --glob {pat}; install ripgrep (rg) or use a basename glob")
    grep_path = shutil.which("grep")
    if not grep_path:
        fail("grep_unavailable", "neither rg nor grep found on the remote host")
    mode_flag = "-F" if fixed_strings else "-E"
    warnings.append(
        "rg not found; used grep -F fallback (fixed-strings semantics)"
        if fixed_strings
        else "rg not found; used grep -E fallback (POSIX ERE semantics)"
    )
    cmd = [grep_path, "-r", mode_flag, "-I"]
    if case_insensitive:
        cmd.append("-i")
    if include_ignored:
        warnings.append("grep fallback cannot evaluate .gitignore; include_ignored only re-enables hidden and .git directories")
    else:
        for exclude_dir in (".git", ".*"):
            if fnmatch.fnmatch(base.name, exclude_dir):
                continue
            cmd.append(f"--exclude-dir={exclude_dir}")
    if glob_pattern:
        cmd.append(f"--include={glob_pattern}")
    for inc in includes:
        if inc:
            cmd.append(f"--include={inc}")
    for exc in excludes:
        if exc:
            cmd.append(f"--exclude={str(exc).lstrip('!')}")
    if output_mode == "count_matches":
        import contextlib
        counts, total_matches, count = [], 0, 0
        count_cmd = [grep_path, "-o", mode_flag]
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
