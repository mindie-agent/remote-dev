"""Native WorkBuddy (wb) tool descriptors and argument normalizers.

[Role] Translate the native WorkBuddy (CodeBuddy runtime) tool contract into canonical remote operations.
[Invariants] Preserve artifact-derived schemas, defaults (TaskOutput 60s, Glob limit/offset, MultiEdit, ViewImage)
and units; validation stays strict and never treats local approval controls as remote authorization.
[Timeouts & Locks] Pure argument normalization; no I/O, waits or locks.
[Side-Effect Policy] Construct descriptors and argument mappings only.
"""
from __future__ import annotations

import re
from typing import Any

from .native_spec import HarnessProfileError, NativeToolSpec, _endpoint_args
from .profiles_claude import (
    CLAUDE_BASH,
    CLAUDE_EDIT,
    CLAUDE_GREP,
    CLAUDE_NOTEBOOK_EDIT,
    CLAUDE_READ,
    CLAUDE_TASK_STOP,
    CLAUDE_WRITE,
    _CLAUDE_VCS_EXCLUDES,
    _claude_task_output,
    _claude_task_stop,
)


def _wb_multi_edit(args: dict[str, Any]) -> dict[str, Any]:
    edits_raw = args.get("edits")
    if not isinstance(edits_raw, list) or len(edits_raw) < 1:
        raise HarnessProfileError("remote_MultiEdit edits must contain at least 1 edit item", field="edits")
    edits: list[dict[str, Any]] = []
    for idx, item in enumerate(edits_raw):
        if not isinstance(item, dict):
            raise HarnessProfileError(f"remote_MultiEdit edits[{idx}] must be an object", field="edits")
        edits.append({
            "old_string": str(item["old_string"]),
            "new_string": str(item["new_string"]),
            "replace_all": bool(item.get("replace_all", False)),
        })
    out = _endpoint_args(args)
    out["file_path"] = args["file_path"]
    out["edits"] = edits
    return out


def _wb_glob(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["pattern"] = args["pattern"]
    if args.get("path") is not None:
        out["path"] = args["path"]
    out["limit"] = int(args.get("limit", 100))
    out["offset"] = int(args.get("offset", 0))
    out["respect_gitignore"] = False
    out["hidden"] = True
    out["entry_type"] = "file"
    out["excludes"] = list(_CLAUDE_VCS_EXCLUDES)
    return out


def _wb_task_output(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("filter") is not None:
        try:
            re.compile(str(args["filter"]))
        except re.error as exc:
            raise HarnessProfileError(f"invalid regex in filter: {exc}", field="filter") from exc
    return _claude_task_output(args)


def _wb_bash_output(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("filter") is not None:
        try:
            re.compile(str(args["filter"]))
        except re.error as exc:
            raise HarnessProfileError(f"invalid regex in filter: {exc}", field="filter") from exc
    job_id = args.get("shell_id") or args.get("task_id")
    if not job_id:
        raise HarnessProfileError("remote_BashOutput requires shell_id", field="shell_id")
    return {"job_id": str(job_id), **_endpoint_args(args)}


def _wb_view_image(args: dict[str, Any]) -> dict[str, Any]:
    out = _endpoint_args(args)
    out["file_path"] = args.get("image_path") or args.get("file_path") or args.get("path")
    return out


WB_READ = CLAUDE_READ
WB_WRITE = CLAUDE_WRITE
WB_EDIT = CLAUDE_EDIT
WB_BASH = CLAUDE_BASH
WB_GREP = CLAUDE_GREP
WB_NOTEBOOK_EDIT = CLAUDE_NOTEBOOK_EDIT
WB_TASK_STOP = CLAUDE_TASK_STOP

WB_MULTI_EDIT = NativeToolSpec(
    native_name="MultiEdit",
    canonical="remote.multi_edit",
    description="Apply multiple exact string edits sequentially to one file (WorkBuddy MultiEdit; requires a prior Read).",
    properties={
        "file_path": {
            "type": "string",
            "description": "The path to the file to modify (can be absolute or relative)",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {"type": "string", "description": "The text to replace"},
                    "new_string": {"type": "string", "description": "The text to replace it with"},
                    "replace_all": {
                        "type": "boolean",
                        "default": False,
                        "description": "Replace all occurrences of old_string (default false).",
                    },
                },
                "required": ["old_string", "new_string"],
                "additionalProperties": False,
            },
            "description": "Array of edit operations to perform sequentially on the file",
        },
    },
    required=("file_path", "edits"),
    require_prior_read=True,
    normalize=_wb_multi_edit,
)

WB_GLOB = NativeToolSpec(
    native_name="Glob",
    canonical="remote.glob",
    description="Find files by glob pattern (WorkBuddy Glob; supports limit and offset pagination).",
    properties={
        "pattern": {"type": "string", "description": "The glob pattern to match files against"},
        "path": {
            "type": "string",
            "description": "The directory to search in; defaults to the working directory.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 100,
            "description": "Maximum number of results to return. Defaults to 100.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Number of results to skip from the beginning. Defaults to 0.",
        },
    },
    required=("pattern",),
    defaults={"limit": 100, "offset": 0},
    normalize=_wb_glob,
)

WB_TASK_OUTPUT = NativeToolSpec(
    native_name="TaskOutput",
    canonical="remote.job_output",
    description="Retrieve output from a running or completed background task (WorkBuddy TaskOutput; timeout defaults to 60000ms).",
    properties={
        "task_id": {"type": "string", "description": "The task ID to get output from."},
        "block": {
            "type": "boolean",
            "default": True,
            "description": "Whether to wait for task completion (default: true, up to timeout). Pass false for a non-blocking status check.",
        },
        "timeout": {
            "type": "integer",
            "default": 60000,
            "minimum": 0,
            "maximum": 600000,
            "description": "Timeout in milliseconds (0-600000, default: 60000).",
        },
        "filter": {
            "type": "string",
            "description": "Optional regex pattern applied only to output read by this call (background shell tasks only).",
        },
        "full": {
            "type": "boolean",
            "description": "Return the current accumulated output snapshot instead of only output added since the previous read (background shell tasks only).",
        },
    },
    required=("task_id",),
    defaults={"block": True, "timeout": 60000},
    handler="wb_task_output",
    normalize=_wb_task_output,
)

WB_BASH_OUTPUT = NativeToolSpec(
    native_name="BashOutput",
    canonical="remote.job_output",
    description="Retrieve output from a running or completed background shell (WorkBuddy BashOutput; shell_id/filter).",
    properties={
        "shell_id": {
            "type": "string",
            "description": "The ID of the background shell to retrieve output from.",
        },
        "filter": {
            "type": "string",
            "description": "Optional regex pattern to filter output lines.",
        },
    },
    required=("shell_id",),
    defaults={"block": False, "timeout": 0},
    handler="wb_task_output",
    normalize=_wb_bash_output,
)

WB_KILL_SHELL = NativeToolSpec(
    native_name="KillShell",
    canonical="remote.job_stop",
    description="Stop a running background shell by ID (WorkBuddy KillShell).",
    properties={
        "shell_id": {
            "type": "string",
            "description": "The ID of the background shell to kill.",
        },
    },
    required=("shell_id",),
    handler="claude_task_stop",
    normalize=_claude_task_stop,
)

WB_VIEW_IMAGE = NativeToolSpec(
    native_name="ViewImage",
    canonical="remote.read",
    description="Inspect a remote image file and return its visual content block (WorkBuddy ViewImage).",
    properties={
        "image_path": {
            "type": "string",
            "minLength": 1,
            "description": "Absolute path to the image file to look at.",
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "The specific information to extract from the image.",
        },
    },
    required=("image_path", "query"),
    handler="wb_view_image",
    normalize=_wb_view_image,
)

SPECS = {
    spec.native_name: spec
    for spec in (
        WB_READ,
        WB_WRITE,
        WB_EDIT,
        WB_MULTI_EDIT,
        WB_BASH,
        WB_GLOB,
        WB_GREP,
        WB_TASK_OUTPUT,
        WB_BASH_OUTPUT,
        WB_NOTEBOOK_EDIT,
        WB_TASK_STOP,
        WB_KILL_SHELL,
        WB_VIEW_IMAGE,
    )
}
