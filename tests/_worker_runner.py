"""Run a production worker source through a temp file, not argv.

The SSH transport executes workers with ``python -I -c <source>``.  A Windows
client has a much smaller command-line limit than the remote worker source;
local portability tests must exercise the worker, not WinError 206.  Keep the
same stdin/JSON contract and put the source in a temporary file so the real
worker still runs on every platform.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


def run_worker_script(
    source: str,
    payload: Mapping[str, Any],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    isolated: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="rd-worker-source-") as directory:
        path = Path(directory) / "worker.py"
        path.write_text(source, encoding="utf-8")
        argv = [sys.executable]
        if isolated:
            argv.append("-I")
        argv.append(str(path))
        return subprocess.run(
            argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            timeout=timeout,
        )
