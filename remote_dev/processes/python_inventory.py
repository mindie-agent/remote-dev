"""Inspect bounded interpreter candidates and select from observed metadata.

[Role] Discover business Python environments independently of a control Python.
[Invariants] An explicit interpreter never falls back. Different virtualenv
prefixes remain distinct even when their executables resolve to the same file.
[Timeouts & Locks] Candidate subprocesses share one deadline; no locks.
[Side-Effect Policy] Metadata only; no package installs or framework imports.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def discover(payload):
    explicit = payload.get("python_executable")
    if explicit:
        if not Path(explicit).is_absolute():
            raise ValueError("explicit business Python must be an absolute path")
        return [explicit], [explicit]
    preferred = [os.environ.get("MINDIE_PYTHON")]
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(key)
        if prefix:
            preferred.append(str(Path(prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    candidates = [*preferred, *payload.get("python_candidates", []), os.environ.get("PYTHON")]
    cwd = Path(payload["cwd"])
    candidates.extend(str(cwd / folder / "bin/python") for folder in (".venv", "venv"))
    for directory in os.get_exec_path():
        for name in (("python.exe", "python3.exe") if os.name == "nt" else ("python3", "python")):
            path = str(Path(directory) / name)
            if Path(path).is_file() and os.access(path, os.X_OK):
                candidates.append(path)
    candidates.extend(sorted(glob.glob("/usr/local/python*/bin/python3")))
    candidates.append(sys.executable)
    paths = list(dict.fromkeys(path for path in candidates if path and Path(path).is_absolute()))
    # Explicit candidates and active environments must be observed even when
    # absent; only optional conventional discovery paths are filtered here.
    wanted = set(preferred) | set(payload.get("python_candidates", []))
    paths = [path for path in paths if path in wanted or (Path(path).is_file() and os.access(path, os.X_OK))]
    return paths, [path for path in preferred if path]


def inventory(payload):
    paths, preferred = discover(payload)
    deadline = time.monotonic() + payload.get("timeout_ms", 30000) / 1000 * 0.8
    rows, environments = [], set()
    required = payload.get("required_distributions", [])
    names = list(dict.fromkeys([*payload.get("distributions", []), *required]))
    pending = []
    for index, path in enumerate(paths[:16]):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pending = paths[index:16]
            break
        request = {"root": payload["root"], "cwd": payload["cwd"],
                   "distributions": names, "metadata_only": True}
        try:
            proc = subprocess.run(
                [path, "-I", "-c", payload["probe_source"], json.dumps(request)],
                capture_output=True, text=True, timeout=min(3.0, remaining), check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode:
                raise ValueError(f"interpreter exited {proc.returncode}: {proc.stderr[-1000:]}")
            result = json.loads(proc.stdout)
            summary = result["summary"]
            missing = [name for name in required if summary["distributions"][name]["status"] != "ok"]
            identity = (summary["python_prefix"], summary["python_abi"])
            if identity in environments:
                continue
            environments.add(identity)
            rows.append({"requested_python": path, **summary,
                         "status": "eligible" if not missing and result["status"] == "ok" else "ineligible",
                         "missing_distributions": missing})
        except TimeoutError:
            raise
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            rows.append({"requested_python": path, "status": "failed", "error": repr(exc)})
    eligible = [row for row in rows if row["status"] == "eligible"]
    bound = [row for row in eligible if row["requested_python"] in preferred]
    unprobed = pending + paths[16:]
    choices = bound[:1] if bound else ([] if unprobed else eligible)
    selected = choices[0] if len(choices) == 1 else None
    status = "selected" if selected else ("ambiguous" if eligible else "unavailable")
    if unprobed and not selected:
        status = "incomplete"
    return {"status": "ok" if selected else "failed",
            **({"error": "interpreter selection " + status + "; inspect candidates or specify python_executable"} if not selected else {}),
            "summary": {
        **({key: value for key, value in selected.items()
            if key not in {"status", "missing_distributions", "requested_python"}} if selected else {}),
        "interpreter_selection": status, "interpreters": rows,
        "unprobed_candidates": unprobed, "validation_scope": "distribution_metadata",
    }}


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2]:
        request["python_executable"] = sys.argv[2]
    result = inventory(request)
    if request.get("output") == "path":
        print("PYTHON_INVENTORY " + json.dumps(result), file=sys.stderr)
        if result["status"] == "ok":
            print(result["summary"]["python_executable"])
        raise SystemExit(0 if result["status"] == "ok" else 3)
    print(json.dumps(result))
