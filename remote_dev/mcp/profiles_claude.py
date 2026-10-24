"""Native claude tool descriptors and argument normalizers.

Schemas/defaults/units are derived from the installed claude artifact; the MCP
profile validator remains strict and no canonical-compat bypass is applied.
"""
from __future__ import annotations

from typing import Any, Mapping

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args
from .native_spec import _number  # noqa: F401 - shared helper for host specs

def _claude_read(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["offset"] = int(args.get("offset") or 1)
    out["limit"] = min(int(args.get("limit") or 2000), 2000)
    out["max_chars"] = 262144
    out["utf8_only"] = True
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
        if timeout <= 0:
            raise HarnessProfileError("remote_Bash timeout must be positive")
        timeout = min(timeout, 600000)
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
        "limit": {"type": "integer", "minimum": 1, "description": "The number of lines to read."},
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
        "command": {"type": "string", "description": "The command to execute."},
        "timeout": {"type": "number", "description": "Optional timeout in milliseconds (max 600000; default 120000)."},
        "description": {"type": "string", "description": "Clear, concise description of what this command does in active voice."},
        "run_in_background": {"type": "boolean", "description": "Set to true to run this command in the background."},
        "dangerouslyDisableSandbox": {"type": "boolean", "description": "Local host sandbox override; rejected for remote calls."},
    },
    required=("command",),
    handler="claude_bash",
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
        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
        "-B": {"type": "integer", "minimum": 0},
        "-A": {"type": "integer", "minimum": 0},
        "-C": {"type": "integer", "minimum": 0},
        "context": {"type": "integer", "minimum": 0},
        "-n": {"type": "boolean"},
        "-i": {"type": "boolean"},
        "-o": {"type": "boolean"},
        "type": {"type": "string"},
        "head_limit": {"type": "integer", "minimum": 0},
        "offset": {"type": "integer", "minimum": 0},
        "multiline": {"type": "boolean"},
    },
    required=("pattern",),
    defaults={"output_mode": "files_with_matches", "head_limit": 250, "offset": 0, "-n": True, "-o": False, "multiline": False},
    normalize=_claude_grep,
)

CLAUDE_TASK_OUTPUT = NativeToolSpec(
    native_name="TaskOutput",
    canonical="remote.job_output",
    description="Retrieve output from a running or completed task (Claude Code TaskOutput; block defaults true).",
    properties={
        "task_id": {"type": "string", "description": "The task ID to get output from."},
        "block": {"type": "boolean", "default": True, "description": "Whether to wait for completion."},
        "timeout": {"type": "integer", "default": 30000, "minimum": 0, "maximum": 600000, "description": "Max wait time in ms."},
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
        "notebook_path": {"type": "string", "description": "The absolute path to the Jupyter notebook file."},
        "new_source": {"type": "string", "description": "The new source for the cell."},
        "cell_id": {"type": "string", "description": "The ID of the cell to edit."},
        "cell_type": {"type": "string", "enum": ["code", "markdown"], "description": "The type of the cell."},
        "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
    },
    required=("notebook_path", "new_source"),
    defaults={"edit_mode": "replace"},
    require_prior_read=True,
    handler="claude_notebook_edit",
    normalize=lambda args: {"file_path": args["notebook_path"], **_endpoint_args(args)},
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


SPECS = {
    spec.native_name: spec
    for spec in (
        CLAUDE_READ,
        CLAUDE_WRITE,
        CLAUDE_EDIT,
        CLAUDE_BASH,
        CLAUDE_GLOB,
        CLAUDE_GREP,
        CLAUDE_TASK_OUTPUT,
        CLAUDE_BASH_OUTPUT,
        CLAUDE_NOTEBOOK_EDIT,
        CLAUDE_TASK_STOP,
        CLAUDE_KILL_SHELL
    )
}
