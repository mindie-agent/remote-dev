from __future__ import annotations

from remote_dev.observability import observed_tool

import hashlib
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from .locking import serialize_mutation

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
if not target.exists() and not resolved.exists():
    print(json.dumps({"status": "needs_input", "error": "remote path does not exist", "remote_path": str(target)}))
    raise SystemExit(0)
if target.is_symlink():
    print(json.dumps({"status": "blocked", "error": "artifact symlinks are not allowed", "remote_path": str(target)}))
    raise SystemExit(0)

def sha256_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size

files = []
paths = [target] if target.is_file() else sorted(pathlib.Path(target).rglob("*"))
for path in paths:
    if path.is_symlink():
        print(json.dumps({"status": "blocked", "error": f"artifact symlink is not allowed: {path}"}))
        raise SystemExit(0)
    if not path.is_file():
        continue
    digest, size = sha256_file(path)
    st = path.stat()
    files.append({
        "relpath": "." if path == target else str(path.relative_to(target)),
        "path": str(path),
        "size": size,
        "sha256": digest,
        "mode": stat.S_IMODE(st.st_mode),
        "mtime_ns": st.st_mtime_ns,
    })
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
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_manifest(local_path: Path) -> dict[str, Any]:
    raw_path = local_path.expanduser()
    if raw_path.is_symlink():
        raise ValueError(f"local artifact symlinks are not allowed: {raw_path}")
    resolved = raw_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"local artifact path does not exist: {resolved}")
    files: list[dict[str, Any]] = []
    roots = [resolved] if resolved.is_file() else sorted(path for path in resolved.rglob("*") if path.is_file())
    for path in roots:
        if path.is_symlink():
            raise ValueError(f"local artifact symlinks are not allowed: {path}")
        stat = path.stat()
        files.append({
            "relpath": "." if path == resolved else path.relative_to(resolved).as_posix(),
            "path": str(path),
            "size": stat.st_size,
            "sha256": _sha256_file(path),
            "mtime_ns": stat.st_mtime_ns,
        })
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
        except OSError as exc:
            raise ValueError(f"artifact relpath is not a creatable path: {relpath}") from exc
    if candidate.is_symlink():
        raise ValueError(f"refusing to overwrite local symlink: {candidate}")
    return candidate


@observed_tool("remote.artifact_manifest")
@pinned_endpoint
def remote_artifact_manifest(endpoint: Endpoint, *, remote_path: str, timeout_ms: int = 120000) -> dict[str, Any]:
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
    if isinstance(data, dict) and data.get("status") == "ok":
        data["endpoint_id"] = endpoint.endpoint_id
        artifact_id = f"manifest-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        data["artifact_id"] = artifact_id
        manifest_path = ensure_endpoint_state(endpoint) / "artifacts" / artifact_id / "manifest.json"
        atomic_write_json(manifest_path, data)
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
    return {"text": f"RemoteArtifactManifest {status}: {path}\nfiles: {data.get('file_count', 0)}\n", "result": result}


def _transfer_failure(endpoint, tool, started, start, exc, evidence):
    message = str(exc)[-4000:]
    status = ("hash_mismatch" if "hash_mismatch" in message else "cancelled" if "cancelled" in message
              else "timeout" if "timed out" in message else "path_traversal" if isinstance(exc, ValueError) else "failed")
    outcome = "blocked" if status == "path_traversal" else status if status in {"cancelled", "timeout"} else "failed"
    result = make_result(tool=tool, target=endpoint.to_result_target(), outcome=outcome,
                         status=status, summary=f"Artifact transfer {status}.", started_at=started,
                         duration_ms=_duration_ms(start), preview={"stderr": message}, artifacts=[evidence],
                         extra={"error_details": error_details(exc), "expected_sha256": getattr(exc, "expected_sha256", None), "observed_sha256": getattr(exc, "observed_sha256", None)})
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
                         overwrite: bool = False, timeout_ms: int = 120000) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    manifest_payload = remote_artifact_manifest(endpoint, remote_path=remote_path, timeout_ms=timeout_ms)
    manifest = manifest_payload["result"].get("manifest", {})
    if manifest.get("status") != "ok":
        return manifest_payload
    base = Path(local_dir) if local_dir else ensure_endpoint_state(endpoint) / "artifacts" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    pulled, skipped, pending, conflicts = [], [], [], []
    evidence = {"manifest": manifest, "pulled": pulled, "skipped": skipped, "local_dir": str(base), "remote_path": remote_path}
    try:
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
                         timeout_ms: int = 120000) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    pushed = []
    evidence = {"pushed": pushed}
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
    except (RemoteExecutionError, OSError, ValueError, PathPolicyError) as exc:
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
