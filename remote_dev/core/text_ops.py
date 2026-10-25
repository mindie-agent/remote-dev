"""
[Role] Bounded UTF-8 text fetch for native notebook/attachment renderers (`text_ops.py`).
[Invariants]
- Resolves under the endpoint root/cwd, refuses symlinks, and reads at most max_bytes.
[Timeouts & Locks]
- Single remote Python call; forwards the caller timeout.
[Side-Effect Policy]
- Read-only.
"""
from __future__ import annotations

import time
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from .ssh_transport import run_remote_python
from remote_dev.result import make_result, utc_now_iso

REMOTE_TEXT_PY = r'''
import json
import pathlib
import sys
request = json.loads(sys.stdin.read())
path = pathlib.Path(request["path"])
limit = int(request["max_bytes"])
if path.is_symlink():
    print(json.dumps({"status": "symlink_not_allowed", "error": "refusing to read a symlink"})); raise SystemExit(0)
if not path.is_file():
    print(json.dumps({"status": "not_file", "error": "remote path is not a regular file"})); raise SystemExit(0)
size = path.stat().st_size
if size > limit:
    print(json.dumps({"status": "too_large", "error": f"file is {size} bytes, over the {limit}-byte bound", "size": size})); raise SystemExit(0)
data = path.read_bytes()
if b"\x00" in data[:8192]:
    print(json.dumps({"status": "binary_file", "error": "file is not UTF-8 text", "size": size})); raise SystemExit(0)
print(json.dumps({"status": "ok", "size": size, "text": data.decode("utf-8", errors="replace")}))
'''


def remote_read_utf8_text(
    endpoint: Endpoint,
    *,
    file_path: str,
    max_bytes: int = 1_500_000,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        result = make_result(
            tool="remote.read_text",
            target=endpoint.to_result_target(),
            outcome="blocked",
            status="path_outside_root",
            summary=f"Remote text read blocked for {file_path}.",
            preview={"stderr": str(exc)},
            extra={"error": str(exc)},
        )
        return {"text": result["summary"] + "\n" + str(exc) + "\n", "result": result}
    data = run_remote_python(
        endpoint, REMOTE_TEXT_PY, {"path": path, "max_bytes": int(max_bytes)}, timeout_ms=timeout_ms
    )
    status = str(data.get("status") or "failed")
    if status != "ok":
        result = make_result(
            tool="remote.read_text",
            target=endpoint.to_result_target(),
            outcome="failed",
            status=status,
            summary=f"Remote text read {status} for {path}.",
            duration_ms=int(round((time.monotonic() - start) * 1000)),
            preview={"stderr": str(data.get("error") or status)},
            extra={"error": data.get("error"), "path": path},
        )
        return {"text": result["summary"] + "\n" + str(data.get("error") or "") + "\n", "result": result}
    result = make_result(
        tool="remote.read_text",
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=f"Remote text read {path} ({int(data['size'])} bytes).",
        started_at=started,
        duration_ms=int(round((time.monotonic() - start) * 1000)),
        preview={"content": str(data["text"])[:4000]},
        extra={"path": path, "size": int(data["size"])},
    )
    return {"text": result["summary"] + "\n", "result": result, "raw_text": str(data["text"])}
