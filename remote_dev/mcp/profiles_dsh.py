"""Native dsh tool descriptors and argument normalizers.

Schemas/defaults/units are derived from the installed dsh artifact; the MCP
profile validator remains strict and no canonical-compat bypass is applied.
"""
from __future__ import annotations

from typing import Any, Mapping

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args
from .native_spec import _number  # noqa: F401 - shared helper for host specs

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
    limit = int(args.get("limit", 2000))
    if limit > 2000:
        raise HarnessProfileError("remote_read limit must be <= 2000")
    out["limit"] = limit
    out["strict_offset"] = True
    out["max_chars"] = 51200
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
        if timeout_ms <= 0 or timeout_ms > 600000:
            raise HarnessProfileError("remote_bash timeoutMs must be positive and <= 600000")
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
        "file_path": {"type": "string"},
        "offset": {"type": "number"},
        "limit": {"type": "number"},
    },
    additional_properties=None,
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
    normalize=_dsh_read,
)

DSH_WRITE = NativeToolSpec(
    native_name="write",
    canonical="remote.write",
    description="Create or fully replace a UTF-8 text file (DSH write; observation policy is local to the host).",
    properties={
        "file_path": {"type": "string"},
        "content": {"type": "string"},
    },
    additional_properties=None,
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
    require_prior_read=True,
    normalize=_dsh_write,
)

DSH_EDIT = NativeToolSpec(
    native_name="edit",
    canonical="remote.edit",
    description="Edit an existing UTF-8 text file by replacing literal text (DSH edit).",
    properties={
        "file_path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    additional_properties=None,
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
    require_prior_read=True,
    normalize=_dsh_edit,
)

DSH_BASH = NativeToolSpec(
    native_name="bash",
    canonical="remote.bash",
    description="Execute a bash command (DSH bash; timeoutMs promotes a foreground command to a background job).",
    properties={
        "command": {"type": "string"},
        "description": {"type": "string"},
        "timeoutMs": {"type": "number"},
        "workdir": {"type": "string"},
        "run_in_background": {"type": "boolean"},
    },
    additional_properties=None,
    required=("command", "description"),
    compat_properties={
        "cmd": {"type": "string"},
        "timeout_ms": {"type": "integer"},
        "yield_time_ms": {"type": "integer"},
        "max_output_tokens": {"type": "integer"},
        "env": {"type": "object"},
        "tty": {"type": "boolean"},
    },
    handler="dsh_bash",
    normalize=_dsh_bash,
)

DSH_GLOB = NativeToolSpec(
    native_name="glob",
    canonical="remote.glob",
    description="Find files by path pattern, including hidden and ignored files (DSH glob).",
    properties={
        "pattern": {"type": "string"},
        "path": {"type": "string"},
    },
    additional_properties=None,
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
    normalize=_dsh_glob,
)

DSH_GREP = NativeToolSpec(
    native_name="grep",
    canonical="remote.grep",
    description="Search file contents with a ripgrep regular expression (DSH grep; matching lines with line numbers).",
    properties={
        "pattern": {"type": "string"},
        "path": {"type": "string"},
        "include": {"type": "string"},
    },
    additional_properties=None,
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
    normalize=_dsh_grep,
)

DSH_JOB_OUTPUT = NativeToolSpec(
    native_name="job_output",
    canonical="remote.job_output",
    description="Read a background job: output since the previous read, optionally waiting for completion (DSH job_output).",
    properties={
        "job_id": {"type": "string"},
        "wait": {"type": "boolean"},
        "timeout_ms": {"type": "number", "description": "Max wait with wait=true; default 30000, cap 600000."},
    },
    additional_properties=None,
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
    normalize=lambda args: {"job_id": args["job_id"], **_endpoint_args(args)},
)

DSH_JOB_LIST = NativeToolSpec(
    native_name="job_list",
    canonical="remote.job_status",
    description="List your background jobs (running and finished) with ids, kinds, and statuses (DSH job_list).",
    properties={},
    additional_properties=None,
    handler="dsh_job_list",
    normalize=lambda args: dict(args),
)

DSH_JOB_KILL = NativeToolSpec(
    native_name="job_kill",
    canonical="remote.job_stop",
    description="Request cancellation of a running background job (DSH job_kill).",
    properties={
        "job_id": {"type": "string"},
        "reason": {"type": "string"},
    },
    additional_properties=None,
    required=("job_id",),
    handler="dsh_job_kill",
    normalize=lambda args: {"job_id": args["job_id"], **_endpoint_args(args)},
)

DSH_READ_IMAGE = NativeToolSpec(
    native_name="read_image",
    canonical="remote.read_image",
    description="Read a PNG/JPEG/WebP/GIF file and return the image itself (DSH read_image).",
    properties={
        "file_path": {"type": "string", "description": "Path to the image file, resolved by the filesystem backend."},
    },
    additional_properties=None,
    required=("file_path",),
    handler="dsh_read_image",
    normalize=lambda args: {"file_path": args["file_path"], **_endpoint_args(args)},
)


SPECS = {
    spec.native_name: spec
    for spec in (
        DSH_READ,
        DSH_WRITE,
        DSH_EDIT,
        DSH_BASH,
        DSH_GLOB,
        DSH_GREP,
        DSH_JOB_OUTPUT,
        DSH_JOB_LIST,
        DSH_JOB_KILL,
        DSH_READ_IMAGE
    )
}
