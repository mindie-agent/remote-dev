"""Native kimi tool descriptors and argument normalizers.

[Role] Translate the native kimi tool contract into canonical remote operations.
[Invariants] Preserve artifact-derived schemas, defaults and units; validation stays
strict and never treats local approval controls as remote authorization.
[Timeouts & Locks] Pure argument normalization; no I/O, waits or locks.
[Side-Effect Policy] Construct descriptors and argument mappings only.
"""
from __future__ import annotations

from typing import Any, Mapping

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args
from .native_spec import _number  # noqa: F401 - shared helper for host specs

# Native Kimi Glob/Grep use rg --hidden by default, always skip VCS metadata
# directories, and always filter sensitive credential files.
_KIMI_VCS_EXCLUDES = [
    ".git", "**/.git", ".hg", "**/.hg", ".svn", "**/.svn",
    ".bzr", "**/.bzr", ".jj", "**/.jj", ".sl", "**/.sl",
]
_KIMI_SENSITIVE_EXCLUDES = [
    "**/.env",
    "**/id_rsa", "**/id_rsa[-_]*",
    "**/id_ed25519", "**/id_ed25519[-_]*",
    "**/id_ecdsa", "**/id_ecdsa[-_]*",
    "**/.aws/credentials", "**/.aws/credentials/**",
    "**/.gcp/credentials", "**/.gcp/credentials/**",
]
_KIMI_SEARCH_EXCLUDES = _KIMI_VCS_EXCLUDES + _KIMI_SENSITIVE_EXCLUDES

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
        out["limit"] = 1000000
    if args.get("max_chars") is not None:
        out["max_chars"] = min(int(args["max_chars"]), 500000)
    if args.get("column_offset") is not None:
        out["column_offset"] = int(args["column_offset"])
    out["utf16_units"] = True
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
    out["hidden"] = True
    out["entry_type"] = "file"
    out["excludes"] = list(_KIMI_SEARCH_EXCLUDES)
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
    out["excludes"] = list(_KIMI_SEARCH_EXCLUDES)
    out["sort_by_mtime"] = True
    return out

KIMI_READ = NativeToolSpec(
    native_name="Read",
    canonical="remote.read",
    description="Read a text file from the local filesystem (Kimi Code Read; max_chars/column_offset pagination).",
    properties={
        "path": {"type": "string", "description": "Path to a text file or a kimi-file:// attachment reference in the current session."},
        "line_offset": {"type": "integer", "not": {"const": 0}, "description": "1-based start line; negative reads from the end. Omit to start at line 1."},
        "column_offset": {"type": "integer", "minimum": 0, "description": "Zero-based character offset within the first line of a forward read."},
        "n_lines": {"type": "integer", "minimum": 1, "description": "Number of lines to read. Omit to read toward the end of the file."},
        "max_chars": {"type": "integer", "minimum": 1, "description": "Maximum characters in the returned text, including line numbers and status."},
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
        "path": {"type": "string"},
        "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["overwrite", "append"]},
    },
    required=("path", "content"),
    defaults={"mode": "overwrite"},
    normalize=_kimi_write,
)

KIMI_EDIT = NativeToolSpec(
    native_name="Edit",
    canonical="remote.edit",
    description="Perform exact replacements in existing files (Kimi Code Edit).",
    properties={
        "path": {"type": "string"},
        "old_string": {"type": "string", "minLength": 1},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    required=("path", "old_string", "new_string"),
    defaults={"replace_all": False},
    normalize=_kimi_edit,
)

KIMI_BASH = NativeToolSpec(
    native_name="Bash",
    canonical="remote.bash",
    description="Execute a bash command (Kimi Code Bash; timeout is in seconds).",
    properties={
        "command": {"type": "string", "minLength": 1},
        "cwd": {"type": "string", "description": "Working directory for the command; defaults to the session working directory."},
        "timeout": {"type": "integer", "default": 60, "minimum": 1, "description": "Timeout in seconds; background default 600, background max 86400, foreground max 300."},
        "description": {"type": "string", "description": "Short description; required when run_in_background is true."},
        "run_in_background": {"type": "boolean"},
        "disable_timeout": {"type": "boolean", "description": "Do not apply a timeout; only meaningful with run_in_background=true."},
    },
    required=("command",),
    handler="kimi_bash",
    normalize=_kimi_bash,
)

KIMI_GLOB = NativeToolSpec(
    native_name="Glob",
    canonical="remote.glob",
    description="Find files whose paths match a glob pattern (Kimi Code Glob; head_limit=0 removes the match-count limit).",
    properties={
        "pattern": {"type": "string"},
        "head_limit": {"type": "integer", "minimum": 0, "description": "Maximum paths after offset; 0 removes the match-count limit."},
        "offset": {"type": "integer", "minimum": 0},
        "path": {"type": "string"},
        "include_ignored": {"type": "boolean"},
        "include_dirs": {"type": "boolean", "description": "Deprecated and ignored; results are always files-only."},
    },
    required=("pattern",),
    defaults={"head_limit": 100, "offset": 0, "include_ignored": False},
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
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count_matches"]},
        "-i": {"type": "boolean"},
        "-n": {"type": "boolean"},
        "-A": {"type": "integer", "minimum": 0},
        "-B": {"type": "integer", "minimum": 0},
        "-C": {"type": "integer", "minimum": 0},
        "head_limit": {"type": "integer", "minimum": 0},
        "offset": {"type": "integer", "minimum": 0},
        "multiline": {"type": "boolean"},
        "include_ignored": {"type": "boolean"},
    },
    required=("pattern",),
    defaults={"output_mode": "files_with_matches", "head_limit": 250, "offset": 0, "-i": False, "-n": True, "multiline": False, "include_ignored": False},
    normalize=_kimi_grep,
)

KIMI_READ_MEDIA = NativeToolSpec(
    native_name="ReadMediaFile",
    canonical="remote.read_image",
    description="Read an image file and return the image itself (Kimi Code ReadMediaFile; region crops are not implemented remotely).",
    properties={
        "path": {"type": "string"},
        "region": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
            },
            "required": ["x", "y", "width", "height"],
        },
        "full_resolution": {"type": "boolean"},
    },
    required=("path",),
    defaults={"full_resolution": False},
    handler="kimi_read_media",
    normalize=lambda args: {"file_path": args["path"], **_endpoint_args(args)},
)

KIMI_TASK_OUTPUT = NativeToolSpec(
    native_name="TaskOutput",
    canonical="remote.job_status",
    description="Retrieve a non-blocking snapshot of a running or completed background task (Kimi Code TaskOutput).",
    properties={"task_id": {"type": "string"}},
    required=("task_id",),
    handler="kimi_task_output",
    normalize=lambda args: {"job_id": args["task_id"], **_endpoint_args(args)},
)

KIMI_TASK_STOP = NativeToolSpec(
    native_name="TaskStop",
    canonical="remote.job_stop",
    description="Stop a running background task (Kimi Code TaskStop).",
    properties={
        "task_id": {"type": "string"},
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


SPECS = {
    spec.native_name: spec
    for spec in (
        KIMI_READ,
        KIMI_WRITE,
        KIMI_EDIT,
        KIMI_BASH,
        KIMI_GLOB,
        KIMI_GREP,
        KIMI_READ_MEDIA,
        KIMI_TASK_OUTPUT,
        KIMI_TASK_STOP,
        KIMI_TASK_LIST
    )
}
