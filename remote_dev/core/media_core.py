"""
[Role] Shared local media limits, subprocess/error helpers (`media_core.py`).
[Invariants] Bytes-in functions; every subprocess/output is size- and time-bounded; temp cleanup exact.
[Timeouts & Locks] 120s subprocess cap; no locks.
[Side-Effect Policy] Local temp files only, removed in finally.
"""
from __future__ import annotations

import importlib
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

# Bounds keep a single media read from becoming an unbounded decode.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_TOTAL_BYTES = 32 * 1024 * 1024
MAX_NOTEBOOK_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024
MAX_VIDEO_THUMB_BYTES = 16 * 1024 * 1024

_SUBPROCESS_TIMEOUT_SECONDS = 120
_VIDEO_THUMB_TIMEOUT_SECONDS = 30
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}
_SUPPORTED_NOTEBOOK_CELL_TYPES = {"code", "markdown", "raw"}


class MediaOpError(RuntimeError):
    """A local renderer or input validation failed; converted to ``failed`` results."""


def _failed(error: object) -> dict[str, Any]:
    return {"status": "failed", "error": str(error)}


def _as_bytes(data: object, *, label: str) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise MediaOpError(f"{label} data must be bytes")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _module_available(name: str) -> bool:
    """Return whether an optional import exists without importing it eagerly."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _run(argv: list[str], *, timeout: int = _SUBPROCESS_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[bytes]:
    """Run one local renderer without a shell and normalize failure into MediaOpError."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaOpError(f"renderer executable is unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaOpError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        raise MediaOpError(
            f"{Path(argv[0]).name} exited {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return completed


def _cleanup_temp(path: Path | None) -> None:
    """Remove exactly the directory returned by ``mkdtemp``; never a prefix/glob."""
    if path is not None:
        shutil.rmtree(str(path), ignore_errors=True)


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    """Read a renderer output only after its on-disk size is known to be bounded."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaOpError(f"{label} is missing: {exc}") from exc
    if size > limit:
        raise MediaOpError(f"{label} is {size} bytes, over the {limit}-byte bound")
    data = path.read_bytes()
    if len(data) > limit:
        raise MediaOpError(f"{label} is {len(data)} bytes, over the {limit}-byte bound")
    return data


# ---------------------------------------------------------------------------
# Image format sniffing, dimension probing, and validation
# ---------------------------------------------------------------------------


