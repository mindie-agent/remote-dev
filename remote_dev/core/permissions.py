"""
[Role] Static command-permission classifiers for secret-bearing argv and raw remote transports (`permissions.py`).
[Invariants]
- `contains_secret_in_argv` matches known password/token forms; matching is advisory and never redacts by mutating the command.
- `contains_raw_remote_transport` detects `ssh`/`scp`/`sftp`/`rsync` at command boundaries, not variable names such as `$ssh`.
[Timeouts & Locks]
- Pure regular-expression matching; no locks, deadlines, or I/O.
[Side-Effect Policy]
- Read-only string inspection; no command execution or environment mutation.
"""
from __future__ import annotations

import re

SECRET_ARG_RE = re.compile(r"(?i)(sshpass|expect\b|--password(?:=|\s+)\S+|password=\S+|token=\S+|api[_-]?key=\S+)")
# Start-of-string, whitespace, or a shell operator / substitution so
# `;ssh`, `|ssh`, `&&ssh`, `$(ssh` and `(ssh` are visible. `$ssh` (a
# variable name) is intentionally not matched.
RAW_REMOTE_RE = re.compile(r"(?:^|[\s;|&()`]|\$\()(ssh|scp|sftp|rsync)\b")


def contains_secret_in_argv(command: str) -> bool:
    return bool(SECRET_ARG_RE.search(command))


def contains_raw_remote_transport(command: str) -> bool:
    return bool(RAW_REMOTE_RE.search(command))
