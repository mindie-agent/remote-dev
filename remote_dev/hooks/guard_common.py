"""
[Role] Client hook payload parser and permissive guard decision adapter (`guard_common.py`).
[Invariants]
- Never blocks raw SSH or remote MCP tool calls at the client hook boundary; path containment and concurrency guards are enforced authoritatively by the remote worker and `file_ops.py`.
[Timeouts & Locks]
- Synchronous stdin payload parsing with zero locks or subprocesses.
[Side-Effect Policy]
- Pure stdin reader and JSON decision formatter; mutates no files or state.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class GuardDecision:
    action: str
    reason: str | None = None
    additional_context: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "deny"


def read_hook_payload() -> dict[str, Any]:
    text = sys.stdin.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    return data if isinstance(data, dict) else {"value": data}


def inspect_remote_tool_call(tool_name: str, tool_input: dict[str, Any]) -> GuardDecision:
    return GuardDecision("allow")


def inspect_command(command: str) -> GuardDecision:
    return GuardDecision("allow")


def inspect_payload(payload: dict[str, Any]) -> GuardDecision:
    return GuardDecision("allow")


def codex_response(decision: GuardDecision) -> dict[str, Any]:
    if decision.blocked:
        return {"decision": "deny", "reason": decision.reason or "blocked by remote-dev guard"}
    if decision.additional_context:
        return {"decision": "allow", "additionalContext": decision.additional_context}
    return {"decision": "allow"}

