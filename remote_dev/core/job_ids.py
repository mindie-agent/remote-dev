"""
[Role] Canonical job-id syntax shared by lifecycle, persistence, and retention modules (`job_ids.py`).
[Invariants]
- A job id is 3-96 characters from `A-Z a-z 0-9 _ . -`, starts alphanumeric, and is the only
  caller-supplied component used to build `<job_id>.json` paths; strings such as `../outside`
  are rejected before any path resolution or directory creation.
[Timeouts & Locks]
- Pure in-memory validation; no I/O, locks, or waiting.
[Side-Effect Policy]
- Read-only string validation; never writes records, logs, or state.
"""
from __future__ import annotations

import re

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")


def require_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value):
        raise ValueError("job id must be 3-96 chars from A-Z a-z 0-9 _ . -")
    return value


__all__ = ["JOB_ID_RE", "require_job_id"]
