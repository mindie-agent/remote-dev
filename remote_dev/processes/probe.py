"""Read-only source and interpreter facts without importing installed workloads.

[Role] Inspect endpoint Git context and optional distribution metadata.
[Invariants] Metadata paths are observations, not proof of loaded native code.
No framework import occurs unless explicitly requested through modules.
[Timeouts & Locks] Git subprocesses share the caller's probe budget; no locks.
[Side-Effect Policy] Read-only; never clones, installs, builds or rewrites sources.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import time
from urllib.parse import urlsplit
from urllib.request import url2pathname


def _normalize_git_path(path: str) -> str:
    if os.name == "nt":
        if path[:8].casefold() == "\\\\?\\unc\\" and len(path) > 8 and path[8] not in ("\\", "/") and len(path) - 6 < 260:
            return "\\" + path[7:]
        if (
            path.startswith("\\\\?\\")
            and len(path) >= 7
            and path[4].isascii()
            and path[4].isalpha()
            and path[5] == ":"
            and path[6] in ("\\", "/")
            and len(path) - 4 < 260
        ):
            return path[4:]
    return path


def git_fact(path: str, *args: str, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("probe Git budget exhausted")
    try:
        proc = subprocess.run(
            ["git", "-C", _normalize_git_path(path), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=min(3.0, remaining), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except TimeoutError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": None, "error": repr(exc)}
    return {"returncode": proc.returncode, "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:]}


def distribution_info(name: str) -> dict:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return {"status": "not_installed"}
    try:
        raw = dist.read_text("direct_url.json")
        direct = json.loads(raw) if raw is not None else None
        info = {"status": "ok", "name": dist.metadata["Name"],
                "version": dist.version, "installation_root": str(dist.locate_file("")),
                "editable": None, "source_path": None}
        if raw is not None:
            if not isinstance(direct, dict) or not isinstance(direct.get("url"), str):
                raise ValueError("direct_url.json must contain a URL string")
            directory = direct.get("dir_info", {})
            if not isinstance(directory, dict):
                raise ValueError("direct_url.json dir_info must be an object")
            editable = directory.get("editable", False)
            if not isinstance(editable, bool):
                raise ValueError("direct_url.json editable must be a boolean")
            info["editable"] = editable
            url = urlsplit(direct["url"])
            if url.scheme == "file" and url.netloc in ("", "localhost"):
                path = Path(url2pathname(url.path)).resolve()
                info.update(source_path=str(path), source_exists=path.is_dir())
            elif editable:
                raise ValueError("editable source URL is not a local file URL")
        return info
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "error", "error": repr(exc)}


def module_info(name: str) -> dict:
    try:
        module = importlib.import_module(name)
        return {"available": True, "version": str(getattr(module, "__version__", "unknown")),
                "file": str(getattr(module, "__file__", ""))}
    except BaseException as exc:
        return {"available": False, "error": repr(exc)}


def probe(payload: dict) -> dict:
    root, cwd = payload["root"], payload["cwd"]
    # Reserve transport cleanup time while sharing one budget across Git calls.
    budget = payload.get("timeout_ms", 30000) / 1000
    deadline = time.monotonic() + budget * 0.8
    metadata_only = payload.get("metadata_only", False)
    root_git = None if metadata_only else git_fact(root, "rev-parse", "--show-toplevel", deadline=deadline)
    root_head = None if metadata_only else git_fact(root, "rev-parse", "HEAD", deadline=deadline)
    packages = {name: distribution_info(name) for name in payload.get("distributions", [])}
    summary = {
        "hostname": platform.node(), "python": sys.version.split()[0],
        "python_executable": sys.executable, "python_prefix": sys.prefix,
        "python_abi": sysconfig.get_config_var("SOABI"), "python_base_prefix": sys.base_prefix,
        "isolated_python": bool(sys.flags.isolated), "cwd": os.getcwd(),
        "root_exists": Path(root).exists(), "git_root": root_git, "root_head": root_head,
        "cwd_git_root": (root_git if cwd == root or metadata_only else
                         git_fact(cwd, "rev-parse", "--show-toplevel", deadline=deadline)),
        "distributions": packages,
        "modules": {name: module_info(name) for name in payload.get("modules", [])},
    }
    return {"status": "failed" if any(row["status"] == "error" for row in packages.values()) else "ok",
            "summary": summary}


if __name__ == "__main__":
    print(json.dumps(probe(json.loads(sys.argv[1])), sort_keys=True))
