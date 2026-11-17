"""
[Role] Remote artifact manifest generation and hash-verified pull/push orchestration (`artifact_ops.py`).
[Invariants]
- Allows valid symlinks within authorized `endpoint.root` / explicit `local_path` (e.g., HuggingFace Hub snapshots and `.so` links) while rejecting symlinks or `..` traversal escaping the authorized root, and refusing to overwrite destination symlinks on write.
- Streams SHA-256 digests in `64 KiB` chunks; skips hash-identical files during `remote_artifact_pull` and refuses to overwrite differing local files unless `overwrite=True`.
[Timeouts & Locks]
- Serializes transfers per endpoint via `@serialize_mutation` and forwards `timeout_ms` to `ArtifactStream`.
[Side-Effect Policy]
- Records manifests under `state/endpoints/<id>/artifacts/<artifact_id>/manifest.json` (never polluting the user's `local_dir` with tool metadata).
"""
from __future__ import annotations

from remote_dev.observability import observed_tool

import hashlib
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from .locking import record_lock, serialize_mutation

from .endpoint import Endpoint
from .container_endpoint import pinned_endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from remote_dev.result import make_result, utc_now_iso
from .ssh_transport import run_remote_python
from .artifact_transport import ArtifactStream, ArtifactTransferError
from .errors import RemoteExecutionError, error_details
from .state_store import atomic_write_json, ensure_endpoint_state

REMOTE_MANIFEST_PY = r'''
import hashlib
import json
import os
import pathlib
import stat
import sys

payload = json.loads(sys.stdin.read())
root = pathlib.Path(payload["root"]).resolve()
target = pathlib.Path(payload["remote_path"])
if not target.is_absolute():
    target = pathlib.Path(payload.get("cwd") or payload["root"]) / target
resolved = target.resolve()
if resolved != root and root not in resolved.parents:
    print(json.dumps({"status": "blocked", "error": f"remote path is outside root: {resolved}", "remote_path": str(target)}))
    raise SystemExit(0)
if not resolved.exists():
    print(json.dumps({"status": "needs_input", "error": "remote path does not exist", "remote_path": str(target)}))
    raise SystemExit(0)

def sha256_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size

def within_root(candidate):
    real = candidate.resolve()
    return real == root or root in real.parents, real

files = []
if resolved.is_file():
    candidates = [target]
else:
    candidates = []
    visited = set()
    for cur_dir, dirs, filenames in os.walk(target, followlinks=True):
        cur_path = pathlib.Path(cur_dir)
        ok, real_dir = within_root(cur_path)
        if not ok:
            print(json.dumps({"status": "blocked", "error": f"artifact symlink escapes root: {cur_path} -> {real_dir}"}))
            raise SystemExit(0)
        try:
            dir_st = real_dir.stat()
        except OSError:
            dirs[:] = []
            continue
        dir_id = (dir_st.st_dev, dir_st.st_ino)
        if dir_id in visited:
            dirs[:] = []
            continue
        visited.add(dir_id)
        dirs.sort()
        for fname in sorted(filenames):
            candidates.append(cur_path / fname)

for path in candidates:
    ok, real = within_root(path)
    if not ok:
        print(json.dumps({"status": "blocked", "error": f"artifact symlink escapes root: {path} -> {real}"}))
        raise SystemExit(0)
    if not real.exists() or not real.is_file():
        if path.is_symlink() and not real.exists():
            print(json.dumps({"status": "blocked", "error": f"dangling artifact symlink: {path}"}))
            raise SystemExit(0)
        continue
    digest, size = sha256_file(real)
    st = real.stat()
    files.append({
        "relpath": "." if path == target else path.relative_to(target).as_posix(),
        "path": str(path),
        "size": size,
        "sha256": digest,
        "mode": stat.S_IMODE(st.st_mode),
        "mtime_ns": st.st_mtime_ns,
    })
files.sort(key=lambda item: pathlib.PurePosixPath(item["relpath"]).parts)
print(json.dumps({
    "schema_version": "remote-dev.artifact_manifest.v1",
    "status": "ok",
    "root": str(target),
    "is_dir": target.is_dir(),
    "file_count": len(files),
    "total_bytes": sum(item["size"] for item in files),
    "files": files,
}, sort_keys=True))
'''


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_manifest(local_path: Path) -> dict[str, Any]:
    import os as _os

    raw_path = local_path.expanduser()
    resolved = raw_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"local artifact path does not exist: {raw_path}")
    files: list[dict[str, Any]] = []
    if resolved.is_file():
        candidates = [resolved]
    else:
        candidates = []
        visited: set[tuple[int, int]] = set()
        for cur_dir, dirs, filenames in _os.walk(resolved, followlinks=True):
            cur_path = Path(cur_dir)
            real_dir = cur_path.resolve()
            if real_dir != resolved and resolved not in real_dir.parents:
                raise ValueError(f"local artifact symlink escapes source root: {cur_path}")
            dir_st = real_dir.stat()
            dir_id = (dir_st.st_dev, dir_st.st_ino)
            if dir_id in visited:
                dirs[:] = []
                continue
            visited.add(dir_id)
            dirs.sort()
            for fname in sorted(filenames):
                candidates.append(cur_path / fname)
    for path in candidates:
        real = path.resolve()
        if real != resolved and resolved not in real.parents:
            raise ValueError(f"local artifact symlink escapes source root: {path}")
        if not real.exists():
            if path.is_symlink():
                raise ValueError(f"dangling local artifact symlink: {path}")
            continue
        if not real.is_file():
            continue
        stat = real.stat()
        files.append({
            "relpath": "." if path == resolved else path.relative_to(resolved).as_posix(),
            "path": str(real),
            "size": stat.st_size,
            "sha256": _sha256_file(real),
            "mtime_ns": stat.st_mtime_ns,
        })
    files.sort(key=lambda item: PurePosixPath(item["relpath"]).parts)
    return {
        "schema_version": "remote-dev.local_artifact_manifest.v1",
        "status": "ok",
        "local_path": str(resolved),
        "is_dir": resolved.is_dir(),
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "files": files,
    }


def _safe_local_artifact_path(base: Path, relpath: str, *, create_parents: bool = True) -> Path:
    if relpath == ".":
        relpath = "artifact"
    rel = PurePosixPath(relpath)
    if rel.is_absolute() or any(part in {"..", ""} for part in rel.parts):
        raise ValueError(f"unsafe artifact relpath: {relpath}")
    candidate = base.joinpath(*rel.parts)
    base_resolved = base.resolve()
    # Check containment and the parent chain *before* mkdir. resolve()
    # follows a pre-existing symlink prefix without creating anything, so
    # `linkdir/sub/x` that points outside base is rejected with no
    # side effect. A file or dangling symlink in a parent position used
    # to raise FileExistsError from mkdir; treat that as ValueError too.
    parent_resolved = candidate.parent.resolve()
    if parent_resolved != base_resolved and base_resolved not in parent_resolved.parents:
        raise ValueError(f"artifact relpath escapes local dir: {relpath}")
    probe = base
    for part in rel.parts[:-1]:
        probe = probe / part
        if probe.is_symlink() or probe.is_file():
            raise ValueError(f"artifact relpath escapes local dir: {relpath}")
    if create_parents:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        except TimeoutError:
            raise
        except OSError as exc:
            raise ValueError(f"artifact relpath is not a creatable path: {relpath}") from exc
    if candidate.is_symlink():
        raise ValueError(f"refusing to overwrite local symlink: {candidate}")
    return candidate


@observed_tool("remote.artifact_manifest")
@pinned_endpoint
def remote_artifact_manifest(endpoint: Endpoint, *, remote_path: str, timeout_ms: int | None = None) -> dict[str, Any]:
    started = utc_now_iso()
    start = time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, remote_path)
    except PathPolicyError as exc:
        result = make_result(
            tool="remote.artifact_manifest",
            target=endpoint.to_result_target(),
            outcome="blocked",
            status="path_outside_root",
            summary=f"Remote artifact manifest blocked for {remote_path}.",
            started_at=started,
            duration_ms=_duration_ms(start),
            preview={"stderr": str(exc)},
            extra={"error": str(exc)},
        )
        return {"text": result["summary"] + "\n" + str(exc) + "\n", "result": result}
    data = run_remote_python(
        endpoint,
        REMOTE_MANIFEST_PY,
        {"root": endpoint.root, "cwd": endpoint.effective_cwd, "remote_path": path},
        timeout_ms=timeout_ms,
    )
    recording_error = None
    if isinstance(data, dict) and data.get("status") == "ok":
        data["endpoint_id"] = endpoint.endpoint_id
        artifact_id = f"manifest-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        data["artifact_id"] = artifact_id
        try:
            manifest_path = ensure_endpoint_state(endpoint) / "artifacts" / artifact_id / "manifest.json"
            atomic_write_json(manifest_path, data)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            recording_error = f"{type(exc).__name__}: {exc}"
            manifest_path = None
    else:
        manifest_path = None
    status = str(data.get("status", "failed"))
    result = make_result(
        tool="remote.artifact_manifest",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else ("blocked" if status == "blocked" else "failed"),
        status=status,
        summary=f"Remote artifact manifest {status} for {path}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        refs={"local_manifest": str(manifest_path)} if manifest_path else {},
        artifacts=[data] if status == "ok" else [],
        extra={"manifest": data, "error": data.get("error"), "error_details": data.get("error_details")},
    )
    if recording_error:
        execution_outcome, execution_status = result["outcome"], result["status"]
        primary_succeeded = execution_outcome == "success"
        result.update(outcome=execution_outcome if primary_succeeded else "failed",
                      status="local_recording_failed", operation_completed=True,
                      execution_outcome=execution_outcome, execution_status=execution_status,
                      recording_error=recording_error,
                      **({"receipt_warning": recording_error, "receipt_unconfirmed": True} if primary_succeeded else {}),
                      summary="Remote artifact manifest was received, but local recording failed.")
        return {"text": result["summary"] + "\n" + recording_error + "\n", "result": result}
    return {"text": f"RemoteArtifactManifest {status}: {path}\nfiles: {data.get('file_count', 0)}\n", "result": result}


def _transfer_failure(endpoint, tool, started, start, exc, evidence):
    message = str(exc)[-4000:]
    status = ("hash_mismatch" if "hash_mismatch" in message else "cancelled" if "cancelled" in message
              else "timeout" if "timed out" in message else "path_traversal" if isinstance(exc, ValueError) else "failed")
    outcome = "blocked" if status == "path_traversal" else status if status in {"cancelled", "timeout"} else "failed"
    result = make_result(tool=tool, target=endpoint.to_result_target(), outcome=outcome,
                         status=status, summary=f"Artifact transfer {status}.", started_at=started,
                         duration_ms=_duration_ms(start), preview={"stderr": message}, artifacts=[evidence],
                         extra={"error_details": error_details(exc), "expected_sha256": getattr(exc, "expected_sha256", None), "observed_sha256": getattr(exc, "observed_sha256", None),
                                "operation_completed": getattr(exc, "operation_completed", False),
                                "cleanup_error": getattr(exc, "cleanup_error", None), "automatic_retry": False})
    return {"text": result["summary"] + "\n" + message + "\n", "result": result}


def _pull_destination_exists(endpoint, started, start, evidence, conflicts):
    evidence["conflicts"] = conflicts
    result = make_result(
        tool="remote.artifact_pull", target=endpoint.to_result_target(),
        outcome="blocked", status="destination_exists",
        summary="Artifact pull would overwrite local files.",
        started_at=started, duration_ms=_duration_ms(start),
        preview={"stderr": "Choose another local_dir, rename the local file, or set overwrite=true."},
        artifacts=[evidence], extra={"conflicts": conflicts},
    )
    return {"text": result["summary"] + "\n" + "\n".join(conflicts) + "\n", "result": result}


@observed_tool("remote.artifact_pull")
@serialize_mutation
def remote_artifact_pull(endpoint: Endpoint, *, remote_path: str, local_dir: str | None = None,
                         overwrite: bool = False, timeout_ms: int | None = None) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    manifest_payload = remote_artifact_manifest(endpoint, remote_path=remote_path, timeout_ms=timeout_ms)
    manifest = manifest_payload["result"].get("manifest", {})
    if manifest_payload["result"]["outcome"] != "success" or manifest.get("status") != "ok":
        return manifest_payload
    import contextlib

    managed_state_pull = not local_dir
    base = Path(local_dir) if local_dir else ensure_endpoint_state(endpoint) / "artifacts" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    pull_guard = (
        record_lock(base, timeout_seconds=5.0)
        if managed_state_pull
        else contextlib.nullcontext()
    )
    pulled, skipped, pending, conflicts = [], [], [], []
    evidence = {"manifest": manifest, "pulled": pulled, "skipped": skipped, "local_dir": str(base), "remote_path": remote_path}
    try:
        with pull_guard:
            for item in manifest.get("files", []):
                path = _safe_local_artifact_path(base, str(item["relpath"]), create_parents=False)
                if path.is_file() and path.stat().st_size == item["size"] and _sha256_file(path) == item["sha256"]:
                    skipped.append({"relpath": item["relpath"], "local_path": str(path), "reason": "hash-match"})
                elif path.exists() and (not overwrite or not path.is_file()):
                    conflicts.append(str(path))
                else:
                    pending.append((item, path))
            if conflicts:
                return _pull_destination_exists(endpoint, started, start, evidence, conflicts)
            if pending:
                with ArtifactStream(endpoint, "pull", len(pending), timeout_ms) as stream:
                    for item, path in pending:
                        _safe_local_artifact_path(base, str(item["relpath"]))
                        digest = stream.pull(item, path, overwrite=overwrite)
                        pulled.append({"relpath": item["relpath"], "local_path": str(path), "sha256": digest, "size": item["size"]})
    except FileExistsError as exc:
        return _pull_destination_exists(endpoint, started, start, evidence, [str(exc.filename or exc)])
    except TimeoutError:
        raise
    except (RemoteExecutionError, OSError, ValueError) as exc:
        return _transfer_failure(endpoint, "remote.artifact_pull", started, start, exc, evidence)
    # remote_artifact_manifest already wrote a uniquely named manifest under
    # the endpoint state. Do not place tool metadata in the user's local_dir:
    # a remote tree can itself contain a file named manifest.json.
    manifest_path = manifest_payload["result"].get("refs", {}).get("local_manifest")
    result = make_result(tool="remote.artifact_pull", target=endpoint.to_result_target(), outcome="success",
                         status="ok", summary=f"Pulled {len(pulled)} files from {remote_path}.",
                         started_at=started, duration_ms=_duration_ms(start),
                         refs={"local_manifest": str(manifest_path)} if manifest_path else {}, artifacts=[evidence])
    return {"text": f"RemoteArtifactPull completed\nlocal_dir: {base}\npulled: {len(pulled)}\nskipped: {len(skipped)}\n", "result": result}


@observed_tool("remote.artifact_push")
@serialize_mutation
def remote_artifact_push(endpoint: Endpoint, *, local_path: str, remote_path: str,
                         timeout_ms: int | None = None) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    pushed = []
    evidence = {"pushed": pushed}
    stream = None
    try:
        remote_base = join_under_root(endpoint.root, endpoint.effective_cwd, remote_path)
        manifest = _local_manifest(Path(local_path))
        evidence.update(manifest=manifest, remote_path=remote_base)
        files = manifest["files"]
        if files:
            with ArtifactStream(endpoint, "push", len(files), timeout_ms) as stream:
                for item in files:
                    relpath = item["relpath"]
                    remote_file = remote_base if relpath == "." else str(PurePosixPath(remote_base) / relpath)
                    remote_file = join_under_root(endpoint.root, endpoint.effective_cwd, remote_file)
                    digest = stream.push({**item, "path": remote_file}, Path(item["path"]))
                    pushed.append({"relpath": relpath, "local_path": item["path"], "remote_path": remote_file,
                                   "sha256": digest, "size": item["size"]})
    except TimeoutError:
        raise
    except (RemoteExecutionError, OSError, ValueError, PathPolicyError) as exc:
        if stream is not None and stream.commit_possible and not getattr(exc, "submission_state", None):
            uncertain = RemoteExecutionError(str(exc), submission_state="uncertain")
            uncertain.__cause__ = exc
            exc = uncertain
        payload = _transfer_failure(endpoint, "remote.artifact_push", started, start, exc, evidence)
        if isinstance(exc, PathPolicyError):
            payload["result"].update(outcome="blocked", status="path_outside_root")
        elif isinstance(exc, FileNotFoundError):
            payload["result"].update(outcome="needs_input", status="local_path_not_found")
        elif "symlink" in str(exc):
            payload["result"].update(outcome="blocked", status="symlink_not_allowed")
        return payload
    result = make_result(tool="remote.artifact_push", target=endpoint.to_result_target(), outcome="success",
                         status="ok", summary=f"Pushed {len(pushed)} files to {remote_base}.",
                         started_at=started, duration_ms=_duration_ms(start), artifacts=[evidence])
    return {"text": f"RemoteArtifactPush completed\nremote_path: {remote_base}\npushed: {len(pushed)}\n", "result": result}
