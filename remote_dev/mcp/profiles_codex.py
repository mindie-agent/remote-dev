"""Native codex tool descriptors and argument normalizers.

Schemas/defaults/units are derived from the installed codex artifact; the MCP
profile validator remains strict and no canonical-compat bypass is applied.
"""
from __future__ import annotations

from typing import Any, Mapping

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args
from .native_spec import _number  # noqa: F401 - shared helper for host specs

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
    if args.get("environment_id") not in (None, "primary"):
        raise HarnessProfileError(
            "remote_exec_command supports only the primary remote endpoint; "
            "host-local environment_id selection is not replicated"
        )
    out = _endpoint_args(args)
    out["command"] = args["cmd"]
    if args.get("shell") is not None:
        out["shell"] = str(args["shell"])
    out["login"] = bool(args.get("login", True))
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
        "cmd": {"type": "string", "description": "Shell command to execute."},
        "workdir": {"type": "string", "description": "Working directory for the command. Defaults to the turn cwd."},
        "tty": {"type": "boolean", "description": "True allocates a PTY; false or omitted uses plain pipes."},
        "yield_time_ms": {"type": "number", "description": "Wait before yielding output. Defaults to 10000 ms; effective range 250-30000 ms."},
        "max_output_tokens": {"type": "number", "description": "Output token budget. Defaults to 10000 tokens."},
        "shell": {"type": "string", "description": "Shell binary executed on the remote endpoint; omitted uses bash."},
        "login": {"type": "boolean", "description": "Run the selected remote shell with -l; native default true."},
        "environment_id": {"type": "string", "description": "Environment id; remote-dev supports only the primary environment."},
        "sandbox_permissions": {"type": "string", "enum": _CODEX_SANDBOX_VALUES},
        "justification": {"type": "string"},
        "prefix_rule": {"type": "array", "items": {"type": "string"}},
    },
    required=("cmd",),
    defaults={"tty": False, "yield_time_ms": 10000, "max_output_tokens": 10000, "login": True, "sandbox_permissions": "use_default"},
    handler="codex_exec",
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
        "path": {"type": "string", "description": "Local filesystem path to an image file."},
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
        "patch": {"type": "string", "description": "Raw *** Begin Patch ... *** End Patch text."},
    },
    required=("patch",),
    normalize=lambda args: {"patch": args["patch"], **_endpoint_args(args)},
)


SPECS = {
    spec.native_name: spec
    for spec in (
        CODEX_EXEC,
        CODEX_WRITE_STDIN,
        CODEX_VIEW_IMAGE,
        CODEX_APPLY_PATCH
    )
}
