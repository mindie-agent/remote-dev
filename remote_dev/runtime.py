"""
[Role] Process-start package identity and loaded-vs-installed runtime status (`runtime.py`).
[Invariants]
- `installed_identity` reports `None` fields only when package metadata is genuinely unavailable; a metadata read failure is never a fake version.
- `runtime_status` marks `restart_required` only when both loaded and installed versions are comparable and identity fields differ.
[Timeouts & Locks]
- Pure in-memory importlib metadata reads; no locks, subprocesses, or network calls.
[Side-Effect Policy]
- Read-only: never writes package metadata, cache files, or process state.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path


def installed_identity(package: str) -> dict:
    try:
        dist = metadata.distribution(package)
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        return {"package": package, "version": dist.version,
                "commit": direct.get("vcs_info", {}).get("commit_id"),
                "location": str(Path(dist.locate_file("")).resolve())}
    except TimeoutError:
        raise
    except (metadata.PackageNotFoundError, ValueError, OSError):
        return {"package": package, "version": None, "commit": None, "location": None}


def process_identity(package: str) -> dict:
    return {**installed_identity(package), "pid": os.getpid(), "python": sys.executable}


def runtime_status(loaded: dict) -> dict:
    installed = installed_identity(loaded["package"])
    comparable = loaded.get("version") is not None and installed.get("version") is not None
    changed = comparable and any(loaded.get(key) != installed.get(key) for key in ("version", "commit", "location"))
    return {"loaded": dict(loaded), "installed": installed,
            "status": "restart_required" if changed else "current" if comparable else "unknown"}
