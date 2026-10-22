"""
[Role] Endpoint-scoped read-ledger facade for SHA-256 optimistic-concurrency records (`read_ledger.py`).
[Invariants]
- Paths and records are always endpoint- and optional-client-context-scoped; a read never falls back to a global ledger.
- Lookups return `None` for an absent record rather than synthesizing freshness.
[Timeouts & Locks]
- Delegates persistence, atomic replacement, and `record_lock` to `state_store`; no additional locks are held here.
[Side-Effect Policy]
- `record_read` writes one endpoint-scoped ledger record; `ledger_path`/`load_read` are read-only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .endpoint import Endpoint
from .state_store import load_read_ledger, read_ledger_path, write_read_ledger


def ledger_path(endpoint: Endpoint, file_path: str, client_context_id: str | None = None) -> Path:
    return read_ledger_path(endpoint, file_path, client_context_id)


def record_read(endpoint: Endpoint, file_info: dict[str, Any], client_context_id: str | None = None) -> Path:
    return write_read_ledger(endpoint, file_info, client_context_id)


def load_read(endpoint: Endpoint, file_path: str, client_context_id: str | None = None) -> dict[str, Any] | None:
    return load_read_ledger(endpoint, file_path, client_context_id)
