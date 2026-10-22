#!/usr/bin/env python3
"""
[Role] Claude Code hook adapter that blocks raw remote transports (`claude_remote_guard.py`).
[Invariants]
- A blocked decision exits `2` with the reason on stderr; allowed context is written to stdout exactly once.
- Payload interpretation is delegated to `guard_common`; no tool-specific policy is duplicated here.
[Timeouts & Locks]
- Reads one hook payload from stdin synchronously; no locks or network calls.
[Side-Effect Policy]
- Process-local stdin/stdout/stderr only; never edits files or invokes remote commands.
"""
from __future__ import annotations

import sys

from remote_dev.hooks.guard_common import inspect_payload, read_hook_payload


def main() -> int:
    decision = inspect_payload(read_hook_payload())
    if decision.blocked:
        sys.stderr.write((decision.reason or "blocked by remote-dev guard") + "\n")
        return 2
    if decision.additional_context:
        sys.stdout.write(decision.additional_context + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
