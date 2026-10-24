"""
[Role] Bounded remote file pull for local media/conversion (`bytes_ops.py`).
[Invariants]
- Uses the existing streaming artifact transport, never a whole-file SSH read.
- Refuses a manifest file larger than ``max_bytes`` before pulling.
- Removes only the exact temporary directory it created.
[Timeouts & Locks]
- Forwards the caller deadline to manifest and stream transfer.
[Side-Effect Policy]
- Read-only on the remote host; deletes only its own local temp directory.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .artifact_ops import remote_artifact_manifest, remote_artifact_pull
from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root


def _mime(path: str, data: bytes) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf" or data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video/mp4" if suffix in {".mp4", ".m4v"} else "video/webm"
    if suffix == ".ipynb":
        return "application/x-ipynb+json"
    return "application/octet-stream"


def remote_read_bytes(
    endpoint: Endpoint,
    *,
    file_path: str,
    max_bytes: int = 32 * 1024 * 1024,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    try:
        resolved = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        return {"status": "path_outside_root", "error": str(exc)}
    manifest_payload = remote_artifact_manifest(endpoint, remote_path=file_path, timeout_ms=timeout_ms)
    manifest = manifest_payload.get("result", {}).get("manifest") or {}
    if manifest_payload.get("result", {}).get("outcome") != "success" or manifest.get("status") != "ok":
        return {
            "status": manifest.get("status", "manifest_failed"),
            "error": manifest_payload.get("text") or "remote manifest failed",
        }
    files = manifest.get("files") or []
    if len(files) != 1:
        return {"status": "not_a_file", "error": f"media pull requires exactly one file, got {len(files)}"}
    item = files[0]
    size = int(item.get("size") or 0)
    if size > max_bytes:
        return {"status": "too_large", "error": f"file is {size} bytes, over the {max_bytes}-byte bound", "size": size}
    temp = Path(tempfile.mkdtemp(prefix="remote-dev-media-"))
    try:
        pulled = remote_artifact_pull(
            endpoint, remote_path=file_path, local_dir=str(temp), overwrite=True, timeout_ms=timeout_ms
        )
        if pulled.get("result", {}).get("outcome") != "success":
            return {"status": "pull_failed", "error": pulled.get("text") or "artifact pull failed"}
        rel = str(item.get("relpath") or ".")
        local = temp / ("artifact" if rel == "." else rel)
        data = local.read_bytes()
        if len(data) > max_bytes:
            return {"status": "too_large", "error": f"pulled {len(data)} bytes, over the {max_bytes}-byte bound"}
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.get("sha256"):
            return {"status": "hash_mismatch", "error": "pulled bytes do not match the remote manifest"}
        return {
            "status": "ok",
            "data": data,
            "size": len(data),
            "sha256": digest,
            "path": str(resolved),
            "mime_type": _mime(str(resolved), data),
        }
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(temp, ignore_errors=True)
