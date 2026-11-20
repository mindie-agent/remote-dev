"""
[Role] Local endpoint state, read-ledger, and job-record persistence (`state_store.py`).
[Invariants]
- Scopes all local state by deterministic `endpoint_id` and SHA-256 `client_context_id` (`id-<sha256>`) so concurrent sessions never collide or leak stale-write authorization.
- Supports lock-free per-call `session_scope()` via `contextvars` without mutating `os.environ`.
- Writes JSON/text records via sibling `.tmp` + `fsync` + `replace_file` (`os.replace()`).
[Timeouts & Locks]
- File replacement tolerates transient Windows sharing locks up to `0.5s` via `atomic.replace_file`.
- Opportunistic retention runs on record/log writes with a `300s` cooldown marker and a
  bounded `2s` budget; a prune failure never blocks the caller's committed write.
[Side-Effect Policy]
- Mutates files exclusively under `state_root()` (scoped `state_dir`, `REMOTE_DEV_STATE_DIR`, or `<cwd>/state`).
- Prunes only expired read ledgers/logs and confirmed-terminal job artifacts; running and
  `unknown` job records are never removed.
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
from contextvars import ContextVar
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from .endpoint import Endpoint, substrate_root
from .container_endpoint import pinned_endpoint
from .atomic import replace_file
from .path_policy import path_fingerprint
from .retention import prune_endpoint_state, prune_stale_session_runtime_dirs
from remote_dev.result import dumps, new_invocation_id, utc_now_iso

LEDGER_SCOPE_ENV_VARS = ("REMOTE_DEV_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_RUN_ID")
LEDGER_SCOPE_PREFIX = "id-"
LEDGER_NO_CONTEXT_SCOPE = "default"
LEDGER_SCHEMA_VERSION = "remote-dev.read_ledger.v2"
LEDGER_SCOPE_ENCODING = "id-sha256"

_SESSION_ID_VAR: ContextVar[str | None] = ContextVar("remote_dev_session_id", default=None)
_STATE_DIR_VAR: ContextVar[Path | None] = ContextVar("remote_dev_state_dir", default=None)
_AUTH_DIR_VAR: ContextVar[Path | None] = ContextVar("remote_dev_auth_dir", default=None)


@contextlib.contextmanager
def session_scope(
    session_id: str | None,
    *,
    state_dir: Path | str | None = None,
    auth_dir: Path | str | None = None,
) -> Iterator[None]:
    """Bind per-call session, state directory, and auth directory without mutating ``os.environ``."""
    session_token = _SESSION_ID_VAR.set(str(session_id) if session_id else None)
    state_token = None
    auth_token = None
    try:
        if state_dir is not None:
            state_token = _STATE_DIR_VAR.set(Path(state_dir).expanduser())
        if auth_dir is not None:
            auth_token = _AUTH_DIR_VAR.set(Path(auth_dir).expanduser())
        yield
    finally:
        if auth_token is not None:
            _AUTH_DIR_VAR.reset(auth_token)
        if state_token is not None:
            _STATE_DIR_VAR.reset(state_token)
        _SESSION_ID_VAR.reset(session_token)


def scoped_auth_dir() -> Path | None:
    """Active context-scoped SSH auth directory, or ``None`` when unscoped."""
    return _AUTH_DIR_VAR.get()


def state_root() -> Path:
    """Local runtime state directory.

    Defaults to ``<cwd>/state`` (ignored by Git). ``session_scope(..., state_dir=...)``
    or ``REMOTE_DEV_STATE_DIR`` relocates it so an embedding consumer can keep
    remote-dev state isolated per session or next to its own untracked state.
    """
    scoped = _STATE_DIR_VAR.get()
    if scoped is not None:
        return scoped
    configured = os.environ.get("REMOTE_DEV_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return substrate_root() / "state"


def _fs_path(path: Path | str) -> str:
    raw = os.fspath(path)
    if os.name != "nt" or raw.startswith(("\\\\?\\", "\\\\.\\")):
        return raw
    abs_path = os.path.abspath(raw)
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


@pinned_endpoint
def endpoint_state_dir(endpoint: Endpoint) -> Path:
    return state_root() / "endpoints" / endpoint.endpoint_id


@pinned_endpoint
def ensure_endpoint_state(endpoint: Endpoint) -> Path:
    base = endpoint_state_dir(endpoint)
    for name in ("context", "reads", "logs", "jobs", "artifacts", "patches"):
        os.makedirs(_fs_path(base / name), exist_ok=True)
    endpoint_path = base / "endpoint.json"
    try:
        os.stat(_fs_path(endpoint_path))
    except FileNotFoundError:
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
    parent_fs = _fs_path(path.parent)
    os.makedirs(parent_fs, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent_fs)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(dumps(data) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(temp_name, _fs_path(path))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, data: str) -> None:
    parent_fs = _fs_path(path.parent)
    os.makedirs(parent_fs, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent_fs)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_file(temp_name, _fs_path(path))
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> Any:
    return json.loads(Path(_fs_path(path)).read_text(encoding="utf-8"))


@pinned_endpoint
def new_log_dir(endpoint: Endpoint, tool_kind: str, invocation_id: str | None = None) -> Path:
    base = ensure_endpoint_state(endpoint)
    token = invocation_id or new_invocation_id()
    path = base / "logs" / tool_kind / token
    os.makedirs(_fs_path(path), exist_ok=True)
    maybe_prune_endpoint_state(endpoint)
    return path


def maybe_prune_endpoint_state(
    endpoint: Endpoint, *, force: bool = False, protect_job_ids=()
) -> dict[str, Any]:
    """Apply bounded local retention after a write; never fails the committed write."""
    return maybe_prune_endpoint_state_dir(
        state_root() / "endpoints" / endpoint.endpoint_id, force=force,
        protect_job_ids=protect_job_ids,
    )


def maybe_prune_endpoint_state_dir(
    base: Path, *, force: bool = False, protect_job_ids=()
) -> dict[str, Any]:
    """Run best-effort maintenance without turning it into a caller-visible failure."""
    clean_base = str(base)
    try:
        result = prune_endpoint_state(
            _fs_path(base), force=force, protect_job_ids=protect_job_ids
        )
        if "base" in result:
            result["base"] = clean_base
        return result
    except TimeoutError as exc:
        # The committed record/log write is already durable. A maintenance
        # timeout is reported, never re-raised into the tool operation.
        return {"base": clean_base, "error": f"TimeoutError: {exc}", "timed_out": True}
    except OSError as exc:
        return {"base": clean_base, "error": f"{type(exc).__name__}: {exc}"}


def _ledger_scope_digest(raw: str) -> str:
    """Full SHA-256 of an arbitrary client context id."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _effective_ledger_context_id(client_context_id: str | None = None) -> str | None:
    if client_context_id:
        return str(client_context_id)
    scoped = _SESSION_ID_VAR.get()
    if scoped:
        return scoped
    for name in LEDGER_SCOPE_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


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
    maybe_prune_endpoint_state(endpoint)
    return path


@pinned_endpoint
def load_read_ledger(endpoint: Endpoint, file_path: str, client_context_id: str | None = None) -> dict[str, Any] | None:
    path = read_ledger_path(endpoint, file_path, client_context_id)
    try:
        data = read_json(path)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        from remote_dev.observability import record_contract_violation
        record_contract_violation(
            "remote.read",
            stage="state_contract",
            contract_name="read_ledger_json",
            reason_code="corrupt_state_json",
            exception=exc,
        )
        raise
    if not isinstance(data, dict):
        from remote_dev.observability import record_contract_violation
        record_contract_violation(
            "remote.read",
            stage="state_contract",
            contract_name="read_ledger_json",
            reason_code="non_object_state_record",
        )
        return None
    return data


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
        entries = os.scandir(_fs_path(directory))
    except FileNotFoundError:
        return []
    with entries:
        return sorted(entries, key=lambda entry: entry.name)


def _endpoint_directories():
    endpoints_root = state_root() / "endpoints"
    return [endpoints_root / entry.name for entry in _state_entries(endpoints_root) if entry.is_dir()]


def _read_owned_record(path: Path, *, operation: str, contract_name: str, label: str) -> dict[str, Any]:
    try:
        data = read_json(path)
    except json.JSONDecodeError as exc:
        from remote_dev.observability import record_contract_violation
        record_contract_violation(
            operation,
            stage="state_contract",
            contract_name=contract_name,
            reason_code="corrupt_state_json",
            exception=exc,
        )
        raise
    if not isinstance(data, dict):
        exc = ValueError(f"invalid {label} record: {path}")
        from remote_dev.observability import record_contract_violation
        record_contract_violation(
            operation,
            stage="state_contract",
            contract_name=contract_name,
            reason_code="non_object_state_record",
            exception=exc,
        )
        raise exc
    return data


def find_job_record(job_id: str) -> tuple[Path, dict[str, Any]] | None:
    for directory in _endpoint_directories():
        path = directory / "jobs" / f"{job_id}.json"
        try:
            data = _read_owned_record(
                path, operation="remote.job_status", contract_name="job_record_json", label="job",
            )
        except FileNotFoundError:
            continue
        return path, data
    return None


def list_endpoint_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in _endpoint_directories():
        path = directory / "endpoint.json"
        try:
            data = _read_owned_record(
                path, operation="remote.probe", contract_name="endpoint_record_json", label="endpoint",
            )
        except FileNotFoundError:
            continue
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
        path = directory / entry.name
        try:
            data = _read_owned_record(
                path, operation="remote.job_status", contract_name="job_record_json", label="job",
            )
        except FileNotFoundError:
            # A concurrent bounded prune removed this terminal record after the
            # directory scan; it is no longer part of the list.
            continue
        records.append({**data, "local_record": str(path)})
    return records


def read_text_if_exists(path: Path) -> str | None:
    fs_target = _fs_path(path)
    try:
        if not stat.S_ISREG(os.stat(fs_target).st_mode):
            return None
        return Path(fs_target).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
