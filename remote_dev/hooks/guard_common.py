"""
[Role] Client hook payload parser and dangerous-command guard adapter (`guard_common.py`).
[Invariants]
- Intercepts unconfirmed destructive host/NPU commands (`npu_reset`, `reboot`, `shutdown`) via `command_policy.inspect_dangerous_command`.
- Never blocks normal SSH or remote MCP tool calls at the client hook boundary; path containment and concurrency guards are enforced authoritatively by the remote worker and `file_ops.py`.
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

from remote_dev.core.command_policy import inspect_dangerous_command


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
    if not isinstance(tool_input, dict):
        return GuardDecision("allow")
    confirm = bool(tool_input.get("confirm", False))
    env = tool_input.get("env") if isinstance(tool_input.get("env"), dict) else None
    for key in ("command", "cmd", "CommandLine", "command_line", "chars", "Input", "input", "stdin"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            # `apply_patch` uses `command` as a legacy alias for unified diff text, not a shell command.
            if key == "command" and "apply_patch" in (tool_name or "").lower():
                continue
            match = inspect_dangerous_command(val, confirm=confirm, env=env)
            if match is not None:
                return GuardDecision("deny", reason=match.reason)
    return GuardDecision("allow")


def inspect_command(command: str, *, confirm: bool = False, env: dict[str, str] | None = None) -> GuardDecision:
    match = inspect_dangerous_command(command, confirm=confirm, env=env)
    if match is not None:
        return GuardDecision("deny", reason=match.reason)
    return GuardDecision("allow")


def inspect_payload(payload: dict[str, Any]) -> GuardDecision:
    if not isinstance(payload, dict):
        return GuardDecision("allow")
    tool_name = str(payload.get("tool_name") or payload.get("name") or "")
    for key in ("tool_input", "arguments", "input"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            decision = inspect_remote_tool_call(tool_name, sub)
            if decision.blocked:
                return decision
    return inspect_remote_tool_call(tool_name, payload)


def codex_response(decision: GuardDecision) -> dict[str, Any]:
    if decision.blocked:
        return {"decision": "deny", "reason": decision.reason or "blocked by remote-dev guard"}
    if decision.additional_context:
        return {"decision": "allow", "additionalContext": decision.additional_context}
    return {"decision": "allow"}

