"""Non-secret endpoint alias persistence for frictionless SSH onboarding.

[Role]
Record a verified SSH endpoint (host/port/user/container/cwd/root/auth_ref) in
the local `endpoints.json` alias map so later calls can omit host and port.
[Invariants]
- Only non-secret connection fields are persisted; passwords and credential
  references never enter the alias file.
- Read-merge-write happens under `record_lock` on `<target>.lock`; the target
  is replaced atomically with `0o600` permissions, and only directories created
  by this store receive `0o700` (existing parent modes are never changed).
[Timeouts & Locks]
Bounded `record_lock` acquisition; no SSH or network I/O.
[Side-Effect Policy]
Mutates only the selected endpoint alias file and its adjacent lock.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from remote_dev.core.locking import record_lock

DEFAULT_ENDPOINTS_FILE = Path("~/.mindie/endpoints.json")
_LOCK_TIMEOUT_SECONDS = 10.0
RESERVED_ALIASES = frozenset({"default_alias", "endpoints"})


def endpoints_target_path() -> Path:
    """Highest-precedence configured endpoints path, else the user-level file.

    The resolver merges ``REMOTE_DEV_ENDPOINTS_FILE`` paths in order and gives
    later paths precedence, so writes must land on the same last path. Writing
    the first path would leave the effective merged view unchanged.
    """
    configured = os.environ.get("REMOTE_DEV_ENDPOINTS_FILE", "")
    paths = [
        Path(item.strip()).expanduser()
        for item in configured.split(os.pathsep)
        if item.strip()
    ]
    return paths[-1] if paths else DEFAULT_ENDPOINTS_FILE.expanduser()


def _validate_alias(alias: str) -> str:
    if alias in RESERVED_ALIASES:
        raise ValueError(f"endpoint alias cannot be named {alias!r}")
    return alias


def _non_secret_record(endpoint: Any, facts: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "host": endpoint.host,
        "port": int(endpoint.port),
        "user": endpoint.user,
    }
    if getattr(endpoint, "container", None):
        record["container"] = endpoint.container
    if getattr(endpoint, "cwd", None):
        record["cwd"] = endpoint.cwd
    root = getattr(endpoint, "root", None)
    if root and root != "/":
        record["root"] = root
    auth_ref = facts.get("auth_ref")
    if isinstance(auth_ref, str) and auth_ref:
        record["auth_ref"] = auth_ref
    return record


def _read_payload(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {}, True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid endpoints file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"endpoints file {path} must be a JSON object")
    if "endpoints" in payload and not isinstance(payload["endpoints"], dict):
        raise ValueError(f"endpoints file {path} has a non-object 'endpoints' map")
    nested = isinstance(payload.get("endpoints"), dict)
    return payload, nested


def _merged_payload(payload: dict[str, Any], nested: bool, alias: str,
                    record: dict[str, Any], *, save_default: bool) -> dict[str, Any]:
    if nested:
        entries = dict(payload.get("endpoints") or {})
        entries[alias] = record
        merged = {key: value for key, value in payload.items() if key != "endpoints"}
        merged["endpoints"] = entries
    else:
        existing_default = payload.get("default_alias")
        merged = {key: value for key, value in payload.items() if key != "default_alias"}
        merged[alias] = record
        if not save_default and isinstance(existing_default, str) and existing_default:
            merged["default_alias"] = existing_default
    if save_default:
        merged["default_alias"] = alias
    return merged


def _private_directory(path: Path) -> None:
    """Create missing ancestors with 0o700; never chmod an existing directory."""
    if path.exists():
        return
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(directory, 0o700)
        except TimeoutError: raise
        except OSError:
            # A filesystem without POSIX modes still keeps the atomic write path.
            pass


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    _private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except TimeoutError: raise
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except TimeoutError: raise
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_endpoint_record(endpoint: Any, facts: Mapping[str, Any], *,
                         save_default: bool = False,
                         save_alias: str | None = None,
                         timeout_seconds: float | None = None) -> dict[str, Any]:
    """Merge one verified endpoint into the alias file and return a receipt."""
    alias = _validate_alias((save_alias or "").strip() or "default")
    path = endpoints_target_path()
    record = _non_secret_record(endpoint, facts)
    _private_directory(path.parent)
    lock_timeout = _LOCK_TIMEOUT_SECONDS if timeout_seconds is None else max(0.0, timeout_seconds)
    with record_lock(path, timeout_seconds=lock_timeout):
        payload, nested = _read_payload(path)
        merged = _merged_payload(payload, nested, alias, record, save_default=save_default)
        _atomic_write(path, merged)
    return {"saved": True, "alias": alias, "default": bool(save_default), "path": str(path)}


def clear_auth_ref(auth_refs: Any, *, timeout_seconds: float | None = None) -> dict[str, Any]:
    """Drop matching non-secret ``auth_ref`` values from saved aliases.

    Called after ``auth forget`` so re-enrolling through a saved alias no
    longer resolves an explicit, now-missing profile reference.
    """
    refs = {item for item in (auth_refs if isinstance(auth_refs, (list, tuple, set)) else [auth_refs])
            if isinstance(item, str) and item}
    path = endpoints_target_path()
    if not refs or not path.exists():
        return {"cleared": 0, "path": str(path)}
    _private_directory(path.parent)
    lock_timeout = _LOCK_TIMEOUT_SECONDS if timeout_seconds is None else max(0.0, timeout_seconds)
    cleared = 0
    with record_lock(path, timeout_seconds=lock_timeout):
        payload, nested = _read_payload(path)
        if nested:
            entries = dict(payload.get("endpoints") or {})
        else:
            entries = {key: value for key, value in payload.items() if key != "default_alias"}
        for alias, record in list(entries.items()):
            if isinstance(record, dict) and record.get("auth_ref") in refs:
                updated = dict(record)
                updated.pop("auth_ref", None)
                entries[alias] = updated
                cleared += 1
        if cleared:
            if nested:
                merged = {key: value for key, value in payload.items() if key != "endpoints"}
                merged["endpoints"] = entries
            else:
                merged = dict(entries)
                default_alias = payload.get("default_alias")
                if isinstance(default_alias, str) and default_alias:
                    merged["default_alias"] = default_alias
            _atomic_write(path, merged)
    return {"cleared": cleared, "path": str(path)}
