"""
[Role] Request cancellation propagation shared by protocol adapters and package operations (`cancellation.py`).
[Invariants]
- `request_context` restores the previous contextvar even when the body raises; cancellation state is never inferred or fabricated.
- `current_event` returns the active event or `None`; it never creates one implicitly.
[Timeouts & Locks]
- Contextvar operations are in-memory and lock-free; no deadlines are imposed here.
[Side-Effect Policy]
- Pure in-process state: no files, network, subprocesses, or external signals.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading

_event = contextvars.ContextVar("remote_dev_cancellation", default=None)


def current_event() -> threading.Event | None:
    return _event.get()


@contextlib.contextmanager
def request_context(event: threading.Event):
    token = _event.set(event)
    try:
        yield
    finally:
        _event.reset(token)
