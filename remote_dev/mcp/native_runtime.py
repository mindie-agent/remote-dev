"""
[Role] Native dispatch registry and canonical hand-off.
[Invariants]
- Validates native contract before routing; no canonical-compatibility bypass.
- Prior-read policy is checked only after the native schema passes.
[Timeouts & Locks] No I/O; prior-read stat is delegated to core.
[Side-Effect Policy] Dispatches to canonical core only.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from remote_dev.core.endpoint import selector_fields
from remote_dev.core.errors import caller_error
from remote_dev.mcp.harness_profiles import NativeToolSpec, validate_native_arguments
from remote_dev.mcp.schemas import ENDPOINT_PROPS

CanonicalCall = Callable[..., dict[str, Any]]
Handler = Callable[[str, NativeToolSpec, dict[str, Any], dict[str, Any], CanonicalCall], dict[str, Any]]
_HANDLERS: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    def register(function: Handler) -> Handler:
        _HANDLERS[name] = function
        return function

    return register

def _allowed_endpoint_fields() -> set[str]:
    return set(ENDPOINT_PROPS) | set(selector_fields())

def _require_prior_read(normalized: Mapping[str, Any]) -> None:
    """Enforce the native observation policy for existing files."""
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core.errors import EndpointError
    from remote_dev.core.file_ops import remote_stat
    from remote_dev.core.path_policy import join_under_root
    from remote_dev.core.state_store import load_read_ledger

    file_path = normalized.get("file_path")
    if not file_path:
        return
    try:
        endpoint = resolve_endpoint(dict(normalized))
    except EndpointError:
        # Dispatch will report the missing/ambiguous endpoint itself.
        return
    try:
        stat = remote_stat(endpoint, file_path=str(file_path))
    except (ValueError, TimeoutError, OSError) as exc:
        raise caller_error(f"cannot verify prior read for {file_path}: {exc}") from exc
    if stat.get("status") == "path_outside_root":
        raise caller_error(str(stat.get("error") or "path outside endpoint root"))
    if not stat.get("exists"):
        return
    path = join_under_root(endpoint.root, endpoint.effective_cwd, str(file_path))
    if not load_read_ledger(endpoint, str(path)):
        raise caller_error(
            f"{file_path} has not been read yet in this session; read it before writing or editing"
        )

def _native_defaults(spec: NativeToolSpec, args: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(spec.defaults)
    merged.update(args)
    return merged

def dispatch_native(
    profile: str,
    spec: NativeToolSpec,
    args: dict[str, Any],
    call_canonical: CanonicalCall,
) -> dict[str, Any]:
    # Validate the native contract before any compatibility shortcut: a
    # canonical field must never bypass a required native field or a native
    # type/unit check.
    validate_native_arguments(spec, args, _allowed_endpoint_fields())
    # Native wire names are strict. Canonical-compatibility fields are not
    # accepted under a native name; use the canonical dotted tool instead.
    native_args = _native_defaults(spec, args)
    normalized = spec.normalize(native_args) if spec.normalize is not None else native_args
    if not isinstance(normalized, dict):
        raise caller_error(f"{spec.wire} internal normalization did not produce an object")
    if spec.require_prior_read:
        _require_prior_read(normalized)
    if spec.handler == "canonical":
        return call_canonical(spec.canonical, normalized)
    try:
        implementation = _HANDLERS[spec.handler]
    except KeyError as exc:
        raise caller_error(f"{spec.wire} has no registered native handler {spec.handler!r}") from exc
    return implementation(profile, spec, native_args, normalized, call_canonical)

