from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from .endpoint import Endpoint, substrate_root
from .container_endpoint import pinned_endpoint
from .atomic import replace_file
from .path_policy import path_fingerprint
from remote_dev.result import dumps, new_invocation_id, utc_now_iso

LEDGER_SCOPE_ENV_VARS = ("CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_RUN_ID", "REMOTE_DEV_SESSION_ID")
LEDGER_SCOPE_PREFIX = "id-"
LEDGER_NO_CONTEXT_SCOPE = "default"
LEDGER_SCHEMA_VERSION = "remote-dev.read_ledger.v2"
LEDGER_SCOPE_ENCODING = "id-sha256"


def state_root() -> Path:
    """Local runtime state directory.

    Defaults to ``<cwd>/state`` (ignored by Git). ``REMOTE_DEV_STATE_DIR``
    relocates it so an embedding consumer can keep remote-dev state next to
    its own untracked state.
    """
    configured = os.environ.get("REMOTE_DEV_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return substrate_root() / "state"


@pinned_endpoint
def endpoint_state_dir(endpoint: Endpoint) -> Path:
    return state_root() / "endpoints" / endpoint.endpoint_id


@pinned_endpoint
def ensure_endpoint_state(endpoint: Endpoint) -> Path:
    base = endpoint_state_dir(endpoint)
    for name in ("context", "reads", "logs", "jobs", "artifacts", "patches"):
        (base / name).mkdir(parents=True, exist_ok=True)
    endpoint_path = base / "endpoint.json"
    if not endpoint_path.exists():
        atomic_write_json(
            endpoint_path,
            {
                "schema_version": "remote-dev.endpoint.v1",
                "endpoint_id": endpoint.endpoint_id,
                "host": endpoint.host,
                "port": endpoint.port,
                "user": endpoint.user,
                "root": endpoint.root,
                "cwd": endpoint.effective_cwd,
                "kind": endpoint.kind,
                "alias": endpoint.alias,
                **({"container": endpoint.container} if endpoint.container else {}),
                **({"container_selector": endpoint.container_selector} if endpoint.container_selector else {}),
                "created_at": utc_now_iso(),
            },
        )
    return base


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(dumps(data) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pinned_endpoint
def new_log_dir(endpoint: Endpoint, tool_kind: str, invocation_id: str | None = None) -> Path:
    base = ensure_endpoint_state(endpoint)
    token = invocation_id or new_invocation_id()
    path = base / "logs" / tool_kind / token
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_scope_digest(raw: str) -> str:
    """Full SHA-256 of an arbitrary client context id."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _effective_ledger_context_id(client_context_id: str | None = None) -> str | None:
    raw = str(client_context_id) if client_context_id else None
    if not raw:
        for name in LEDGER_SCOPE_ENV_VARS:
            value = os.environ.get(name)
            if value:
                return value
        return None
    return raw


def _encoded_ledger_scope(raw: str) -> str:
    return LEDGER_SCOPE_PREFIX + _ledger_scope_digest(raw)


def resolve_ledger_scope(client_context_id: str | None = None) -> str:
    """Return a single filesystem-safe directory name for this context."""
    raw = _effective_ledger_context_id(client_context_id)
    if not raw:
        return LEDGER_NO_CONTEXT_SCOPE
    return _encoded_ledger_scope(raw)


@pinned_endpoint
def read_ledger_path(endpoint: Endpoint, file_path: str, client_context_id: str | None = None) -> Path:
    scope = resolve_ledger_scope(client_context_id)
    return ensure_endpoint_state(endpoint) / "reads" / scope / f"{path_fingerprint(file_path)}.json"


@pinned_endpoint
def write_read_ledger(endpoint: Endpoint, file_info: dict[str, Any], client_context_id: str | None = None) -> Path:
    scope = resolve_ledger_scope(client_context_id)
    path = read_ledger_path(endpoint, str(file_info["path"]), client_context_id)
    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "ledger_scope_encoding": LEDGER_SCOPE_ENCODING,
        "endpoint_id": endpoint.endpoint_id,
        "ledger_scope": scope,
        "file_path": file_info["path"],
        "root": endpoint.root,
        "sha256": file_info["sha256"],
        "size": file_info["size"],
        "mtime_ns": file_info["mtime_ns"],
        "read_at": utc_now_iso(),
        "offset": file_info.get("offset"),
        "limit": file_info.get("limit"),
    }
    atomic_write_json(path, payload)
    return path


@pinned_endpoint
def load_read_ledger(endpoint: Endpoint, file_path: str, client_context_id: str | None = None) -> dict[str, Any] | None:
    path = read_ledger_path(endpoint, file_path, client_context_id)
    if not path.exists():
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None


class WriteLedgerGuard(NamedTuple):
    """Stale-write authorization for one (endpoint, file, context)."""

    ledger: dict[str, Any] | None
    read_required: bool
    scope: str


@pinned_endpoint
def load_write_ledger_guard(
    endpoint: Endpoint,
    file_path: str,
    client_context_id: str | None = None,
) -> WriteLedgerGuard:
    scope = resolve_ledger_scope(client_context_id)
    data = load_read_ledger(endpoint, file_path, client_context_id)
    return WriteLedgerGuard(ledger=data, read_required=False, scope=scope)


@pinned_endpoint
def job_record_path(endpoint: Endpoint, job_id: str) -> Path:
    return ensure_endpoint_state(endpoint) / "jobs" / f"{job_id}.json"


def _state_entries(directory: Path):
    # Path.glob suppresses directory-scan OSError on newer Python versions.
    # Only an absent state directory is empty; unreadable state is a failure.
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return []
    with entries:
        return sorted(entries, key=lambda entry: entry.name)


def _endpoint_directories():
    return [Path(entry.path) for entry in _state_entries(state_root() / "endpoints") if entry.is_dir()]


def find_job_record(job_id: str) -> tuple[Path, dict[str, Any]] | None:
    for directory in _endpoint_directories():
        path = directory / "jobs" / f"{job_id}.json"
        try:
            data = read_json(path)
        except FileNotFoundError:
            continue
        if not isinstance(data, dict):
            raise ValueError(f"invalid job record: {path}")
        return path, data
    return None


def list_endpoint_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in _endpoint_directories():
        path = directory / "endpoint.json"
        try:
            data = read_json(path)
        except FileNotFoundError:
            continue
        if not isinstance(data, dict):
            raise ValueError(f"invalid endpoint record: {path}")
        records.append({**data, "state_dir": str(path.parent)})
    return records


def latest_context_path(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "context" / "latest.json"


def jobs_dir(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "jobs"


def artifacts_dir(endpoint_id: str) -> Path:
    return state_root() / "endpoints" / endpoint_id / "artifacts"


def list_job_records(endpoint_id: str) -> list[dict[str, Any]]:
    directory = jobs_dir(endpoint_id)
    records: list[dict[str, Any]] = []
    for entry in _state_entries(directory):
        if not entry.name.endswith(".json"):
            continue
        path = Path(entry.path)
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError(f"invalid job record: {path}")
        records.append({**data, "local_record": str(path)})
    return records


def read_text_if_exists(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
