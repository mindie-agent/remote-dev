"""[Role] User-scoped SSH profiles and the shared local credential file.
[Invariants]
- Profiles contain references, never passwords; no system credential UI is used.
- All consumers share auth_root/credentials.json unless a file is explicitly selected.
[Timeouts & Locks]
- Atomic file updates use bounded record locks; documents are limited to 1 MiB.
[Side-Effect Policy]
- Writes only the chosen auth directory or explicit credential file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from remote_dev.core.atomic import replace_file
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.locking import record_lock

_REFERENCE = re.compile(r"ssh-[0-9a-f]{24}\Z")
_MAX_DOCUMENT = 1024 * 1024


def _seconds(timeout_ms):
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0:
        raise ValueError("SSH credential timeout_ms must be positive")
    return timeout_ms / 1000


def auth_error(message, category="authentication"):
    return RemoteExecutionError(message, category=category, submission_state="not_sent")


def auth_root() -> Path:
    return Path(os.environ.get("REMOTE_DEV_AUTH_DIR", "~/.mindie/ssh")).expanduser().resolve()


def ensure_auth_root() -> Path:
    """Create the private root before a lock can create it with broader defaults."""
    root = auth_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def prepare_auth_directory(path: Path):
    if path.is_relative_to(auth_root()):
        ensure_auth_root()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _read(path: Path, default=None):
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_DOCUMENT + 1)
    except FileNotFoundError:
        return default
    if len(raw) > _MAX_DOCUMENT:
        raise auth_error(f"SSH auth document exceeds 1 MiB: {path}", "configuration")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise auth_error(f"Invalid SSH auth document: {path}", "configuration") from exc


def _write(path: Path, value):
    prepare_auth_directory(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class CredentialStore:
    """One local-user secret store shared across processes and plugin runtimes."""

    def _file(self) -> Path:
        configured = os.environ.get("REMOTE_DEV_CREDENTIAL_FILE")
        return Path(configured).expanduser().resolve() if configured else auth_root() / "credentials.json"

    def _file_values(self, path):
        values = _read(path, {})
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise auth_error("The SSH credential file must be a JSON object of reference/password pairs", "configuration")
        return values

    def put(self, password: str, *, timeout_ms=10000) -> str:
        if not isinstance(password, str) or not password:
            raise auth_error("An SSH password is required", "credential_required")
        reference = "file:" + uuid.uuid4().hex
        path = self._file()
        prepare_auth_directory(path)
        with record_lock(path, timeout_seconds=_seconds(timeout_ms)):
            values = self._file_values(path)
            values[reference] = password
            _write(path, values)
        return reference

    def get(self, reference: str, *, timeout_ms=10000) -> str:
        provider, separator, name = reference.partition(":")
        if not separator or not name:
            raise auth_error("Invalid SSH credential reference", "configuration")
        if provider == "env":
            value = os.environ.get(name)
        elif provider == "file":
            value = self._file_values(self._file()).get(reference)
        else:
            raise auth_error(f"Unsupported SSH credential provider: {provider}", "configuration")
        if not value:
            raise auth_error(f"SSH credential is unavailable: {reference}", "credential_unavailable")
        return value

    def delete(self, reference: str, *, timeout_ms=10000):
        provider = reference.partition(":")[0]
        if provider == "env":
            return
        if provider != "file":
            raise auth_error("Unsupported SSH credential reference", "configuration")
        path = self._file()
        prepare_auth_directory(path)
        with record_lock(path, timeout_seconds=_seconds(timeout_ms)):
            values = self._file_values(path)
            values.pop(reference, None)
            _write(path, values)


class ProfileStore:
    """One profile per SSH principal, independent of cwd, container and job state."""

    @staticmethod
    def target(endpoint):
        return {"host": endpoint.host, "port": endpoint.port, "user": endpoint.user}

    @classmethod
    def reference(cls, endpoint):
        value = json.dumps(cls.target(endpoint), sort_keys=True, separators=(",", ":"))
        return "ssh-" + hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def path(reference):
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
            raise auth_error("Invalid SSH auth_ref", "configuration")
        return auth_root() / "profiles" / (reference + ".json")

    def resolve(self, endpoint):
        explicit = getattr(endpoint, "auth_ref", None)
        reference = explicit or self.reference(endpoint)
        value = _read(self.path(reference))
        if value is None:
            if explicit:
                raise auth_error(f"SSH auth profile does not exist: {reference}", "credential_unavailable")
            return None
        if not isinstance(value, dict) or value.get("auth_ref") != reference:
            raise auth_error("Invalid SSH auth profile", "configuration")
        if value.get("target") != self.target(endpoint):
            raise auth_error("SSH auth profile belongs to a different host, port or user", "configuration")
        if value.get("auth_method") not in {"publickey", "password"}:
            raise auth_error("SSH auth profile has an unsupported authentication method", "configuration")
        return value

    def save(self, profile, *, timeout_ms=5000):
        path = self.path(profile["auth_ref"])
        prepare_auth_directory(path)
        with record_lock(path, timeout_seconds=_seconds(timeout_ms)):
            _write(path, profile)

    def delete(self, reference, *, timeout_ms=5000):
        path = self.path(reference)
        prepare_auth_directory(path)
        with record_lock(path, timeout_seconds=_seconds(timeout_ms)):
            path.unlink(missing_ok=True)
