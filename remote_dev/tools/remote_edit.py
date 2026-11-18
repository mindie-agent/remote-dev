#!/usr/bin/env python3
"""[Role] Thin CLI entry point for `remote.edit`.
[Invariants] Delegates argument parsing, validation and dispatch to `remote_dev.cli`; no separate execution path.
[Timeouts & Locks] No local waits or locks; the shared CLI and core operation own deadlines.
[Side-Effect Policy] Performs only the canonical operation selected by the CLI.
"""
from __future__ import annotations

from remote_dev.cli import run_tool_main

if __name__ == "__main__":
    raise SystemExit(run_tool_main('edit'))
