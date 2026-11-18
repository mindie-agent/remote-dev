"""Native OpenCode (oc) tool descriptors and argument normalizers.

[Role]
Translate the native OpenCode tool contract into canonical remote operations.
[Invariants]
Preserve both OpenCode spellings (`filePath` and `file_path`), the Write
`overwrite=True` default, and milliseconds for `bash.timeout`; validation stays
strict and never treats local approval controls as remote authorization.
[Timeouts & Locks]
Pure argument normalization; no I/O, waits or locks.
[Side-Effect Policy]
Construct descriptors and argument mappings only.
"""
from __future__ import annotations

from typing import Any

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args


def _oc_path(args: dict[str, Any], wire: str, field: str = "filePath") -> str:
    value = args.get(field)
    if value is None:
        value = args.get(field.replace("filePath", "file_path"))
    if value is None or str(value) == "":
        raise HarnessProfileError(f"{wire} requires {field} (or file_path)", field=field)
    return str(value)


def _oc_edit_text(args: dict[str, Any], native: str, snake: str, wire: str) -> str:
    value = args[native] if native in args else args.get(snake)
    if value is None:
        raise HarnessProfileError(f"{wire} requires {native} (or {snake})", field=native)
    return str(value)


def _oc_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = _oc_path(args, "remote_read")
    out["offset"] = int(args.get("offset") or 1)
    out["limit"] = min(int(args.get("limit") or 2000), 2000)
    return out


def _oc_write(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = _oc_path(args, "remote_write")
    if "content" not in args or args["content"] is None:
        raise HarnessProfileError("remote_write requires content", field="content")
    out["content"] = args["content"]
    out["overwrite"] = bool(args.get("overwrite", True))
    out["create_dirs"] = True
    return out


def _oc_edit(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = _oc_path(args, "remote_edit")
    out["old_string"] = _oc_edit_text(args, "oldString", "old_string", "remote_edit")
    out["new_string"] = _oc_edit_text(args, "newString", "new_string", "remote_edit")
    out["replace_all"] = bool(args.get("replaceAll") or args.get("replace_all"))
    return out


def _oc_bash(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["command"] = args["command"]
    cwd = args.get("workdir") or args.get("cwd")
    if cwd is not None:
        out["cwd"] = str(cwd)
    if args.get("description") is not None:
        out["description"] = str(args["description"])
    if args.get("timeout") is not None:
        timeout = int(args["timeout"])
        if timeout <= 0:
            raise HarnessProfileError("remote_bash timeout must be positive", field="timeout")
        out["timeout_ms"] = timeout
    if args.get("run_in_background") is True:
        out["yield_time_ms"] = 0
    return out


def _oc_glob(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    return out


def _oc_grep(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    include = args.get("include")
    if isinstance(include, list):
        out["includes"] = [str(item) for item in include]
    elif include is not None:
        out["glob"] = str(include)
    return out


def _oc_list(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    if args.get("path") is not None:
        out["path"] = args["path"]
    ignore = args.get("ignore")
    if isinstance(ignore, list):
        out["ignore_globs"] = [str(item) for item in ignore]
    elif ignore is not None:
        out["ignore_globs"] = [str(ignore)]
    return out


OC_READ = NativeToolSpec(
    native_name="read",
    canonical="remote.read",
    description="Read a remote file (OpenCode read; filePath or file_path with offset/limit).",
    properties={
        "filePath": {"type": "string", "description": "Absolute path of the file to read."},
        "file_path": {"type": "string", "description": "Alias of filePath."},
        "offset": {"type": "integer", "minimum": 1, "description": "1-based first line to return."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Maximum lines to return."},
    },
    required=(),
    normalize=_oc_read,
)

OC_WRITE = NativeToolSpec(
    native_name="write",
    canonical="remote.write",
    description="Write a remote file (OpenCode write; overwrite defaults true, missing parents are created).",
    properties={
        "filePath": {"type": "string", "description": "Absolute path of the file to write."},
        "file_path": {"type": "string", "description": "Alias of filePath."},
        "content": {"type": "string", "description": "Full content to write."},
        "overwrite": {"type": "boolean", "default": True, "description": "Replace an existing file (default true)."},
    },
    required=("content",),
    defaults={"overwrite": True},
    normalize=_oc_write,
)

OC_EDIT = NativeToolSpec(
    native_name="edit",
    canonical="remote.edit",
    description="Replace exact text in a remote file (OpenCode edit; oldString/newString or snake_case aliases).",
    properties={
        "filePath": {"type": "string", "description": "Absolute path of the file to edit."},
        "file_path": {"type": "string", "description": "Alias of filePath."},
        "oldString": {"type": "string", "description": "Exact text to replace."},
        "old_string": {"type": "string", "description": "Alias of oldString."},
        "newString": {"type": "string", "description": "Replacement text (may be empty)."},
        "new_string": {"type": "string", "description": "Alias of newString."},
        "replaceAll": {"type": "boolean", "default": False, "description": "Replace every occurrence."},
        "replace_all": {"type": "boolean", "description": "Alias of replaceAll."},
    },
    required=(),
    defaults={"replaceAll": False},
    normalize=_oc_edit,
)

OC_BASH = NativeToolSpec(
    native_name="bash",
    canonical="remote.bash",
    description="Run a remote shell command (OpenCode bash; timeout is milliseconds).",
    properties={
        "command": {"type": "string", "description": "Shell command to execute."},
        "timeout": {"type": "integer", "minimum": 1, "description": "Execution deadline in milliseconds."},
        "workdir": {"type": "string", "description": "Working directory on the remote endpoint."},
        "cwd": {"type": "string", "description": "Alias of workdir."},
        "description": {"type": "string", "description": "Clear, concise description of the command."},
        "run_in_background": {"type": "boolean", "description": "Return a background job immediately."},
    },
    required=("command",),
    normalize=_oc_bash,
)

OC_GLOB = NativeToolSpec(
    native_name="glob",
    canonical="remote.glob",
    description="Find remote paths by glob pattern (OpenCode glob).",
    properties={
        "pattern": {"type": "string", "description": "Glob pattern to match."},
        "path": {"type": "string", "description": "Directory to search; defaults to the endpoint cwd."},
    },
    required=("pattern",),
    normalize=_oc_glob,
)

OC_GREP = NativeToolSpec(
    native_name="grep",
    canonical="remote.grep",
    description="Search remote file contents with a regular expression (OpenCode grep).",
    properties={
        "pattern": {"type": "string", "description": "Regular expression to search for."},
        "path": {"type": "string", "description": "File or directory to search."},
        "include": {"type": "string", "description": "Glob filter for files to search."},
    },
    required=("pattern",),
    normalize=_oc_grep,
)

OC_LIST = NativeToolSpec(
    native_name="list",
    canonical="remote.ls",
    description="List a remote directory (OpenCode list; optional ignore globs).",
    properties={
        "path": {"type": "string", "description": "Directory to list; defaults to the endpoint cwd."},
        "ignore": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to omit."},
    },
    normalize=_oc_list,
)


SPECS = {
    spec.native_name: spec
    for spec in (OC_READ, OC_WRITE, OC_EDIT, OC_BASH, OC_GLOB, OC_GREP, OC_LIST)
}
