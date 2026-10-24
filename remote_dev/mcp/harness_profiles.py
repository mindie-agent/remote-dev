"""
[Role] Trusted per-Harness profile selection and native basic-tool descriptors (`mcp/harness_profiles.py`).
[Invariants]
- The profile comes only from an explicit API argument or the server-side
  ``REMOTE_DEV_HARNESS_PROFILE`` environment variable injected by the owning
  plugin; tool arguments never select or override it.
- Canonical ``remote.*`` names stay available in every profile. Native wire
  names are ``remote_<exact native name>`` and are exposed only for their
  bound profile.
- Native schemas keep the local tool's field names, required list, enums,
  defaults, units and bounds. Only endpoint-selector fields are added, plus
  documented canonical-compatibility fields on names that would otherwise
  collide with an existing portable canonical alias.
- Unknown fields and wrongly-typed native values are rejected, never dropped.
[Timeouts & Locks]
- Pure validation and dictionary transformation; no I/O or locks.
[Side-Effect Policy]
- Side-effect free. Dispatch lives in ``mcp/harness_native.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

PROFILE_ENV = "REMOTE_DEV_HARNESS_PROFILE"
CANONICAL_PROFILE = "canonical"
NATIVE_PROFILES = ("codex", "claude", "kimi", "dsh")
PROFILES = (CANONICAL_PROFILE,) + NATIVE_PROFILES


class HarnessProfileError(ValueError):
    """The requested profile is unknown or a native call is not bound to it."""


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
    additional_properties: bool = False
    require_prior_read: bool = False
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def wire(self) -> str:
        return wire_name(self.native_name)


# ---------------------------------------------------------------------------
# Shared normalization helpers
# ---------------------------------------------------------------------------

_ENDPOINT_KEYS = (
    "host", "port", "user", "container", "root", "cwd", "runtime_env",
    "identity_file", "connect_timeout_ms", "runtime_env_file", "alias",
)


def _endpoint_args(args: Mapping[str, Any]) -> dict[str, Any]:
    return {key: args[key] for key in _ENDPOINT_KEYS if key in args}


def _number(value: Any) -> Any:
    """JSON numbers are not booleans; keep integral values integral."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Kimi Code 2.3.1 (binary SHA256 f06ba6a3..., embedded agent-core-v2 sources)
# ---------------------------------------------------------------------------

def _kimi_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["path"]
    line_offset = int(args.get("line_offset", 1))
    out["offset"] = line_offset
    if "n_lines" in args:
        out["limit"] = int(args["n_lines"])
    else:
        # Native Read has no fixed line-count cap; the character budget bounds
        # the page. This transport guard covers the 500000-char native maximum
        # for realistic line lengths.
        out["limit"] = 100000
    if args.get("max_chars") is not None:
        out["max_chars"] = int(args["max_chars"])
    if args.get("column_offset") is not None:
        out["column_offset"] = int(args["column_offset"])
    return out


def _kimi_write(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["path"]
    out["content"] = args["content"]
    mode = str(args.get("mode", "overwrite"))
    out["overwrite"] = mode == "overwrite"
    out["append"] = mode == "append"
    out["create_dirs"] = True
    return out


def _kimi_edit(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["path"]
    out["old_string"] = args["old_string"]
    out["new_string"] = args["new_string"]
    out["replace_all"] = bool(args.get("replace_all", False))
    out["normalize_crlf"] = True
    return out


def _kimi_bash(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["command"] = args["command"]
    background = args.get("run_in_background") is True
    if background and not str(args.get("description") or "").strip():
        raise HarnessProfileError("remote_Bash description is required when run_in_background is true")
    timeout_s = args.get("timeout")
    if timeout_s is None:
        timeout_s = 600 if background else 60
    timeout_s = int(timeout_s)
    cap = 86400 if background else 300
    if timeout_s > cap:
        raise HarnessProfileError(
            f"remote_Bash timeout must be <= {cap}s for {'background' if background else 'foreground'} calls"
        )
    if background:
        if args.get("disable_timeout") is True:
            out.pop("timeout_ms", None)
        else:
            out["timeout_ms"] = timeout_s * 1000
    else:
        # Kimi auto-backgrounds a foreground command that reaches its timeout
        # when task tools are active (remote-dev always has a job supervisor),
        # so the native timeout is a yield/promotion budget, not a kill.
        out["yield_time_ms"] = timeout_s * 1000
    if args.get("description") is not None:
        out["description"] = args["description"]
    if background:
        out["yield_time_ms"] = 0
    return out


def _kimi_glob(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    out["limit"] = int(args.get("head_limit", 100))
    out["offset"] = int(args.get("offset", 0))
    out["respect_gitignore"] = not bool(args.get("include_ignored", False))
    # include_dirs is deprecated and ignored by the native tool.
    return out


def _kimi_grep(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    for key in ("path", "glob", "type"):
        if args.get(key) is not None:
            out[key] = args[key]
    out["output_mode"] = str(args.get("output_mode", "files_with_matches"))
    out["case_insensitive"] = bool(args.get("-i", False))
    out["multiline"] = bool(args.get("multiline", False))
    out["include_ignored"] = bool(args.get("include_ignored", False))
    out["hidden"] = True
    if args.get("-A") is not None:
        out["after_context"] = int(args["-A"])
    if args.get("-B") is not None:
        out["before_context"] = int(args["-B"])
    if args.get("-C") is not None:
        out["context_lines"] = int(args["-C"])
    if args.get("-n") is not None:
        out["line_numbers"] = bool(args["-n"])
    out["offset"] = int(args.get("offset", 0))
    out["limit"] = int(args.get("head_limit", 250))
    return out


KIMI_READ = NativeToolSpec(
    native_name="Read",
    canonical="remote.read",
    description="Read a text file from the local filesystem (Kimi Code Read; max_chars/column_offset pagination).",
    properties={
        "path": {"type": "string", "minLength": 1, "description": "Path to a text file or a kimi-file:// attachment reference in the current session."},
        "line_offset": {"type": "integer", "not": {"const": 0}, "description": "1-based start line; negative reads from the end. Omit to start at line 1."},
        "column_offset": {"type": "integer", "minimum": 0, "description": "Zero-based character offset within the first line of a forward read."},
        "n_lines": {"type": "integer", "minimum": 1, "description": "Number of lines to read. Omit to read toward the end of the file."},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 500000, "description": "Maximum characters in the returned text, including line numbers and status."},
    },
    required=("path",),
    defaults={"max_chars": 100000},
    normalize=_kimi_read,
)

KIMI_WRITE = NativeToolSpec(
    native_name="Write",
    canonical="remote.write",
    description="Create, append to, or replace a file entirely (Kimi Code Write; parents are created).",
    properties={
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
    },
    required=("path", "content"),
    defaults={"mode": "overwrite"},
    require_prior_read=True,
    normalize=_kimi_write,
)

KIMI_EDIT = NativeToolSpec(
    native_name="Edit",
    canonical="remote.edit",
    description="Perform exact replacements in existing files (Kimi Code Edit).",
    properties={
        "path": {"type": "string", "minLength": 1},
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean", "default": False},
    },
    required=("path", "old_string", "new_string"),
    defaults={"replace_all": False},
    require_prior_read=True,
    normalize=_kimi_edit,
)

KIMI_BASH = NativeToolSpec(
    native_name="Bash",
    canonical="remote.bash",
    description="Execute a bash command (Kimi Code Bash; timeout is in seconds).",
    properties={
        "command": {"type": "string", "minLength": 1},
        "cwd": {"type": "string", "description": "Working directory for the command; defaults to the session working directory."},
        "timeout": {"type": "integer", "minimum": 1, "default": 60, "description": "Timeout in seconds; background default 600, background max 86400, foreground max 300."},
        "description": {"type": "string", "description": "Short description; required when run_in_background is true."},
        "run_in_background": {"type": "boolean"},
        "disable_timeout": {"type": "boolean", "description": "Do not apply a timeout; only meaningful with run_in_background=true."},
    },
    required=("command",),
    normalize=_kimi_bash,
)

KIMI_GLOB = NativeToolSpec(
    native_name="Glob",
    canonical="remote.glob",
    description="Find files whose paths match a glob pattern (Kimi Code Glob; head_limit=0 removes the match-count limit).",
    properties={
        "pattern": {"type": "string"},
        "head_limit": {"type": "integer", "minimum": 0, "default": 100, "description": "Maximum paths after offset; 0 removes the match-count limit."},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "path": {"type": "string"},
        "include_ignored": {"type": "boolean", "default": False},
        "include_dirs": {"type": "boolean", "description": "Deprecated and ignored; results are always files-only."},
    },
    required=("pattern",),
    defaults={"head_limit": 100, "offset": 0},
    normalize=_kimi_glob,
)

KIMI_GREP = NativeToolSpec(
    native_name="Grep",
    canonical="remote.grep",
    description="Search file contents using a ripgrep regular expression (Kimi Code Grep; head_limit=0 removes the match-count limit).",
    properties={
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "glob": {"type": "string"},
        "type": {"type": "string"},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count_matches"], "default": "files_with_matches"},
        "-i": {"type": "boolean", "default": False},
        "-n": {"type": "boolean", "default": True},
        "-A": {"type": "integer", "minimum": 0},
        "-B": {"type": "integer", "minimum": 0},
        "-C": {"type": "integer", "minimum": 0},
        "head_limit": {"type": "integer", "minimum": 0, "default": 250},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "multiline": {"type": "boolean", "default": False},
        "include_ignored": {"type": "boolean", "default": False},
    },
    required=("pattern",),
    defaults={"output_mode": "files_with_matches", "head_limit": 250, "offset": 0},
    normalize=_kimi_grep,
)

KIMI_READ_MEDIA = NativeToolSpec(
    native_name="ReadMediaFile",
    canonical="remote.read_image",
    description="Read an image file and return the image itself (Kimi Code ReadMediaFile; region crops are not implemented remotely).",
    properties={
        "path": {"type": "string", "minLength": 1},
        "region": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
            },
            "required": ["x", "y", "width", "height"],
            "additionalProperties": False,
        },
        "full_resolution": {"type": "boolean", "default": False},
    },
    required=("path",),
    handler="kimi_read_media",
    normalize=lambda args: {"file_path": args["path"], **_endpoint_args(args)},
)

KIMI_TASK_OUTPUT = NativeToolSpec(
    native_name="TaskOutput",
    canonical="remote.job_status",
    description="Retrieve a non-blocking snapshot of a running or completed background task (Kimi Code TaskOutput).",
    properties={"task_id": {"type": "string", "minLength": 1}},
    required=("task_id",),
    handler="kimi_task_output",
    normalize=lambda args: {"job_id": args["task_id"], **_endpoint_args(args)},
)

KIMI_TASK_STOP = NativeToolSpec(
    native_name="TaskStop",
    canonical="remote.job_stop",
    description="Stop a running background task (Kimi Code TaskStop).",
    properties={
        "task_id": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "default": "Stopped by TaskStop"},
    },
    required=("task_id",),
    defaults={"reason": "Stopped by TaskStop"},
    handler="kimi_task_stop",
    normalize=lambda args: {"job_id": args["task_id"], **_endpoint_args(args)},
)

KIMI_TASK_LIST = NativeToolSpec(
    native_name="TaskList",
    canonical="remote.job_status",
    description="List background tasks and their current status (Kimi Code TaskList).",
    properties={
        "active_only": {"type": "boolean", "default": True},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    defaults={"active_only": True, "limit": 20},
    handler="kimi_task_list",
    normalize=lambda args: dict(args),
)


NATIVE_TOOLS: dict[str, dict[str, NativeToolSpec]] = {profile: {} for profile in NATIVE_PROFILES}
NATIVE_TOOLS["kimi"] = {
    spec.native_name: spec
    for spec in (
        KIMI_READ, KIMI_WRITE, KIMI_EDIT, KIMI_BASH, KIMI_GLOB, KIMI_GREP,
        KIMI_TASK_OUTPUT, KIMI_TASK_STOP, KIMI_TASK_LIST, KIMI_READ_MEDIA,
    )
}


def specs_for(profile: str) -> dict[str, NativeToolSpec]:
    return NATIVE_TOOLS.get(profile, {})


def spec_by_wire(profile: str, name: str) -> NativeToolSpec | None:
    for spec in specs_for(profile).values():
        if spec.wire == name:
            return spec
    return None


def spec_by_wire_any_profile(name: str) -> tuple[str, NativeToolSpec] | None:
    for profile in NATIVE_PROFILES:
        spec = spec_by_wire(profile, name)
        if spec is not None:
            return profile, spec
    return None


def native_input_schema(spec: NativeToolSpec, endpoint_props: Mapping[str, Any]) -> dict[str, Any]:
    # Compatibility fields are accepted by the server but are not advertised
    # on the native wire schema. Only the native fields and the explicit remote
    # target fields belong in tools/list.
    return {
        "type": "object",
        "properties": {**endpoint_props, **spec.properties},
        "required": list(spec.required),
        "additionalProperties": bool(spec.additional_properties),
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
    allowed = set(spec.properties) | set(spec.compat_properties) | set(allowed_endpoint)
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
            raise HarnessProfileError(f"{spec.wire} requires {key}")
    for key, value in args.items():
        schema = spec.properties.get(key)
        if schema is None:
            schema = spec.compat_properties.get(key)
        if schema is not None:
            _validate_value(value, schema, key)


# ---------------------------------------------------------------------------
# DeepSeek Harness 0.2.0-rc.2 (installed @deepseek-ai/dsh-tool-* sources)
# ---------------------------------------------------------------------------

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


def _dsh_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["offset"] = int(args.get("offset", 1))
    out["limit"] = int(args.get("limit", 2000))
    out["strict_offset"] = True
    return out


def _dsh_write(args: dict[str, Any]) -> dict[str, Any]:
    _reject_sandbox_escalation(args)
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["content"] = args["content"]
    out["overwrite"] = True
    out["create_dirs"] = True
    return out


def _dsh_edit(args: dict[str, Any]) -> dict[str, Any]:
    _reject_sandbox_escalation(args)
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["old_string"] = args["old_string"]
    out["new_string"] = args["new_string"]
    out["replace_all"] = bool(args.get("replace_all", False))
    out["normalize_crlf"] = True
    return out


def _dsh_bash(args: dict[str, Any]) -> dict[str, Any]:
    _reject_sandbox_escalation(args)
    out = _endpoint_args(args)
    out["command"] = args["command"]
    out["description"] = args["description"]
    if args.get("workdir") is not None:
        out["cwd"] = args["workdir"]
    if args.get("run_in_background") is True:
        out["yield_time_ms"] = 0
    else:
        # DSH semantics: timeoutMs defaults to 120000, and on expiry the
        # foreground command is promoted to a background job, not killed. The
        # canonical yield budget produces that hand-off; the execution
        # deadline stays unset.
        timeout_ms = int(args.get("timeoutMs", 120000))
        if timeout_ms > 600000:
            raise HarnessProfileError("remote_bash timeoutMs must be <= 600000")
        out["yield_time_ms"] = timeout_ms
    return out


def _dsh_glob(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    out["limit"] = 100
    out["respect_gitignore"] = False
    out["excludes"] = [".git", "**/.git", ".hg", "**/.hg", ".svn", "**/.svn"]
    return out


def _dsh_grep(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    if args.get("include") is not None:
        out["glob"] = args["include"]
    out["output_mode"] = "content"
    out["line_numbers"] = True
    out["limit"] = 250
    return out


DSH_READ = NativeToolSpec(
    native_name="read",
    canonical="remote.read",
    description="Read a UTF-8 text file and return line-numbered content (DSH read; default/max 2000 lines).",
    properties={
        "file_path": {"type": "string", "minLength": 1},
        "offset": {"type": "number", "minimum": 1, "default": 1},
        "limit": {"type": "number", "minimum": 1, "maximum": 2000, "default": 2000},
    },
    required=("file_path",),
    defaults={"offset": 1, "limit": 2000},
    compat_properties={
        "path": {"type": "string"},
        "verify_content": {"type": "boolean"},
        "allow_symlink": {"type": "boolean"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "content_offset": {"type": "integer"},
        "outline": {"type": "boolean"},
        "item_offset": {"type": "integer"},
        "node_paths": {"type": "array", "items": {"type": "string"}},
        "client_context_id": {"type": "string"},
    },
    additional_properties=True,
    normalize=_dsh_read,
)

DSH_WRITE = NativeToolSpec(
    native_name="write",
    canonical="remote.write",
    description="Create or fully replace a UTF-8 text file (DSH write; observation policy is local to the host).",
    properties={
        "file_path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
    },
    required=("file_path", "content"),
    compat_properties={
        "path": {"type": "string"},
        "overwrite": {"type": "boolean"},
        "append": {"type": "boolean"},
        "mode": {"type": "string"},
        "create_dirs": {"type": "boolean"},
        "description": {"type": "string"},
        "client_context_id": {"type": "string"},
    },
    additional_properties=True,
    require_prior_read=True,
    normalize=_dsh_write,
)

DSH_EDIT = NativeToolSpec(
    native_name="edit",
    canonical="remote.edit",
    description="Edit an existing UTF-8 text file by replacing literal text (DSH edit).",
    properties={
        "file_path": {"type": "string", "minLength": 1},
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean", "default": False},
    },
    required=("file_path", "old_string", "new_string"),
    defaults={"replace_all": False},
    compat_properties={
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "instruction": {"type": "string"},
        "description": {"type": "string"},
        "client_context_id": {"type": "string"},
    },
    additional_properties=True,
    require_prior_read=True,
    normalize=_dsh_edit,
)

DSH_BASH = NativeToolSpec(
    native_name="bash",
    canonical="remote.bash",
    description="Execute a bash command (DSH bash; timeoutMs promotes a foreground command to a background job).",
    properties={
        "command": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "timeoutMs": {"type": "number", "minimum": 1},
        "workdir": {"type": "string"},
        "run_in_background": {"type": "boolean"},
    },
    required=("command", "description"),
    compat_properties={
        "cmd": {"type": "string"},
        "timeout_ms": {"type": "integer"},
        "yield_time_ms": {"type": "integer"},
        "max_output_tokens": {"type": "integer"},
        "env": {"type": "object"},
        "tty": {"type": "boolean"},
    },
    additional_properties=True,
    handler="dsh_bash",
    normalize=_dsh_bash,
)

DSH_GLOB = NativeToolSpec(
    native_name="glob",
    canonical="remote.glob",
    description="Find files by path pattern, including hidden and ignored files (DSH glob).",
    properties={
        "pattern": {"type": "string", "minLength": 1},
        "path": {"type": "string"},
    },
    required=("pattern",),
    compat_properties={
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "respect_gitignore": {"type": "boolean"},
        "include_ignored": {"type": "boolean"},
        "extensions": {"type": "array", "items": {"type": "string"}},
        "excludes": {"type": "array", "items": {"type": "string"}},
        "entry_type": {"type": "string"},
        "max_depth": {"type": "integer"},
        "full_path": {"type": "boolean"},
    },
    additional_properties=True,
    normalize=_dsh_glob,
)

DSH_GREP = NativeToolSpec(
    native_name="grep",
    canonical="remote.grep",
    description="Search file contents with a ripgrep regular expression (DSH grep; matching lines with line numbers).",
    properties={
        "pattern": {"type": "string", "minLength": 1},
        "path": {"type": "string"},
        "include": {"type": "string"},
    },
    required=("pattern",),
    compat_properties={
        "glob": {"type": "string"},
        "includes": {"type": "array", "items": {"type": "string"}},
        "excludes": {"type": "array", "items": {"type": "string"}},
        "type": {"type": "string"},
        "output_mode": {"type": "string"},
        "multiline": {"type": "boolean"},
        "case_insensitive": {"type": "boolean"},
        "fixed_strings": {"type": "boolean"},
        "before_context": {"type": "integer"},
        "after_context": {"type": "integer"},
        "context_lines": {"type": "integer"},
        "line_numbers": {"type": "boolean"},
        "include_ignored": {"type": "boolean"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    },
    additional_properties=True,
    normalize=_dsh_grep,
)

DSH_JOB_OUTPUT = NativeToolSpec(
    native_name="job_output",
    canonical="remote.job_output",
    description="Read a background job: output since the previous read, optionally waiting for completion (DSH job_output).",
    properties={
        "job_id": {"type": "string", "minLength": 1},
        "wait": {"type": "boolean", "default": False},
        "timeout_ms": {"type": "number", "minimum": 1, "description": "Max wait with wait=true; default 30000, cap 600000."},
    },
    required=("job_id",),
    defaults={"wait": False},
    handler="dsh_job_output",
    compat_properties={
        "lines": {"type": "integer"},
        "stream": {"type": "string"},
        "yield_time_ms": {"type": "integer"},
        "max_output_tokens": {"type": "integer"},
        "session_id": {"type": "string"},
    },
    additional_properties=True,
    normalize=lambda args: {"job_id": args["job_id"], **_endpoint_args(args)},
)

DSH_JOB_LIST = NativeToolSpec(
    native_name="job_list",
    canonical="remote.job_status",
    description="List your background jobs (running and finished) with ids, kinds, and statuses (DSH job_list).",
    properties={},
    handler="dsh_job_list",
    additional_properties=True,
    normalize=lambda args: dict(args),
)

DSH_JOB_KILL = NativeToolSpec(
    native_name="job_kill",
    canonical="remote.job_stop",
    description="Request cancellation of a running background job (DSH job_kill).",
    properties={
        "job_id": {"type": "string", "minLength": 1},
        "reason": {"type": "string"},
    },
    required=("job_id",),
    handler="dsh_job_kill",
    additional_properties=True,
    normalize=lambda args: {"job_id": args["job_id"], **_endpoint_args(args)},
)


# ---------------------------------------------------------------------------
# Claude Code 2.1.269 (installed Bun bundle, source symbols at 160-171 MB)
# ---------------------------------------------------------------------------

def _claude_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["offset"] = int(args.get("offset") or 1)
    out["limit"] = int(args.get("limit") or 2000)
    if args.get("pages") is not None:
        out["pages"] = str(args["pages"])
    return out


def _claude_write(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["content"] = args["content"]
    out["overwrite"] = True
    out["create_dirs"] = True
    return out


def _claude_edit(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["old_string"] = args["old_string"]
    out["new_string"] = args["new_string"]
    out["replace_all"] = bool(args.get("replace_all", False))
    out["normalize_crlf"] = True
    return out


def _claude_bash(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("dangerouslyDisableSandbox") is not None:
        raise HarnessProfileError(
            "dangerouslyDisableSandbox is a local host sandbox override; remote-dev does not grant remote privileges"
        )
    out = _endpoint_args(args)
    out["command"] = args["command"]
    if args.get("description") is not None:
        out["description"] = args["description"]
    if args.get("run_in_background") is True:
        out["yield_time_ms"] = 0
    else:
        timeout = int(args.get("timeout", 120000))
        if timeout > 600000:
            raise HarnessProfileError("remote_Bash timeout must be <= 600000 ms")
        # Claude auto-backgrounds a command that outlives its timeout.
        out["yield_time_ms"] = timeout
    return out


def _claude_glob(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    out["limit"] = 100
    out["respect_gitignore"] = False
    out["excludes"] = [".git", "**/.git", ".hg", "**/.hg", ".svn", "**/.svn", ".bzr", "**/.bzr", ".jj", "**/.jj", ".sl", "**/.sl"]
    return out


def _claude_grep(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    for key in ("path", "glob", "type"):
        if args.get(key) is not None:
            out[key] = args[key]
    out["output_mode"] = str(args.get("output_mode", "files_with_matches"))
    out["case_insensitive"] = bool(args.get("-i", False))
    out["multiline"] = bool(args.get("multiline", False))
    out["only_matching"] = bool(args.get("-o", False))
    if args.get("-n") is not None:
        out["line_numbers"] = bool(args["-n"])
    if args.get("-C") is not None:
        out["context_lines"] = int(args["-C"])
    elif args.get("context") is not None:
        out["context_lines"] = int(args["context"])
    if args.get("-B") is not None:
        out["before_context"] = int(args["-B"])
    if args.get("-A") is not None:
        out["after_context"] = int(args["-A"])
    out["offset"] = int(args.get("offset", 0))
    out["limit"] = int(args.get("head_limit", 250))
    return out


def _claude_task_output(args: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": args["task_id"], **_endpoint_args(args)}


def _claude_task_stop(args: dict[str, Any]) -> dict[str, Any]:
    job_id = args.get("task_id") or args.get("shell_id")
    if not job_id:
        raise HarnessProfileError("remote_TaskStop requires task_id (or deprecated shell_id)")
    return {"job_id": job_id, **_endpoint_args(args)}


CLAUDE_READ = NativeToolSpec(
    native_name="Read",
    canonical="remote.read",
    description="Read a file from the local filesystem (Claude Code Read; text, image, PDF page range, or notebook).",
    properties={
        "file_path": {"type": "string", "description": "The absolute path to the file to read."},
        "offset": {"type": "integer", "minimum": 0, "description": "The line number to start reading from."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "The number of lines to read."},
        "pages": {"type": "string", "description": "Page range for PDF files (e.g. 1-5, max 20 pages); remote-dev does not render PDF pages."},
    },
    required=("file_path",),
    handler="claude_read",
    normalize=_claude_read,
)

CLAUDE_WRITE = NativeToolSpec(
    native_name="Write",
    canonical="remote.write",
    description="Write a file to the local filesystem (Claude Code Write; requires a prior Read of an existing file).",
    properties={
        "file_path": {"type": "string", "description": "The absolute path to the file to write (must be absolute, not relative)."},
        "content": {"type": "string", "description": "The content to write to the file."},
    },
    required=("file_path", "content"),
    require_prior_read=True,
    normalize=_claude_write,
)

CLAUDE_EDIT = NativeToolSpec(
    native_name="Edit",
    canonical="remote.edit",
    description="Edit a file with exact string replacement (Claude Code Edit; requires a prior Read).",
    properties={
        "file_path": {"type": "string", "description": "The absolute path to the file to modify."},
        "old_string": {"type": "string", "description": "The text to replace."},
        "new_string": {"type": "string", "description": "The text to replace it with (must be different from old_string)."},
        "replace_all": {"type": "boolean", "default": False, "description": "Replace all occurrences (default false)."},
    },
    required=("file_path", "old_string", "new_string"),
    defaults={"replace_all": False},
    require_prior_read=True,
    normalize=_claude_edit,
)

CLAUDE_BASH = NativeToolSpec(
    native_name="Bash",
    canonical="remote.bash",
    description="Run a shell command (Claude Code Bash; timeout in milliseconds, 120000 default, 600000 max).",
    properties={
        "command": {"type": "string", "minLength": 1, "description": "The command to execute."},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 600000, "description": "Optional timeout in milliseconds (max 600000; default 120000)."},
        "description": {"type": "string", "description": "Clear, concise description of what this command does in active voice."},
        "run_in_background": {"type": "boolean", "description": "Set to true to run this command in the background."},
        "dangerouslyDisableSandbox": {"type": "boolean", "description": "Local host sandbox override; rejected for remote calls."},
    },
    required=("command",),
    normalize=_claude_bash,
)

CLAUDE_GLOB = NativeToolSpec(
    native_name="Glob",
    canonical="remote.glob",
    description="Find files by glob pattern (Claude Code Glob; up to 100 paths, hidden and ignored files included).",
    properties={
        "pattern": {"type": "string", "description": "The glob pattern to match files against."},
        "path": {"type": "string", "description": "The directory to search in; defaults to the working directory."},
    },
    required=("pattern",),
    normalize=_claude_glob,
)

CLAUDE_GREP = NativeToolSpec(
    native_name="Grep",
    canonical="remote.grep",
    description="Search file contents with a ripgrep regular expression (Claude Code Grep).",
    properties={
        "pattern": {"type": "string", "description": "The regular expression pattern to search for in file contents."},
        "path": {"type": "string", "description": "File or directory to search in (rg PATH)."},
        "glob": {"type": "string", "description": "Glob pattern to filter files."},
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "files_with_matches"},
        "-B": {"type": "integer", "minimum": 0},
        "-A": {"type": "integer", "minimum": 0},
        "-C": {"type": "integer", "minimum": 0},
        "context": {"type": "integer", "minimum": 0},
        "-n": {"type": "boolean", "default": True},
        "-i": {"type": "boolean"},
        "-o": {"type": "boolean", "default": False},
        "type": {"type": "string"},
        "head_limit": {"type": "integer", "minimum": 0, "default": 250},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "multiline": {"type": "boolean", "default": False},
    },
    required=("pattern",),
    defaults={"output_mode": "files_with_matches", "head_limit": 250, "offset": 0},
    normalize=_claude_grep,
)

CLAUDE_TASK_OUTPUT = NativeToolSpec(
    native_name="TaskOutput",
    canonical="remote.job_output",
    description="Retrieve output from a running or completed task (Claude Code TaskOutput; block defaults true).",
    properties={
        "task_id": {"type": "string", "description": "The task ID to get output from."},
        "block": {"type": "boolean", "default": True, "description": "Whether to wait for completion."},
        "timeout": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 30000, "description": "Max wait time in ms."},
    },
    required=("task_id",),
    defaults={"block": True, "timeout": 30000},
    handler="claude_task_output",
    normalize=_claude_task_output,
)

CLAUDE_BASH_OUTPUT = NativeToolSpec(
    native_name="BashOutput",
    canonical="remote.job_output",
    description="Alias of Claude Code TaskOutput in 2.1.269 (task_id/block/timeout).",
    properties=CLAUDE_TASK_OUTPUT.properties,
    required=("task_id",),
    defaults={"block": True, "timeout": 30000},
    handler="claude_task_output",
    normalize=_claude_task_output,
)

CLAUDE_NOTEBOOK_EDIT = NativeToolSpec(
    native_name="NotebookEdit",
    canonical="remote.write",
    description="Edit .ipynb cells (Claude Code NotebookEdit; replace/insert/delete).",
    properties={
        "notebook_path": {"type": "string", "minLength": 1, "description": "The absolute path to the Jupyter notebook file."},
        "new_source": {"type": "string", "description": "The new source for the cell."},
        "cell_id": {"type": "string", "description": "The ID of the cell to edit."},
        "cell_type": {"type": "string", "enum": ["code", "markdown"], "description": "The type of the cell."},
        "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"], "default": "replace"},
    },
    required=("notebook_path", "new_source"),
    defaults={"edit_mode": "replace"},
    require_prior_read=True,
    handler="claude_notebook_edit",
    normalize=lambda args: {"file_path": args["notebook_path"], **_endpoint_args(args)},
)

CLAUDE_TASK_LIST = NativeToolSpec(
    native_name="TaskList",
    canonical="remote.job_status",
    description="List the session task list (Claude Code TaskList; remote jobs are the remote task registry).",
    properties={},
    handler="claude_task_list",
    normalize=lambda args: dict(args),
)

CLAUDE_TASK_STOP = NativeToolSpec(
    native_name="TaskStop",
    canonical="remote.job_stop",
    description="Stop a running background task by ID (Claude Code TaskStop).",
    properties={
        "task_id": {"type": "string", "description": "The ID of the background task to stop."},
        "shell_id": {"type": "string", "description": "Deprecated: use task_id instead."},
    },
    handler="claude_task_stop",
    normalize=_claude_task_stop,
)

CLAUDE_KILL_SHELL = NativeToolSpec(
    native_name="KillShell",
    canonical="remote.job_stop",
    description="Alias of Claude Code TaskStop (task_id/shell_id).",
    properties=CLAUDE_TASK_STOP.properties,
    handler="claude_task_stop",
    normalize=_claude_task_stop,
)


# ---------------------------------------------------------------------------
# Codex CLI 0.153.4 (installed binary + rust-v0.153.4 shell_spec.rs)
# ---------------------------------------------------------------------------

_CODEX_SANDBOX_VALUES = ["use_default", "require_escalated"]


def _codex_permissions(args: Mapping[str, Any]) -> None:
    mode = args.get("sandbox_permissions")
    if mode is not None and mode != "use_default":
        raise HarnessProfileError(
            "sandbox_permissions other than use_default is a local host approval control; "
            "remote-dev does not grant remote privileges"
        )
    for key in ("additional_permissions", "justification", "prefix_rule"):
        if args.get(key) is not None:
            raise HarnessProfileError(
                f"{key} is a local host approval control; remote-dev does not grant remote privileges"
            )


def _codex_exec(args: dict[str, Any]) -> dict[str, Any]:
    _codex_permissions(args)
    if args.get("environment_id") is not None:
        raise HarnessProfileError("remote_exec_command supports only the primary environment; omit environment_id")
    if args.get("shell") is not None or args.get("login") is not None:
        raise HarnessProfileError(
            "remote_exec_command does not accept host-local shell/login selection; "
            "the remote endpoint's configured shell is used"
        )
    out = _endpoint_args(args)
    out["command"] = args["cmd"]
    if args.get("workdir") is not None:
        out["cwd"] = args["workdir"]
    out["tty"] = bool(args.get("tty", False))
    yield_ms = int(args.get("yield_time_ms", 10000))
    out["yield_time_ms"] = max(250, min(yield_ms, 30000))
    out["max_output_tokens"] = int(args.get("max_output_tokens", 10000))
    out["description"] = "Codex exec_command"
    return out


def _codex_write_stdin(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["job_id"] = str(args["session_id"])
    out["chars"] = args.get("chars", "")
    if args.get("yield_time_ms") is not None:
        out["yield_time_ms"] = int(args["yield_time_ms"])
    else:
        out["yield_time_ms"] = 5000 if not out["chars"] else 250
    if args.get("max_output_tokens") is not None:
        out["max_output_tokens"] = int(args["max_output_tokens"])
    return out


CODEX_EXEC = NativeToolSpec(
    native_name="exec_command",
    canonical="remote.bash",
    description="Runs a command in a PTY, returning output or a session ID for ongoing interaction (Codex exec_command).",
    properties={
        "cmd": {"type": "string", "minLength": 1, "description": "Shell command to execute."},
        "workdir": {"type": "string", "description": "Working directory for the command. Defaults to the turn cwd."},
        "tty": {"type": "boolean", "default": False, "description": "True allocates a PTY; false or omitted uses plain pipes."},
        "yield_time_ms": {"type": "number", "default": 10000, "description": "Wait before yielding output. Defaults to 10000 ms; effective range 250-30000 ms."},
        "max_output_tokens": {"type": "number", "description": "Output token budget. Defaults to 10000 tokens."},
        "shell": {"type": "string", "description": "Host-local shell binary; rejected for remote calls."},
        "login": {"type": "boolean", "description": "Host-local login-shell selection; rejected for remote calls."},
        "environment_id": {"type": "string", "description": "Environment id; remote-dev supports only the primary environment."},
        "sandbox_permissions": {"type": "string", "enum": _CODEX_SANDBOX_VALUES},
        "justification": {"type": "string"},
        "prefix_rule": {"type": "array", "items": {"type": "string"}},
    },
    required=("cmd",),
    defaults={"tty": False, "yield_time_ms": 10000},
    handler="codex_exec",
    compat_properties={
        "timeout_ms": {"type": "integer"},
        "timeout": {"type": "integer"},
    },
    normalize=_codex_exec,
)

CODEX_WRITE_STDIN = NativeToolSpec(
    native_name="write_stdin",
    canonical="remote.job_stdin",
    description="Writes characters to an existing unified exec session and returns recent output (Codex write_stdin).",
    properties={
        "session_id": {"type": "number", "description": "Identifier of the running unified exec session."},
        "chars": {"type": "string", "description": "Bytes to write to stdin. Defaults to empty, which polls without writing."},
        "yield_time_ms": {"type": "number", "description": "Wait before yielding output. Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default."},
        "max_output_tokens": {"type": "number", "description": "Output token budget. Defaults to 10000 tokens."},
    },
    required=("session_id",),
    handler="codex_write_stdin",
    compat_properties={
        "eof": {"type": "boolean"},
        "timeout_ms": {"type": "integer"},
    },
    normalize=_codex_write_stdin,
)


CODEX_VIEW_IMAGE = NativeToolSpec(
    native_name="view_image",
    canonical="remote.read_image",
    description="View a local image file from the filesystem when visual inspection is needed (Codex view_image).",
    properties={
        "path": {"type": "string", "minLength": 1, "description": "Local filesystem path to an image file."},
    },
    required=("path",),
    handler="codex_view_image",
    normalize=lambda args: {"file_path": args["path"], **_endpoint_args(args)},
)

CODEX_APPLY_PATCH = NativeToolSpec(
    native_name="apply_patch",
    canonical="remote.apply_patch",
    description="Apply a Codex apply_patch payload. Native apply_patch is freeform; over MCP the raw patch text is passed in `patch`.",
    properties={
        "patch": {"type": "string", "minLength": 1, "description": "Raw *** Begin Patch ... *** End Patch text."},
    },
    required=("patch",),
    compat_properties={
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout_ms": {"type": "integer"},
    },
    normalize=lambda args: {"patch": args["patch"], **_endpoint_args(args)},
)

NATIVE_TOOLS["codex"] = {
    spec.native_name: spec for spec in (CODEX_EXEC, CODEX_WRITE_STDIN, CODEX_VIEW_IMAGE, CODEX_APPLY_PATCH)
}

NATIVE_TOOLS["claude"] = {
    spec.native_name: spec
    for spec in (
        CLAUDE_READ, CLAUDE_WRITE, CLAUDE_EDIT, CLAUDE_BASH, CLAUDE_GLOB, CLAUDE_GREP,
        CLAUDE_TASK_OUTPUT, CLAUDE_BASH_OUTPUT, CLAUDE_NOTEBOOK_EDIT, CLAUDE_TASK_LIST,
        CLAUDE_TASK_STOP, CLAUDE_KILL_SHELL,
    )
}

DSH_READ_IMAGE = NativeToolSpec(
    native_name="read_image",
    canonical="remote.read_image",
    description="Read a PNG/JPEG/WebP/GIF file and return the image itself (DSH read_image).",
    properties={
        "file_path": {"type": "string", "minLength": 1, "description": "Path to the image file, resolved by the filesystem backend."},
    },
    required=("file_path",),
    handler="dsh_read_image",
    additional_properties=True,
    normalize=lambda args: {"file_path": args["file_path"], **_endpoint_args(args)},
)

NATIVE_TOOLS["dsh"] = {
    spec.native_name: spec
    for spec in (
        DSH_READ, DSH_WRITE, DSH_EDIT, DSH_BASH, DSH_GLOB, DSH_GREP,
        DSH_JOB_OUTPUT, DSH_JOB_LIST, DSH_JOB_KILL, DSH_READ_IMAGE,
    )
}
