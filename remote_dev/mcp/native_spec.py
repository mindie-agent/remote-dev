"""
[Role] Core trusted-profile primitives: selection, native schema construction, strict validation.
[Invariants]
- Profile comes only from env/explicit API, never tool arguments.
- Native schema advertises only native fields plus explicit endpoint selectors.
- Unknown fields, wrong types, required fields and bounds are rejected before dispatch.
[Timeouts & Locks] Pure functions; no I/O or locks.
[Side-Effect Policy] Side-effect free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from remote_dev.core.errors import caller_error

PROFILE_ENV = "REMOTE_DEV_HARNESS_PROFILE"

CANONICAL_PROFILE = "canonical"

NATIVE_PROFILES = ("codex", "claude", "kimi", "dsh")

PROFILES = (CANONICAL_PROFILE,) + NATIVE_PROFILES

class HarnessProfileError(ValueError):
    """The requested profile is unknown or a native call is not bound to it.

    A pre-dispatch caller error: it carries ``error_code`` and the offending
    top-level ``field`` so adapters can report actionable, non-echoing hints.
    """

    category = "caller"
    error_code = "invalid_argument"

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field

def resolve_profile(value: str | None = None) -> str:
    """Return the validated profile for this process/call.

    An explicit argument wins over the environment. A missing value means the
    standalone canonical surface; native aliases remain unavailable until a
    caller or plugin explicitly binds a harness profile.
    """
    if value is not None:
        candidate = str(value).strip().lower()
    else:
        import os

        candidate = os.environ.get(PROFILE_ENV, "").strip().lower()
    if not candidate:
        return CANONICAL_PROFILE
    if candidate not in PROFILES:
        raise HarnessProfileError(
            f"unknown {PROFILE_ENV}={candidate!r}; expected one of {', '.join(PROFILES)}"
        )
    return candidate

def wire_name(native_name: str) -> str:
    return f"remote_{native_name}"

@dataclass(frozen=True)
class NativeToolSpec:
    """One local basic tool exposed through a trusted harness profile."""

    native_name: str
    canonical: str
    description: str
    properties: Mapping[str, Any]
    required: tuple[str, ...] = ()
    defaults: Mapping[str, Any] = field(default_factory=dict)
    handler: str = "canonical"
    compat_properties: Mapping[str, Any] = field(default_factory=dict)
    additional_properties: bool | None = False
    require_prior_read: bool = False
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def wire(self) -> str:
        return wire_name(self.native_name)

_ENDPOINT_KEYS = (
    "host", "port", "user", "container", "root", "cwd", "runtime_env",
    "identity_file", "auth_ref", "known_hosts_file", "connect_timeout_ms", "runtime_env_file", "alias",
)

def _endpoint_args(args: Mapping[str, Any]) -> dict[str, Any]:
    from remote_dev.core.endpoint import selector_fields
    return {key: args[key] for key in (*_ENDPOINT_KEYS, *selector_fields()) if key in args}

def _number(value: Any) -> Any:
    """JSON numbers are not booleans; keep integral values integral."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value

NATIVE_TOOLS: dict[str, dict[str, NativeToolSpec]] = {profile: {} for profile in NATIVE_PROFILES}

JOB_RECORD_ENDPOINT_DESCRIPTION = (
    "Endpoint selector fields are optional for this tool: the endpoint is "
    "recovered from the stored job record. Supply host and port together, or "
    "an alias, only to override or when no job record exists."
)

_JOB_RECORD_CANONICALS = frozenset({"remote.job_stdin", "remote.job_output", "remote.job_status", "remote.job_stop", "remote.job_tail"})

def native_input_schema(
    spec: NativeToolSpec,
    endpoint_props: Mapping[str, Any],
    selector_description: str = "",
) -> dict[str, Any]:
    # Compatibility fields are accepted by the server but are not advertised
    # on the native wire schema. Only the native fields and the explicit remote
    # target fields belong in tools/list.
    description = JOB_RECORD_ENDPOINT_DESCRIPTION if spec.canonical in _JOB_RECORD_CANONICALS else selector_description
    return {
        "type": "object",
        **({"description": description} if description else {}),
        "properties": {**endpoint_props, **spec.properties},
        "required": list(spec.required),
        **({"additionalProperties": bool(spec.additional_properties)} if spec.additional_properties is not None else {}),
    }

_TYPE_NAMES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

def _type_ok(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    py = _TYPE_NAMES.get(expected)
    if py is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, py)

def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    if not isinstance(schema, Mapping):
        return
    if "const" in schema and value != schema["const"]:
        raise HarnessProfileError(f"{path} must equal {schema['const']!r}")
    if "not" in schema:
        try:
            _validate_value(value, schema["not"], path)
        except HarnessProfileError:
            pass
        else:
            raise HarnessProfileError(f"{path} has a forbidden value")
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessProfileError(f"{path} must be one of {schema['enum']!r}")
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, str(option)) for option in options):
            raise HarnessProfileError(f"{path} must be {' or '.join(str(o) for o in options)}")
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                _validate_value(value, option, path)
                break
            except HarnessProfileError as exc:
                errors.append(str(exc))
        else:
            raise HarnessProfileError(f"{path} does not match any allowed shape: {'; '.join(errors)}")
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _validate_value(value, option, path)
                matches += 1
            except HarnessProfileError:
                pass
        if matches != 1:
            raise HarnessProfileError(f"{path} must match exactly one allowed shape")
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            raise HarnessProfileError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise HarnessProfileError(f"{path} must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise HarnessProfileError(f"{path} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise HarnessProfileError(f"{path} must be < {schema['exclusiveMaximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise HarnessProfileError(f"{path} must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise HarnessProfileError(f"{path} must have at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(str(schema["pattern"]), value):
            raise HarnessProfileError(f"{path} does not match {schema['pattern']!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                raise HarnessProfileError(f"{path} requires {key}")
        properties = schema.get("properties") or {}
        extra = value.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_value(item, properties[key], f"{path}.{key}")
            elif extra is False:
                raise HarnessProfileError(f"{path} received unsupported field {key!r}")

def validate_native_arguments(spec: NativeToolSpec, args: Mapping[str, Any], allowed_endpoint: set[str]) -> None:
    """Reject unknown/mistyped native arguments before any endpoint resolution."""
    if not isinstance(args, dict):
        raise HarnessProfileError("native tool arguments must be an object")
    allowed = set(spec.properties) | set(allowed_endpoint)
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise HarnessProfileError(
            f"{spec.wire} received unsupported argument(s): {', '.join(unknown)}"
        )
    required_aliases = {
        "file_path": ("path", "AbsolutePath", "TargetFile", "target_file"),
        "command": ("cmd",),
        "job_id": ("session_id", "task_id", "bash_id", "shell_id"),
        "task_id": ("bash_id", "shell_id"),
        "path": ("file_path",),
    }
    for key in spec.required:
        if key not in args or args[key] is None:
            if any(alias in args for alias in required_aliases.get(key, ())):
                continue
            raise caller_error(
                f"{spec.wire} requires {key}",
                HarnessProfileError, field=key,
                repair={"missing": [key],
                        "action": f"Retry with {key} supplied; no remote effect occurred."},
            )
    for key, value in args.items():
        schema = spec.properties.get(key)
        if schema is not None:
            try:
                _validate_value(value, schema, key)
            except HarnessProfileError as exc:
                if exc.field is None:
                    exc.field = key
                raise

_DSH_SANDBOX_PROPERTIES = {
    "sandbox_permissions": {
        "type": "string",
        "enum": ["workspace-write", "danger-full-access"],
        "description": "Local host sandbox escalation target; remote-dev cannot grant host privileges.",
    },
    "justification": {
        "type": "string",
        "description": "Required with sandbox_permissions: one sentence for the user.",
    },
}

def _reject_sandbox_escalation(args: Mapping[str, Any]) -> None:
    if "sandbox_permissions" in args or "justification" in args:
        raise HarnessProfileError(
            "sandbox_permissions/justification are local host escalation controls; "
            "remote-dev does not treat them as remote authorization"
        )

