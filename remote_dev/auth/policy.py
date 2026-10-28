"""[Role] Preserve read-only authentication policy through nested SSH lookups.
[Invariants] Container inspection cannot install keys or alter access profiles.
[Timeouts & Locks] In-memory ContextVar scope; no locks or waits.
[Side-Effect Policy] Changes only the current call's permission to repair keys.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_read_only = ContextVar("remote_dev_auth_read_only", default=False)


def repair_allowed():
    return not _read_only.get()


@contextmanager
def read_only():
    token = _read_only.set(True)
    try:
        yield
    finally:
        _read_only.reset(token)
