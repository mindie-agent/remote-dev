"""
[Role] Bounded native image reads over the remote file transport (`image_ops.py`).
[Invariants]
- Resolves the path under the endpoint root/cwd and refuses symlinks that escape it.
- Reads at most ``max_bytes`` on the remote host; never buffers an unbounded file.
- Accepts only the four image formats the native tools advertise: PNG, JPEG, GIF, WebP.
[Timeouts & Locks]
- Forwards the caller timeout to the single remote Python call.
[Side-Effect Policy]
- Read-only; no remote mutation and no artifact state writes.
"""
from __future__ import annotations

import time
from typing import Any

from .endpoint import Endpoint
from .errors import PathPolicyError
from .path_policy import join_under_root
from .ssh_transport import run_remote_python
from remote_dev.result import make_result, utc_now_iso

REMOTE_IMAGE_PY = r'''
import base64
import io
import json
import pathlib
import sys

request = json.loads(sys.stdin.read())
path = pathlib.Path(request["path"])
limit = int(request["max_bytes"])
region = request.get("region")
max_edge = request.get("max_edge")
if path.is_symlink():
    print(json.dumps({"status": "symlink_not_allowed", "error": "refusing to read a symlink as an image"})); raise SystemExit(0)
if not path.is_file():
    print(json.dumps({"status": "not_file", "error": "remote image path is not a regular file"})); raise SystemExit(0)
size = path.stat().st_size
if size > limit:
    print(json.dumps({"status": "too_large", "error": f"image is {size} bytes, over the {limit}-byte bound", "size": size})); raise SystemExit(0)
data = path.read_bytes()
if data.startswith(b"\x89PNG\r\n\x1a\n"):
    mime = "image/png"
elif data.startswith(b"\xff\xd8\xff"):
    mime = "image/jpeg"
elif data[:6] in (b"GIF87a", b"GIF89a"):
    mime = "image/gif"
elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
    mime = "image/webp"
else:
    print(json.dumps({"status": "unsupported_format", "error": "not a PNG, JPEG, GIF, or WebP image", "size": size})); raise SystemExit(0)
width = height = None
original_width = original_height = None
resized = False
cropped = False
if region is not None or max_edge is not None:
    try:
        from PIL import Image
    except ImportError:
        if region is not None:
            print(json.dumps({"status": "region_unsupported", "error": "remote image region crops require Pillow on the target host; Pillow is unavailable under the isolated worker"}))
            raise SystemExit(0)
        print(json.dumps({"status": "ok", "mime_type": mime, "size": size,
                          "data": base64.b64encode(data).decode("ascii"),
                          "warning": "Pillow unavailable on the remote host; image was not downscaled"}))
        raise SystemExit(0)
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        original_width, original_height = image.size
        if region is not None:
            box = (int(region["x"]), int(region["y"]),
                   int(region["x"]) + int(region["width"]),
                   int(region["y"]) + int(region["height"]))
            if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height or box[2] <= box[0] or box[3] <= box[1]:
                print(json.dumps({"status": "invalid_region", "error": f"region {region!r} is outside the {image.width}x{image.height} image"})); raise SystemExit(0)
            image = image.crop(box)
            cropped = True
        if max_edge is not None and max(image.size) > int(max_edge):
            image.thumbnail((int(max_edge), int(max_edge)), Image.Resampling.LANCZOS)
            resized = True
        width, height = image.size
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        out = io.BytesIO()
        if mime == "image/jpeg" and image.mode == "RGBA":
            image = image.convert("RGB")
        if mime == "image/png":
            image.save(out, format="PNG", optimize=True)
        elif mime == "image/gif":
            image.save(out, format="PNG")
            mime = "image/png"
        elif mime == "image/webp":
            image.save(out, format="WEBP", quality=85)
        else:
            image.save(out, format="JPEG", quality=85)
        encoded = out.getvalue()
        if len(encoded) > limit:
            print(json.dumps({"status": "too_large", "error": f"processed image is {len(encoded)} bytes, over the {limit}-byte bound"})); raise SystemExit(0)
        data = encoded
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"status": "decode_failed", "error": f"failed to crop/downscale image: {exc}"})); raise SystemExit(0)
print(json.dumps({"status": "ok", "mime_type": mime, "size": len(data),
                  "data": base64.b64encode(data).decode("ascii"),
                  "width": width, "height": height,
                  "original_width": original_width, "original_height": original_height,
                  "resized": resized, "cropped": cropped}))
'''


def remote_read_image(
    endpoint: Endpoint,
    *,
    file_path: str,
    max_bytes: int = 8 * 1024 * 1024,
    region: dict[str, Any] | None = None,
    max_edge: int | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    try:
        path = join_under_root(endpoint.root, endpoint.effective_cwd, file_path)
    except PathPolicyError as exc:
        result = make_result(
            tool="remote.read_image",
            target=endpoint.to_result_target(),
            outcome="blocked",
            status="path_outside_root",
            summary=f"Remote image read blocked for {file_path}.",
            preview={"stderr": str(exc)},
            extra={"error": str(exc)},
        )
        return {"text": result["summary"] + "\n" + str(exc) + "\n", "result": result}
    data = run_remote_python(
        endpoint,
        REMOTE_IMAGE_PY,
        {"path": path, "max_bytes": int(max_bytes), "region": region, "max_edge": max_edge},
        timeout_ms=timeout_ms,
    )
    status = str(data.get("status") or "failed")
    if status != "ok":
        result = make_result(
            tool="remote.read_image",
            target=endpoint.to_result_target(),
            outcome="failed" if status not in {"path_outside_root"} else "blocked",
            status=status,
            summary=f"Remote image read {status} for {path}.",
            duration_ms=int(round((time.monotonic() - start) * 1000)),
            preview={"stderr": str(data.get("error") or status)},
            extra={"error": data.get("error"), "path": path},
        )
        return {"text": result["summary"] + "\n" + str(data.get("error") or "") + "\n", "result": result}
    mime = str(data["mime_type"])
    size = int(data["size"])
    result = make_result(
        tool="remote.read_image",
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=f"Remote image read {path} ({mime}, {size} bytes).",
        started_at=started,
        duration_ms=int(round((time.monotonic() - start) * 1000)),
        preview={"content": f"image: {path} ({mime}, {size} bytes)"},
        extra={"path": path, "mime_type": mime, "size": size,
               "width": data.get("width"), "height": data.get("height"),
               "original_width": data.get("original_width"),
               "original_height": data.get("original_height"),
               "resized": bool(data.get("resized")), "cropped": bool(data.get("cropped")),
               **({"warning": data["warning"]} if data.get("warning") else {})},
    )
    return {
        "text": result["summary"] + "\n",
        "result": result,
        "content": [{"type": "image", "data": data["data"], "mimeType": mime}],
    }
