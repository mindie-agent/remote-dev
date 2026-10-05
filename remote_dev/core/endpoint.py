"""Endpoint identity and resolution.

remote-dev resolves endpoints from explicit fields only: ``host`` + ``port``
(plus optional ``user`` / ``root`` / ``cwd`` / ``identity_file`` /
``ssh_mux`` / ``keepalive`` / ...), or an ``alias`` looked up in a local
alias file. Anything else - managed sessions, machine inventories, worktree
bindings, coordinator state - belongs to the consumer. A consumer injects
that knowledge through the resolver plugin interface
(:func:`register_resolver`); remote-dev never imports the consumer.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import EndpointError, caller_error

DEFAULT_USER = os.environ.get("REMOTE_DEV_DEFAULT_USER", "root")
DEFAULT_ROOT = os.environ.get("REMOTE_DEV_DEFAULT_ROOT", "/")
# Empty means "use root". Consumers that want a project tree set
# REMOTE_DEV_DEFAULT_CWD themselves; this package does not assume one.
DEFAULT_CWD = os.environ.get("REMOTE_DEV_DEFAULT_CWD", "") or None
DEFAULT_RUNTIME_ENV_FILE = os.environ.get("REMOTE_DEV_RUNTIME_ENV_FILE", "") or None

# Fields remote-dev understands natively on every tool payload. Consumers may
# add their own selector fields through registered resolvers.
BUILTIN_SELECTOR_FIELDS: tuple[str, ...] = ("host", "port", "alias")

# Environment variable listing consumer resolvers to load at import time:
#   REMOTE_DEV_RESOLVERS="pkg.module:callable,/abs/path/plugin.py:callable"
RESOLVERS_ENV = "REMOTE_DEV_RESOLVERS"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    user: str = DEFAULT_USER
    root: str = DEFAULT_ROOT
    cwd: str | None = None
    runtime_env: bool = True
    runtime_env_file: str | None = DEFAULT_RUNTIME_ENV_FILE
    identity_file: str | None = None
    connect_timeout_ms: int | None = None
    ssh_mux: bool | None = None
    keepalive: bool = False
    kind: str = "direct-endpoint"
    alias: str | None = None
    source: dict[str, Any] | None = None
    container: str | None = None
    container_selector: str | None = None

    def __post_init__(self):
        from .container_endpoint import validate_container
        validate_container(self.container)

    @classmethod
    def for_long_stream(cls, host: str, port: int, **kwargs: Any) -> Endpoint:
        """Endpoint for hour-scale attached streams and ``ssh -N -L`` tunnels.

        Always sets ``ssh_mux=False`` and ``keepalive=True``. This constructor
        cannot be half-configured: an explicit ``ssh_mux=True`` is refused,
        and a passed ``keepalive`` value is ignored.

        ControlMaster cannot carry this use case. It delegates ``-N``
        forwards to the mux master and the client exits rc=0 immediately,
        tearing the tunnel down. That failure is silent — rc=0 with the
        tunnel gone. OpenSSH first-option-wins makes a later
        ``ControlMaster=no`` override ineffective, so the independent
        triple (``ControlMaster=no``, ``ControlPath=none``,
        ``ControlPersist=no``) has to be chosen here, before argv is
        built. ``ControlMaster=no`` alone is not enough: a client can
        still attach to an existing ``ControlPath``.
        """
        if kwargs.get("ssh_mux") is True:
            raise EndpointError(
                "Endpoint.for_long_stream refuses ssh_mux=True: ControlMaster "
                "delegates -N forwards to the mux master and the client exits "
                "rc=0 immediately, tearing the tunnel down. That failure is "
                "silent — rc=0 with the tunnel gone. OpenSSH first-option-wins "
                "makes a later ControlMaster=no override ineffective, and "
                "ControlMaster=no alone is not enough because a client can "
                "still attach to an existing ControlPath. This constructor "
                "always sets ssh_mux=False and keepalive=True."
            )
        kwargs.pop("ssh_mux", None)
        kwargs.pop("keepalive", None)
        return cls(host=host, port=port, ssh_mux=False, keepalive=True, **kwargs)

    @property
    def effective_cwd(self) -> str:
        return self.cwd or DEFAULT_CWD or self.root

    @property
    def endpoint_key(self) -> str:
        key = f"{self.user}@{self.host}:{self.port}|root={self.root}"
        return key + f"|container={self.container}" if self.container else key

    @property
    def endpoint_id(self) -> str:
        return hashlib.sha256(self.endpoint_key.encode("utf-8")).hexdigest()[:16]

    def destination(self) -> str:
        return f"{self.user}@{self.host}"

    def to_result_target(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "endpoint_id": self.endpoint_id,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "root": self.root,
            "cwd": self.effective_cwd,
            "runtime_env": self.runtime_env,
        }
        if self.runtime_env_file:
            payload["runtime_env_file"] = self.runtime_env_file
        if self.container:
            payload["container"] = self.container
        if self.container_selector:
            payload["container_selector"] = self.container_selector
        if self.alias:
            payload["alias"] = self.alias
        if self.source:
            payload["source"] = self.source
        if self.ssh_mux is not None:
            payload["ssh_mux"] = self.ssh_mux
        if self.keepalive:
            payload["keepalive"] = True
        return payload


def substrate_root() -> Path:
    """Directory used for default local files (endpoints.json, state/).

    Defaults to the process working directory. Relocate state with
    ``REMOTE_DEV_STATE_DIR`` and alias files with ``REMOTE_DEV_ENDPOINTS_FILE``.
    """
    return Path.cwd()


# ---------------------------------------------------------------------------
# Alias files
# ---------------------------------------------------------------------------


def alias_files() -> list[Path]:
    """Alias files searched in order; later files override earlier ones.

    ``REMOTE_DEV_ENDPOINTS_FILE`` may hold one path or an ``os.pathsep``
    separated list. The process-cwd ``endpoints.json`` /
    ``endpoints.local.json`` are always consulted afterwards; both are
    ignored by Git so a checkout never carries real endpoint data.
    """
    paths: list[Path] = []
    configured = os.environ.get("REMOTE_DEV_ENDPOINTS_FILE", "")
    for item in configured.split(os.pathsep):
        if item.strip():
            paths.append(Path(item.strip()).expanduser())
    paths.append(substrate_root() / "endpoints.json")
    paths.append(substrate_root() / "endpoints.local.json")
    return paths


def _read_endpoint_aliases() -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in alias_files():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EndpointError(f"invalid endpoint alias file {path}: {exc}") from exc
        entries = data.get("endpoints", data if isinstance(data, dict) else {})
        if not isinstance(entries, dict):
            raise EndpointError(f"endpoint alias file {path} must be an object")
        merged.update(entries)
    return merged


# ---------------------------------------------------------------------------
# Direct endpoints
# ---------------------------------------------------------------------------


def _connect_timeout_ms(payload: dict[str, Any]) -> int | None:
    raw = payload.get("connect_timeout_ms")
    if raw is not None and (type(raw) is not int or raw <= 0):
        raise EndpointError("endpoint connect_timeout_ms must be a positive integer when supplied")
    return raw


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if isinstance(raw, bool):
        return raw
    raise EndpointError(f"endpoint {key} must be a boolean")


def _bool_field(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in payload or payload[key] is None:
        return default
    raw = payload[key]
    if isinstance(raw, bool):
        return raw
    raise EndpointError(f"endpoint {key} must be a boolean")


def _direct_endpoint(payload: dict[str, Any]) -> Endpoint:
    if "host" not in payload or "port" not in payload:
        raise EndpointError("direct endpoint requires host and port")
    try:
        port = int(payload["port"])
    except (TypeError, ValueError) as exc:
        raise EndpointError("endpoint port must be an integer") from exc
    if isinstance(payload.get("port"), bool) or not (1 <= port <= 65535):
        raise EndpointError(f"endpoint port must be in 1..65535, got {payload['port']!r}")
    root = str(payload.get("root") or DEFAULT_ROOT)
    cwd = str(payload["cwd"]) if payload.get("cwd") else None
    if not root.startswith("/"):
        raise EndpointError(f"endpoint root must be an absolute path, got {root!r}")
    effective_cwd = cwd or DEFAULT_CWD or root
    if not str(effective_cwd).startswith("/"):
        raise EndpointError(f"endpoint cwd must be an absolute path, got {effective_cwd!r}")
    runtime_env_file = payload.get("runtime_env_file", DEFAULT_RUNTIME_ENV_FILE)
    return Endpoint(
        host=str(payload["host"]),
        port=port,
        user=str(payload.get("user") or DEFAULT_USER),
        root=root,
        cwd=cwd,
        runtime_env=bool(payload.get("runtime_env", True)),
        runtime_env_file=str(runtime_env_file) if runtime_env_file else None,
        identity_file=str(payload["identity_file"]) if payload.get("identity_file") else None,
        connect_timeout_ms=_connect_timeout_ms(payload),
        ssh_mux=_optional_bool(payload, "ssh_mux"),
        keepalive=_bool_field(payload, "keepalive", False),
        kind=str(payload.get("kind") or "direct-endpoint"),
        alias=str(payload["alias"]) if payload.get("alias") else None,
        source=payload.get("source") if isinstance(payload.get("source"), dict) else None,
        container=payload.get("container"),
    )


# ---------------------------------------------------------------------------
# Resolver plugin interface
# ---------------------------------------------------------------------------

# A resolver receives the raw tool payload and returns one of:
#   * ``None``            - it does not claim this payload; try the next one.
#   * ``dict``            - an endpoint payload with at least ``host`` and
#                           ``port``; remote-dev builds the Endpoint from it and
#                           lets explicit caller fields (root/cwd/user/...)
#                           override the resolver's values.
#   * :class:`Endpoint`   - used as is.
# Raising :class:`EndpointError` (or any exception) aborts resolution with a
# clear message; resolvers should raise when they *claim* a payload but cannot
# resolve it, and return ``None`` when the payload is simply not theirs.
Resolver = Callable[[dict[str, Any]], "dict[str, Any] | Endpoint | None"]


@dataclass(frozen=True)
class RegisteredResolver:
    name: str
    resolve: Resolver
    fields: tuple[str, ...] = ()
    description: str = ""


_RESOLVERS: list[RegisteredResolver] = []
_ENV_RESOLVERS_LOADED = False
_ENV_RESOLVERS_ERROR: EndpointError | None = None


def register_resolver(
    resolve: Resolver,
    *,
    name: str | None = None,
    fields: Iterable[str] = (),
    description: str = "",
) -> RegisteredResolver:
    """Register a consumer endpoint resolver.

    ``fields`` names the payload keys this resolver claims (for example
    ``("lab",)``). They are advertised as endpoint selectors
    so tools that normally require an endpoint (``remote.job_status`` and
    friends may run without one) can tell "the caller supplied a selector"
    apart from "no target at all". Resolvers are consulted in registration
    order after direct ``host``+``port`` and ``alias`` handling, and they are
    consulted even for payloads with no selector at all, so a consumer may
    implement zero-argument auto-binding on its own side.
    """
    resolver_name = name or getattr(resolve, "__qualname__", None) or getattr(resolve, "__name__", None) or repr(resolve)
    entry = RegisteredResolver(
        name=str(resolver_name),
        resolve=resolve,
        fields=tuple(str(field) for field in fields),
        description=description,
    )
    for existing in _RESOLVERS:
        if existing.name == entry.name:
            raise EndpointError(f"endpoint resolver {entry.name!r} is already registered")
    _RESOLVERS.append(entry)
    return entry


def unregister_resolver(name: str) -> bool:
    for index, entry in enumerate(_RESOLVERS):
        if entry.name == name:
            del _RESOLVERS[index]
            return True
    return False


def clear_resolvers() -> None:
    """Drop every registered resolver (tests and embedding hosts)."""
    _RESOLVERS.clear()


def registered_resolvers() -> tuple[RegisteredResolver, ...]:
    _load_env_resolvers()
    return tuple(_RESOLVERS)


def selector_fields() -> tuple[str, ...]:
    """Payload keys that count as "an endpoint selector was supplied"."""
    fields = list(BUILTIN_SELECTOR_FIELDS)
    for entry in registered_resolvers():
        for field in entry.fields:
            if field not in fields:
                fields.append(field)
    return tuple(fields)


def has_selector(payload: dict[str, Any]) -> bool:
    return any(payload.get(field) for field in selector_fields())


def _load_callable(spec: str) -> Callable[..., Any]:
    if ":" not in spec:
        raise EndpointError(f"{RESOLVERS_ENV} entry {spec!r} must look like module:callable or /path/file.py:callable")
    module_spec, attr = spec.rsplit(":", 1)
    module_spec = module_spec.strip()
    attr = attr.strip()
    if module_spec.endswith(".py"):
        path = Path(module_spec).expanduser()
        if not path.is_file():
            raise EndpointError(f"{RESOLVERS_ENV} plugin file does not exist: {path}")
        module_name = f"remote_dev_resolver_{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]}"
        loader_spec = importlib.util.spec_from_file_location(module_name, path)
        if loader_spec is None or loader_spec.loader is None:
            raise EndpointError(f"{RESOLVERS_ENV} plugin file could not be loaded: {path}")
        module = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(module)
    else:
        try:
            module = importlib.import_module(module_spec)
        except Exception as exc:  # noqa: BLE001
            raise EndpointError(f"{RESOLVERS_ENV} plugin module {module_spec!r} failed to import: {exc}") from exc
    target = module
    for part in attr.split("."):
        if not hasattr(target, part):
            raise EndpointError(f"{RESOLVERS_ENV} plugin {spec!r} has no attribute {attr!r}")
        target = getattr(target, part)
    if not callable(target):
        raise EndpointError(f"{RESOLVERS_ENV} plugin {spec!r} is not callable")
    return target


def _load_env_resolvers() -> None:
    """Load resolvers named in ``REMOTE_DEV_RESOLVERS`` once per process.

    Each entry is ``module:callable`` or ``/path/plugin.py:callable``. The
    callable is either a resolver (registered directly under its qualified
    name) or a *setup hook*: a callable that itself calls
    :func:`register_resolver` and returns ``None`` when invoked with no
    arguments. Setup hooks let a consumer register several resolvers with
    custom ``fields`` from one entry point.
    """
    global _ENV_RESOLVERS_LOADED, _ENV_RESOLVERS_ERROR
    if _ENV_RESOLVERS_LOADED:
        # A misconfigured plugin list must fail every resolution, not only
        # the first one; otherwise a long-lived MCP server would silently
        # continue with "no resolvers" after one visible error.
        if _ENV_RESOLVERS_ERROR is not None:
            raise _ENV_RESOLVERS_ERROR
        return
    _ENV_RESOLVERS_LOADED = True
    raw = os.environ.get(RESOLVERS_ENV, "")
    try:
        for spec in [item.strip() for item in raw.split(",") if item.strip()]:
            target = _load_callable(spec)
            if getattr(target, "remote_dev_resolver_setup", False):
                target()
                continue
            register_resolver(target, name=spec)
    except EndpointError as exc:
        _ENV_RESOLVERS_ERROR = exc
        raise
    except Exception as exc:  # noqa: BLE001
        _ENV_RESOLVERS_ERROR = EndpointError(f"{RESOLVERS_ENV} plugin setup failed: {exc}")
        raise _ENV_RESOLVERS_ERROR from exc


def resolver_setup(func: Callable[[], None]) -> Callable[[], None]:
    """Mark a zero-argument callable as a resolver *setup hook*.

    A setup hook named in ``REMOTE_DEV_RESOLVERS`` is invoked once at load
    time instead of being registered as a resolver. It should call
    :func:`register_resolver` for every resolver it provides.
    """
    setattr(func, "remote_dev_resolver_setup", True)
    return func


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _endpoint_from_resolver(entry: RegisteredResolver, payload: dict[str, Any]) -> Endpoint | None:
    try:
        resolved = entry.resolve(payload)
    except EndpointError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EndpointError(f"endpoint resolver {entry.name!r} failed: {exc}") from exc
    if resolved is None:
        return None
    if isinstance(resolved, Endpoint):
        if payload.get("container") is not None:
            return replace(resolved, container=payload["container"], container_selector=None)
        return resolved
    if not isinstance(resolved, dict):
        raise EndpointError(f"endpoint resolver {entry.name!r} returned {type(resolved).__name__}; expected dict, Endpoint, or None")
    # Explicit caller fields win over resolver defaults for the endpoint
    # shape (root/cwd/user/...). Selector fields the resolver consumed are
    # not endpoint fields and are dropped from the merged payload.
    overrides = {
        key: value
        for key, value in payload.items()
        if key in (
            "user",
            "root",
            "cwd",
            "runtime_env",
            "runtime_env_file",
            "identity_file",
            "connect_timeout_ms",
            "ssh_mux",
            "keepalive",
            "container",
        )
        and value is not None
    }
    merged = {**resolved, **overrides}
    merged.setdefault("kind", f"resolver:{entry.name}")
    source = merged.get("source")
    merged["source"] = {**(source if isinstance(source, dict) else {}), "resolver": entry.name}
    return _direct_endpoint(merged)


def resolve_endpoint(payload: dict[str, Any]) -> Endpoint:
    """Resolve a tool payload into an :class:`Endpoint`.

    Order: explicit ``host``+``port``; ``alias`` from the alias files;
    registered resolvers in registration order (each may decline with
    ``None``). Nothing else is consulted: remote-dev holds no consumer state
    and never guesses a target from the working directory.
    """
    if payload.get("host") and payload.get("port"):
        return _direct_endpoint(payload)
    if payload.get("alias"):
        aliases = _read_endpoint_aliases()
        alias = str(payload["alias"])
        if alias not in aliases:
            raise EndpointError(f"endpoint alias {alias!r} is not configured")
        merged = {**aliases[alias], **{k: v for k, v in payload.items() if v is not None}}
        merged["alias"] = alias
        return _direct_endpoint(merged)
    for entry in registered_resolvers():
        endpoint = _endpoint_from_resolver(entry, payload)
        if endpoint is not None:
            return endpoint
    known = ", ".join(selector_fields())
    resolvers = ", ".join(entry.name for entry in registered_resolvers()) or "none"
    raise caller_error(
        "no endpoint target: provide host and port together, an alias from the "
        f"endpoint alias files, or a selector understood by a registered resolver "
        f"(known selector fields: {known}; registered resolvers: {resolvers})",
        EndpointError,
    )
