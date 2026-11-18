"""Native Google Antigravity (agy) tool descriptors and argument normalizers.

[Role]
Translate the native Antigravity tool contract into canonical remote operations.
[Invariants]
Preserve artifact-derived names, required fields and units (milliseconds for
`WaitMsBeforeAsync`); a daemon command becomes a background job only after the
requested yield budget, and local approval controls are never remote authorization.
[Timeouts & Locks]
Pure argument normalization; no I/O, waits or locks.
[Side-Effect Policy]
Construct descriptors and argument mappings only.
"""
from __future__ import annotations

from typing import Any

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args


def _agy_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["Path"]
    if args.get("StartLine") is not None:
        out["start_line"] = int(args["StartLine"])
    if args.get("EndLine") is not None:
        out["end_line"] = int(args["EndLine"])
    if args.get("ByteOffset") is not None:
        out["content_offset"] = int(args["ByteOffset"])
    return out


def _agy_write(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["Path"]
    out["content"] = args["Content"]
    append = bool(args.get("Append", False))
    # Antigravity defaults Overwrite=True only for replacement writes. A raw
    # Append=True write must not inherit Overwrite=True (the canonical write
    # contract rejects that combination as invalid_flags).
    overwrite = bool(args["Overwrite"]) if "Overwrite" in args else not append
    out["overwrite"] = overwrite
    out["append"] = append
    out["create_dirs"] = True
    return out


def _agy_edit(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["Path"]
    out["old_string"] = args["Target"]
    out["new_string"] = args["Replacement"]
    return out


def _agy_run_command(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["command"] = args["CommandLine"]
    daemon = bool(args.get("IsDaemon", False))
    wait_ms = args.get("WaitMsBeforeAsync")
    if wait_ms is not None and int(wait_ms) < 0:
        raise HarnessProfileError("remote_run_command WaitMsBeforeAsync must be >= 0",
                                  field="WaitMsBeforeAsync")
    if daemon:
        # Antigravity returns after WaitMsBeforeAsync while the command keeps
        # running; the canonical background contract is the same hand-off.
        out["run_in_background"] = True
        out["yield_time_ms"] = int(wait_ms) if wait_ms is not None else 200
    elif wait_ms is not None:
        out["yield_time_ms"] = int(wait_ms)
    return out


def _agy_manage_task(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["action"] = str(args["Action"]).strip().lower()
    if args.get("TaskId") is not None:
        out["job_id"] = str(args["TaskId"])
    if args.get("Input") is not None:
        out["chars"] = str(args["Input"])
    return out


def _agy_find_by_name(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["Pattern"]
    for native, canonical in (
        ("SearchDirectory", "path"),
        ("Extensions", "extensions"),
        ("Excludes", "excludes"),
        ("MaxDepth", "max_depth"),
        ("Type", "entry_type"),
    ):
        if args.get(native) is not None:
            out[canonical] = args[native]
    return out


def _agy_grep_search(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["Query"]
    if args.get("SearchPath") is not None:
        out["path"] = args["SearchPath"]
    if args.get("Includes") is not None:
        includes = args["Includes"]
        out["includes"] = [includes] if isinstance(includes, str) else list(includes)
    if args.get("Excludes") is not None:
        excludes = args["Excludes"]
        out["excludes"] = [excludes] if isinstance(excludes, str) else list(excludes)
    out["case_insensitive"] = bool(args.get("CaseInsensitive", False))
    out["fixed_strings"] = not bool(args.get("IsRegex", True))
    out["output_mode"] = "content" if bool(args.get("MatchPerLine", True)) else "files_with_matches"
    return out


def _agy_list_dir(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["path"] = args["DirectoryPath"]
    return out


AGY_READ = NativeToolSpec(
    native_name="read_file",
    canonical="remote.read",
    description="Read a remote file (Antigravity read_file; start/end lines and byte offset).",
    properties={
        "Path": {"type": "string", "description": "Absolute path of the file to read."},
        "StartLine": {"type": "integer", "minimum": 1, "description": "First 1-indexed line to return."},
        "EndLine": {"type": "integer", "minimum": 1, "description": "Last 1-indexed line to return, inclusive."},
        "ByteOffset": {"type": "integer", "minimum": 0, "description": "Zero-based UTF-8 byte offset across the selected line range as a whole."},
    },
    required=("Path",),
    normalize=_agy_read,
)

AGY_WRITE = NativeToolSpec(
    native_name="write_file",
    canonical="remote.write",
    description="Write a remote file (Antigravity write_file; Overwrite defaults true, Append is explicit).",
    properties={
        "Path": {"type": "string", "description": "Absolute path of the file to write."},
        "Content": {"type": "string", "description": "Full content to write."},
        "Overwrite": {"type": "boolean", "default": True, "description": "Replace an existing file (default true)."},
        "Append": {"type": "boolean", "default": False, "description": "Append content instead of replacing it."},
    },
    required=("Path", "Content"),
    defaults={"Append": False},
    normalize=_agy_write,
)

AGY_EDIT = NativeToolSpec(
    native_name="edit_file",
    canonical="remote.edit",
    description="Replace one exact target string in a remote file (Antigravity edit_file).",
    properties={
        "Path": {"type": "string", "description": "Absolute path of the file to edit."},
        "Target": {"type": "string", "description": "Exact text to replace."},
        "Replacement": {"type": "string", "description": "Replacement text (may be empty to delete)."},
    },
    required=("Path", "Target", "Replacement"),
    normalize=_agy_edit,
)

AGY_RUN_COMMAND = NativeToolSpec(
    native_name="run_command",
    canonical="remote.bash",
    description="Run a remote shell command (Antigravity run_command; IsDaemon keeps it running after WaitMsBeforeAsync).",
    properties={
        "CommandLine": {"type": "string", "description": "Shell command to execute."},
        "WaitMsBeforeAsync": {"type": "integer", "minimum": 0, "description": "Milliseconds to wait before yielding the background job."},
        "IsDaemon": {"type": "boolean", "default": False, "description": "Keep running as a background job after the yield budget."},
    },
    required=("CommandLine",),
    defaults={"IsDaemon": False},
    normalize=_agy_run_command,
)

AGY_MANAGE_TASK = NativeToolSpec(
    native_name="manage_task",
    canonical="remote.job_status",
    description="Inspect, feed or stop a remote background task (Antigravity manage_task).",
    properties={
        "Action": {"type": "string", "enum": ["list", "status", "send_input", "kill"],
                   "description": "Task-management action."},
        "TaskId": {"type": "string", "description": "Background task id (required except for Action=list)."},
        "Input": {"type": "string", "description": "Input bytes for Action=send_input."},
    },
    required=("Action",),
    normalize=_agy_manage_task,
)

AGY_FIND_BY_NAME = NativeToolSpec(
    native_name="find_by_name",
    canonical="remote.glob",
    description="Find remote paths by name or glob (Antigravity find_by_name).",
    properties={
        "Pattern": {"type": "string", "description": "Glob pattern or file name to match."},
        "SearchDirectory": {"type": "string", "description": "Directory to search; defaults to the endpoint cwd."},
        "Extensions": {"type": "array", "items": {"type": "string"}, "description": "Only return these extensions (for example .py)."},
        "Excludes": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to exclude."},
        "MaxDepth": {"type": "integer", "minimum": 0, "description": "Maximum traversal depth."},
        "Type": {"type": "string", "enum": ["file", "directory", "any"], "description": "Entry type filter."},
    },
    required=("Pattern",),
    normalize=_agy_find_by_name,
)

AGY_GREP_SEARCH = NativeToolSpec(
    native_name="grep_search",
    canonical="remote.grep",
    description="Search remote file contents (Antigravity grep_search; IsRegex/MatchPerLine select the shape).",
    properties={
        "Query": {"type": "string", "description": "Pattern to search for."},
        "SearchPath": {"type": "string", "description": "File or directory to search; defaults to the endpoint cwd."},
        "Includes": {"type": "array", "items": {"type": "string"}, "description": "Include glob filters."},
        "Excludes": {"type": "array", "items": {"type": "string"}, "description": "Exclude glob filters."},
        "CaseInsensitive": {"type": "boolean", "default": False, "description": "Case-insensitive search."},
        "IsRegex": {"type": "boolean", "default": True, "description": "Treat Query as a regular expression; false means a fixed string."},
        "MatchPerLine": {"type": "boolean", "default": True, "description": "Return matching lines; false returns matching file paths."},
    },
    required=("Query",),
    defaults={"CaseInsensitive": False, "IsRegex": True, "MatchPerLine": True},
    normalize=_agy_grep_search,
)

AGY_LIST_DIR = NativeToolSpec(
    native_name="list_dir",
    canonical="remote.ls",
    description="List one remote directory without reading file contents (Antigravity list_dir).",
    properties={
        "DirectoryPath": {"type": "string", "description": "Absolute directory path to list."},
    },
    required=("DirectoryPath",),
    normalize=_agy_list_dir,
)


SPECS = {
    spec.native_name: spec
    for spec in (
        AGY_READ,
        AGY_WRITE,
        AGY_EDIT,
        AGY_RUN_COMMAND,
        AGY_MANAGE_TASK,
        AGY_FIND_BY_NAME,
        AGY_GREP_SEARCH,
        AGY_LIST_DIR,
    )
}
