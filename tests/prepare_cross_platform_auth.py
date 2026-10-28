"""Prepare independent installed consumer runtimes before real SSH tests.

[Role] CI setup for the same component wheels in two plugin-like virtual environments.
[Invariants] Acceptance test bodies never install packages or borrow source component imports.
[Timeouts & Locks] Installation is bounded at three minutes per environment.
[Side-Effect Policy] Creates only the explicitly requested runtime directory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    wheels = sorted(str(path.resolve()) for path in args.wheelhouse.glob("*.whl"))
    for component in ("remote_dev-", "mindie_coordinator-", "mindie_diagnostics-", "npu_top-"):
        if not any(Path(path).name.startswith(component) for path in wheels):
            raise RuntimeError("missing acceptance wheel: " + component)
    pythons = []
    for name in ("coordinator plugin", "kimi monitor plugin"):
        location = root / name
        subprocess.run([sys.executable, "-m", "venv", str(location)], check=True, timeout=60)
        python = location / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "pytest", "jsonschema", "psutil", "setuptools-scm", "mcp==1.30.0"],
                       check=True, timeout=180)
        # The cohort is built together; direct Git dependency metadata must not
        # replace these exact wheels with already published component versions.
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", *wheels],
                       check=True, timeout=90)
        pythons.append(str(python))
    (root / "pythons.json").write_text(json.dumps(pythons), encoding="utf-8")


if __name__ == "__main__":
    main()
