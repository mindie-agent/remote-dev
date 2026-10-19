"""Release microbenchmark for patch parsing and host argument translation.

[Role] Exercise real patch parser and MCP protocol normalization on public input.
[Invariants] Preserve deletion hunks, added content, and literal shell arguments.
[Timeouts & Locks] Fixed iteration count; no subprocess, network, or sleep.
[Side-Effect Policy] Write only the requested benchmark result JSON.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from remote_dev.core.patch_ops import parse_codex_patch
from remote_dev.mcp.schemas import normalize_arguments

ITERATIONS = 10000
PATCH = """*** Begin Patch
*** Update File: src/kernel.py
@@
 def forward(x):
-    debug = True
-    return x + 1
+    return x + 2
*** Add File: src/config.py
+BATCH_SIZE = 8
*** End Patch
"""


def protocol_roundtrip() -> None:
    operations = parse_codex_patch(PATCH)
    assert len(operations) == 2
    assert operations[0]["path"] == "src/kernel.py"
    assert operations[0]["hunks"] == [{
        "old": "def forward(x):\n    debug = True\n    return x + 1\n",
        "new": "def forward(x):\n    return x + 2\n",
    }]
    assert operations[1]["path"] == "src/config.py"
    assert operations[1]["content"] == "BATCH_SIZE = 8\n"
    shell = normalize_arguments(
        "remote.bash", {"cmd": "printf '%s' '$literal'", "workdir": "/workspace"}
    )
    assert shell["command"] == "printf '%s' '$literal'"
    assert shell["cwd"] == "/workspace"


def main() -> None:
    for _ in range(100):
        protocol_roundtrip()
    started = perf_counter_ns()
    for _ in range(ITERATIONS):
        protocol_roundtrip()
    elapsed = perf_counter_ns() - started
    result = {
        "status": "pass",
        "metrics": [{
            "name": "two_file_patch_parse_and_shell_normalization",
            "unit": "us/op",
            "value": elapsed / ITERATIONS / 1000,
            "iterations": ITERATIONS,
        }],
    }
    Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
