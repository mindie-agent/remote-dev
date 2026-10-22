#!/usr/bin/env python3
"""
[Role] Codex hook adapter that emits structured remote-transport guard decisions (`codex_remote_guard.py`).
[Invariants]
- Always writes one JSON response; exits `2` only when the decision is blocked.
- Decision policy is delegated to `guard_common`; no duplicate per-harness rules.
[Timeouts & Locks]
- Reads one hook payload from stdin synchronously; no locks or network calls.
[Side-Effect Policy]
- Process-local stdin/stdout only; never edits files or invokes remote commands.
"""
from __future__ import annotations

import json
import sys

from remote_dev.hooks.guard_common import codex_response, inspect_payload, read_hook_payload


def main() -> int:
    decision = inspect_payload(read_hook_payload())
    sys.stdout.write(json.dumps(codex_response(decision), ensure_ascii=False) + "\n")
    return 2 if decision.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
