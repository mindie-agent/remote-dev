"""[Role] Carry the last authentication-maintenance fact through one tool call.
[Invariants] The context contains only public facts, never credentials or history.
[Timeouts & Locks] In-memory ContextVar operations; no waits or locks.
[Side-Effect Policy] Changes only the active call's diagnostic context.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_event: ContextVar[dict | None] = ContextVar("remote_dev_auth_maintenance", default=None)


def begin() -> Token:
    return _event.set(None)


def record(value: dict) -> None:
    _event.set(value)


def finish(token: Token) -> dict | None:
    value = _event.get()
    _event.reset(token)
    return value
