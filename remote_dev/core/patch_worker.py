"""
[Role] Remote Codex patch worker executed on the target host (`patch_worker.py`).
[Invariants]
- Evaluates multi-file patches in an in-memory virtual overlay first; only commits when every hunk and path check succeeds.
- Snapshots touched files prior to commit and rolls back to `before_state` if any file replacement fails mid-commit.
[Timeouts & Locks]
- Runs inside a caller-bounded RPC Python worker under `@serialize_mutation`.
[Side-Effect Policy]
- Atomically writes/deletes target files via temp file + `os.fsync` + `os.replace`.
"""
import difflib
import hashlib
import json
import os
import pathlib
import sys
import tempfile

payload = json.loads(sys.stdin.read())
root = pathlib.Path(payload["root"]).resolve()
cwd = pathlib.Path(payload["cwd"]).resolve()
ops = payload["ops"]
DELETED = object()

def fail(status, error=None, **extra):
    data = {"status": status}
    if error:
        data["error"] = error
    data.update(extra)
    print(json.dumps(data, sort_keys=True))
    raise SystemExit(0)

def resolve_path(raw, *, parent_ok=False):
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = cwd / p
    try:
        if p.exists() or p.is_symlink():
            resolved = p.resolve()
        elif parent_ok:
            resolved = p.parent.resolve() / p.name
        else:
            resolved = p.resolve(strict=True)
    except FileNotFoundError:
        fail("not_found", f"remote path does not exist: {p}")
    if resolved != root and root not in resolved.parents:
        fail("path_outside_root", f"remote path is outside root: {resolved} not under {root}")
    # Overlay keys must identify one file. Unresolved Path treats
    # `a.py` and `sub/../a.py` (or a path through an in-root dir
    # symlink) as different keys, so an earlier hunk is discarded
    # while both ops report applied. Keep the unresolved path only
    # for a symlink so file_bytes / atomic_write still refuse it.
    return p if p.is_symlink() else resolved

def sha(data):
    return hashlib.sha256(data).hexdigest()

def file_bytes(path):
    if path in virtual:
        data = virtual[path]
        return None if data is DELETED else data
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        fail("symlink_not_allowed" if path.is_symlink() else "not_file", f"refusing to patch non-regular file: {path}")
    return path.read_bytes()

def file_sha_bytes(data):
    return sha(data) if data is not None else None

def exists_in_virtual(path):
    data = file_bytes(path)
    return data is not None

created_dirs = []

def atomic_write(path, data, mode=None):
    if path.is_symlink():
        fail("symlink_not_allowed", f"refusing to patch symlink: {path}")
    if path.exists() and not path.is_file():
        fail("not_file", f"refusing to patch non-regular file: {path}")
    parent = path.parent
    missing = []
    while parent != parent.parent and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    created_dirs.extend(reversed(missing))
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

def snapshot_real(path):
    if not path.exists() and not path.is_symlink():
        return {"exists": False, "bytes": None, "mode": None}
    if path.is_symlink() or not path.is_file():
        fail("symlink_not_allowed" if path.is_symlink() else "not_file", f"refusing to patch non-regular file: {path}")
    st = path.stat()
    return {"exists": True, "bytes": path.read_bytes(), "mode": st.st_mode & 0o777}

def restore(before_state):
    restored = []
    failed = []
    for path, state in reversed(list(before_state.items())):
        try:
            current_exists = path.exists() or path.is_symlink()
            if state["exists"]:
                current_bytes = path.read_bytes() if path.exists() and path.is_file() else None
                if current_exists and current_bytes == state["bytes"]:
                    continue
                atomic_write(path, state["bytes"], state["mode"])
            else:
                if not current_exists:
                    continue
                path.unlink()
            restored.append(str(path))
        except Exception as exc:  # noqa: BLE001
            failed.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"restored": restored, "failed": failed}

changed = []
diffs = []
virtual = {}
touched_paths = []
for op in ops:
    kind = op["kind"]
    path = resolve_path(op["path"], parent_ok=True)
    before_bytes = file_bytes(path)
    before_sha = file_sha_bytes(before_bytes)
    before_text = before_bytes.decode("utf-8", errors="replace") if before_bytes is not None else ""
    path_for_diff = path
    if kind == "add":
        if exists_in_virtual(path) or path.is_symlink():
            fail("file_exists", f"add file target already exists: {path}")
        after_text = op.get("content", "")
        virtual[path] = after_text.encode("utf-8")
    elif kind == "delete":
        if before_bytes is None:
            fail("not_found", f"delete target does not exist: {path}")
        virtual[path] = DELETED
        after_text = ""
    elif kind == "update":
        if before_bytes is None:
            fail("not_found", f"update target does not exist: {path}")
        target_path = resolve_path(op["move_to"], parent_ok=True) if op.get("move_to") else path
        if op.get("move_to"):
            if exists_in_virtual(target_path) or target_path.exists() or target_path.is_symlink():
                fail("file_exists", f"move target already exists: {target_path}")
        after_text = before_text
        for hunk_index, hunk in enumerate(op.get("hunks", [])):
            old = hunk["old"]
            new = hunk["new"]
            anchor = hunk.get("anchor") or ""
            if old == "":
                fail("context_mismatch", f"patch hunk has no context in {path}; refuse unanchored insertion", hunk_index=hunk_index)
            if old not in after_text:
                fail("context_mismatch", f"patch context not found in {path}; re-run remote.read before retrying", hunk_index=hunk_index)
            if anchor:
                pos = after_text.find(anchor)
                if pos < 0:
                    fail("context_mismatch", f"patch anchor {anchor!r} not found in {path}", hunk_index=hunk_index)
                rel = after_text.find(old, pos)
                if rel < 0:
                    fail("context_mismatch", f"patch context not found after anchor in {path}", hunk_index=hunk_index)
                after_text = after_text[:rel] + new + after_text[rel + len(old):]
            else:
                after_text = after_text.replace(old, new, 1)
        virtual[target_path] = after_text.encode("utf-8")
        if target_path != path:
            virtual[path] = DELETED
        path_for_diff = target_path
    else:
        fail("invalid_patch", f"unsupported patch op: {kind}")
    if path not in touched_paths:
        touched_paths.append(path)
    if path_for_diff not in touched_paths:
        touched_paths.append(path_for_diff)
    after_sha = file_sha_bytes(file_bytes(path_for_diff))
    diff = "".join(difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path_for_diff}",
        n=3,
    ))
    diffs.append(diff)
    changed.append({
        "path": str(path_for_diff),
        "old_path": str(path) if path_for_diff != path else None,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "size": path_for_diff.stat().st_size if path_for_diff.exists() else 0,
        "op": "move" if kind == "update" and op.get("move_to") and not op.get("hunks") else kind,
    })

before_state = {path: snapshot_real(path) for path in touched_paths}
try:
    for path, data in virtual.items():
        if data is DELETED:
            continue
        mode = before_state.get(path, {}).get("mode")
        atomic_write(path, data, mode)
    for path, data in virtual.items():
        if data is DELETED and (path.exists() or path.is_symlink()):
            path.unlink()
except Exception as exc:  # noqa: BLE001
    rollback_status = restore(before_state)
    fail("commit_failed", f"{type(exc).__name__}: {exc}", rollback_status=rollback_status)

for item in changed:
    path = pathlib.Path(item["path"])
    item["after_sha256"] = sha(path.read_bytes()) if path.exists() and path.is_file() else None
    item["size"] = path.stat().st_size if path.exists() else 0
print(json.dumps({"status": "applied", "changed_files": changed, "diff_preview": "".join(diffs)[:16000]}, sort_keys=True))
