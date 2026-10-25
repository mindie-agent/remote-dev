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

def matches_exclude(rel_norm, pattern):
    """Match one exclude glob against a path, its basename, and its components.

    Native callers spell recursive excludes both as ``.git`` and ``**/.git``.
    ``fnmatch`` treats ``**`` as an ordinary ``*``, so a leading ``**/`` would
    otherwise miss the same name at the search root (for example a top-level
    ``.env`` against ``**/.env``).
    """
    variants = [str(pattern)]
    if variants[0].startswith("**/"):
        variants.append(variants[0][3:])
    parts = rel_norm.split("/")
    basename = parts[-1] if parts else rel_norm
    for variant in variants:
        candidate = variant.rstrip("/")
        if not candidate:
            continue
        if fnmatch.fnmatch(rel_norm, candidate) or fnmatch.fnmatch(basename, candidate):
            return True
        if any(fnmatch.fnmatch(part, candidate) for part in parts):
            return True
    return False


def bounded_glob(base, pattern, include_hidden=False):
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

    def children(directory, part):
        escaped = glob_mod.escape(directory)
        seen = set()
        for child in glob_mod.iglob(os.path.join(escaped, part)):
            if child not in seen:
                seen.add(child)
                yield child
        if include_hidden and not part.startswith("."):
            for child in glob_mod.iglob(os.path.join(escaped, "." + part)):
                if child not in seen:
                    seen.add(child)
                    yield child

    def walk(directory, remaining, ancestors):
        _, resolved = resolve_path(base / directory)
        if not remaining:
            if not directory_only or (base / directory).is_dir():
                yield directory + ("/" if directory_only and directory else "")
            return
        part, rest = remaining[0], remaining[1:]
        if part == "**":
            yield from walk(directory, rest, ancestors)
            for child in children(directory, "*"):
                _, real_child = resolve_path(base / child)
                if (base / child).is_dir():
                    if real_child not in ancestors:
                        yield from walk(child, remaining, ancestors | {resolved})
                elif not rest and not directory_only:
                    yield child
            return
        for child in children(directory, part):
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

def filter_gitignored_paths(base, paths):
    """Apply the same .gitignore evaluator Glob uses to ``grep -l`` paths."""
    if not base.is_dir():
        return paths, []
    items = []
    base_resolved = base.resolve()
    for path in paths:
        try:
            relative = pathlib.Path(path).resolve().relative_to(base_resolved)
        except (OSError, ValueError):
            continue
        items.append({"path": path, "relpath": str(relative).replace("\\", "/"), "type": "file"})
    kept, warnings = apply_gitignore(base, items)
    return [item["path"] for item in kept], warnings


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


def collect_page(cmd, output_mode, offset, limit, max_line_chars, *, skip_zeros=False, char_budget=5000000):
    import contextlib
    matches, truncated_lines = [], 0
    used_chars = 0
    next_offset = None
    total_matches = 0 if output_mode == "count_matches" else None
    eligible = 0
    cap = max_line_chars if output_mode == "content" else 32768
    with contextlib.closing(command_lines(cmd, cap)) as rows:
        for line, clipped in rows:
            # Zero-count files are filtered by grep -c, not by the caller's
            # offset: offset must skip the same entries the caller sees.
            if skip_zeros and line.endswith(":0"):
                continue
            if total_matches is not None and line.rsplit(":", 1)[-1].isdigit():
                total_matches += int(line.rsplit(":", 1)[-1])
            if eligible < offset:
                eligible += 1
                continue
            eligible += 1
            if len(matches) >= limit or (used_chars + len(line) + 1 > char_budget and matches):
                if next_offset is None:
                    next_offset = offset + len(matches)
                # count_matches must still report the aggregate total over all
                # entries; other modes can stop at the first undelivered one.
                if total_matches is None:
                    break
                continue
            if clipped:
                line += "<remote-dev line truncated>"
                truncated_lines += 1
            matches.append(line)
            used_chars += len(line) + 1
    return matches, next_offset is not None, total_matches, truncated_lines, next_offset


def collect_all_lines(cmd, output_mode, max_line_chars, *, skip_zeros=False, char_budget=5000000):
    """Collect the complete result set for native mtime-ordering semantics."""
    import contextlib
    lines, clipped, used, budget_hit = [], 0, 0, False
    cap = max_line_chars if output_mode == "content" else 32768
    with contextlib.closing(command_lines(cmd, cap)) as rows:
        for line, was_clipped in rows:
            if skip_zeros and line.endswith(":0"):
                continue
            if was_clipped:
                line += "<remote-dev line truncated>"
                clipped += 1
            if used + len(line) + 1 > char_budget and lines:
                budget_hit = True
                break
            lines.append(line)
            used += len(line) + 1
    return lines, clipped, budget_hit


def sort_files_by_mtime(lines):
    """Native Claude/Kimi order: most recently modified first, stable on ties."""
    entries = []
    for index, line in enumerate(lines):
        try:
            mtime = os.stat(line).st_mtime
        except OSError:
            mtime = 0.0
        entries.append((mtime, index, line))
    entries.sort(key=lambda item: (-item[0], item[1]))
    return [line for _mtime, _index, line in entries]


if op == "glob":
    base, resolved = resolve_path(payload.get("path") or payload["root"])
    if not base.is_dir():
        fail("not_directory", f"RemoteGlob path is not a directory: {base}")
    pattern = payload.get("pattern") or "*"
    limit = int(payload["limit"]) if payload.get("limit") is not None else 100
    if limit == 0:
        limit = 1000000
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
    hidden = bool(payload.get("hidden", False))
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
    for item in bounded_glob(base, eff_pattern, include_hidden=hidden):
        rel_norm = item.replace("\\", "/")
        if rel_norm.startswith("./"):
            rel_norm = rel_norm[2:]
        if not rel_norm:
            continue
        if max_depth is not None:
            depth = len([p for p in rel_norm.split("/") if p])
            if depth > max_depth:
                continue
        if full_path and not fnmatch.fnmatch(rel_norm, pattern):
            continue
        if excludes and any(matches_exclude(rel_norm, ex) for ex in excludes):
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
    truncated = len(matches) > offset + limit
    print(json.dumps({"status": "ok", "matches": page, "offset": offset,
                      "next_offset": offset + len(page) if truncated else None,
                      "truncated": truncated, "warnings": warnings}, sort_keys=True))
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
        limit = 1000000
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
    hidden = bool(payload.get("hidden", False))
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
    if rg_path is None and payload.get("require_rg"):
        fail("rg_required", "native Claude/Kimi Grep semantics require ripgrep (rg) on the remote host; install rg or use the canonical remote.grep fallback")
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
        elif hidden:
            cmd.append("--hidden")
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
        if output_mode == "files_with_matches" and payload.get("sort_by_mtime"):
            lines, clipped, budget_hit = collect_all_lines(cmd, output_mode, max_line_chars)
            ordered = sort_files_by_mtime(lines)
            page = ordered[offset:offset + limit] if limit > 0 else ordered[offset:]
            truncated = budget_hit or len(ordered) > offset + len(page)
            next_offset = offset + len(page) if truncated else None
            if clipped:
                warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
            if budget_hit:
                warnings.append("result set exceeded the search character budget; mtime ordering covers the collected prefix only")
            print(json.dumps({"status": "ok", "engine": "rg", "output_mode": output_mode, "offset": offset,
                              "next_offset": next_offset,
                              "matches": page, "total_matches": None, "truncated": truncated,
                              "warnings": warnings}, sort_keys=True))
            raise SystemExit(0)
        lines, truncated, total_matches, clipped, next_offset = collect_page(cmd, output_mode, offset, limit, max_line_chars)
        if clipped:
            warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
        print(json.dumps({"status": "ok", "engine": "rg", "output_mode": output_mode, "offset": offset,
                          "next_offset": next_offset,
                          "matches": lines, "total_matches": total_matches, "truncated": truncated,
                          "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)

    # rg is unavailable: fall back to POSIX `grep -E` (or `grep -F` when fixed_strings=True).
    if multiline:
        fail("rg_required", "multiline grep requires ripgrep (rg) on the remote host; install rg or drop multiline")
    if type_name:
        fail("rg_required", f"grep fallback cannot honor --type {type_name}; install ripgrep (rg) or use --glob")
    for pat in ([glob_pattern] if glob_pattern else []) + list(includes):
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
    if not include_ignored and output_mode != "files_with_matches":
        warnings.append(
            "grep fallback cannot evaluate .gitignore; ignored-path filtering is approximate"
        )
    cmd = [grep_path, "-r", mode_flag, "-I"]
    if case_insensitive:
        cmd.append("-i")
    if include_ignored:
        warnings.append("grep fallback cannot evaluate .gitignore; include_ignored lifts hidden-directory excludes while VCS metadata directories stay excluded")
        if not fnmatch.fnmatch(base.name, ".git"):
            cmd.append("--exclude-dir=.git")
    else:
        if not fnmatch.fnmatch(base.name, ".git"):
            cmd.append("--exclude-dir=.git")
        if not hidden and not fnmatch.fnmatch(base.name, ".*"):
            cmd.append("--exclude-dir=.*")
    if glob_pattern:
        cmd.append(f"--include={glob_pattern}")
    for inc in includes:
        if inc:
            cmd.append(f"--include={inc}")
    unhandled_excludes = []
    for exc in excludes:
        if not exc:
            continue
        clean_exc = str(exc).lstrip('!')
        if "/" in clean_exc:
            # Native callers pass both ``.git`` and ``**/.git``. Honor the
            # basename form of a path-anchored exclude; anything more complex
            # (for example ``**/.aws/credentials/**``) is reported rather than
            # silently widening the search.
            tail = clean_exc.rsplit("/", 1)[-1]
            if tail and "*" not in tail and "?" not in tail:
                cmd.append(f"--exclude={tail}")
                cmd.append(f"--exclude-dir={tail}")
            else:
                unhandled_excludes.append(clean_exc)
            continue
        cmd.append(f"--exclude={clean_exc}")
        cmd.append(f"--exclude-dir={clean_exc}")
    if unhandled_excludes:
        warnings.append(
            "grep fallback cannot honor "
            f"{len(unhandled_excludes)} path-anchored exclude(s): {', '.join(unhandled_excludes)}"
        )
    if output_mode == "count_matches":
        import contextlib
        counts, total_matches, eligible, next_offset = [], 0, 0, None
        count_cmd = [grep_path, "-o", mode_flag]
        if case_insensitive:
            count_cmd.append("-i")
        with contextlib.closing(command_lines([*cmd, "-l", "--", pattern, str(base)])) as candidates:
            for candidate, _ in candidates:
                amount = sum(1 for _ in command_lines([*count_cmd, "--", pattern, candidate]))
                total_matches += amount
                if eligible < offset:
                    eligible += 1
                    continue
                eligible += 1
                if len(counts) < limit:
                    counts.append(f"{candidate}:{amount}")
                elif next_offset is None:
                    next_offset = offset + len(counts)
        print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "offset": offset,
                          "matches": counts, "total_matches": total_matches,
                          "next_offset": next_offset, "truncated": next_offset is not None,
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
    if output_mode == "files_with_matches" and payload.get("sort_by_mtime"):
        lines, clipped, budget_hit = collect_all_lines(cmd, output_mode, max_line_chars)
        if not include_ignored:
            lines, gi_warnings = filter_gitignored_paths(base, lines)
            warnings.extend(gi_warnings)
            warnings.append(
                "grep fallback: files_with_matches applied .gitignore in-process; "
                "content/count modes remain approximate for ignored paths"
            )
        ordered = sort_files_by_mtime(lines)
        page = ordered[offset:offset + limit] if limit > 0 else ordered[offset:]
        truncated = budget_hit or len(ordered) > offset + len(page)
        next_offset = offset + len(page) if truncated else None
        if clipped:
            warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
        if budget_hit:
            warnings.append("result set exceeded the search character budget; mtime ordering covers the collected prefix only")
        print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "offset": offset,
                          "next_offset": next_offset,
                          "matches": page, "total_matches": None, "truncated": truncated,
                          "warnings": warnings}, sort_keys=True))
        raise SystemExit(0)
    if output_mode == "files_with_matches" and not include_ignored:
        all_lines, all_clipped, budget_hit = collect_all_lines(cmd, output_mode, max_line_chars)
        all_lines, gi_warnings = filter_gitignored_paths(base, all_lines)
        warnings.extend(gi_warnings)
        warnings.append(
            "grep fallback: files_with_matches applied .gitignore in-process; "
            "content/count modes remain approximate for ignored paths"
        )
        page = all_lines[offset:offset + limit] if limit > 0 else all_lines[offset:]
        truncated = budget_hit or len(all_lines) > offset + len(page)
        next_offset = offset + len(page) if truncated else None
        lines, clipped = page, all_clipped
    else:
        lines, truncated, _, clipped, next_offset = collect_page(cmd, output_mode, offset, limit, max_line_chars,
                                                    skip_zeros=output_mode == "count")
    if clipped:
        warnings.append(f"{clipped} line(s) truncated to {max_line_chars} chars")
    print(json.dumps({"status": "ok", "engine": "grep", "output_mode": output_mode, "offset": offset,
                      "next_offset": next_offset,
                      "matches": lines, "total_matches": None, "truncated": truncated, "warnings": warnings}, sort_keys=True))
    raise SystemExit(0)

fail("unsupported_op", f"unsupported search op: {op}")
