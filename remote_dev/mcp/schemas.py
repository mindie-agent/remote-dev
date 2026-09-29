from __future__ import annotations

from typing import Any


ENDPOINT_PROPS: dict[str, Any] = {
    "host": {"type": "string"},
    "port": {"type": "integer"},
    "user": {"type": "string", "default": "root"},
    "container": {"type": "string", "description": "Existing Docker container name or ID on the SSH host. All tools operate inside it; results return a full ID for reuse without another name lookup. No provisioning or managed allocation."},
    "root": {"type": "string", "default": "/"},
    "cwd": {"type": "string"},
    "runtime_env": {"type": "boolean", "default": True},
    "identity_file": {"type": "string"},
    "connect_timeout_ms": {"type": "integer", "default": 10000},
    "runtime_env_file": {"type": "string", "description": "Remote profile script sourced before commands when runtime_env is true."},
    "alias": {"type": "string", "description": "Name from the endpoint alias files."},
}

# Consumer resolvers may accept additional selector keys (for example a
# session or machine name). Schemas keep additionalProperties open so those
# keys pass through; the server rejects payloads no resolver claims.
ENDPOINT_SELECTOR_DESCRIPTION = (
    "Provide at least one endpoint selector: host and port together, or an "
    "alias from the endpoint alias files. A consumer-registered resolver may "
    "claim additional keys; remote-dev does not interpret session, profile, "
    "or binding identifiers itself. The server validates the selector before "
    "connecting."
)


def schema(
    props: dict[str, Any],
    required: list[str] | None = None,
    *,
    endpoint_selector: bool = True,
    description: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {**ENDPOINT_PROPS, **props},
        "required": required or [],
    }
    # Model providers accept different JSON Schema subsets. Keep the wire
    # schema a plain object; conditional requirements remain enforced by
    # resolve_endpoint / the tool implementation, not just by the client.
    constraints = [ENDPOINT_SELECTOR_DESCRIPTION] if endpoint_selector else []
    if description:
        constraints.append(description)
    if constraints:
        payload["description"] = " ".join(constraints)
    return payload


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "remote.read": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor native Read habit)."},
            "verify_content": {"type": "boolean", "default": True, "description": "Compute full SHA256 and exact line count for edit concurrency. False bounds positive-offset log reads to the requested window and reports unknown total_lines when more data exists."},
            "allow_symlink": {"type": "boolean", "default": False},
            "offset": {"type": "integer", "default": 1, "description": "1-based first line. Negative values count back from the end of the file."},
            "limit": {"type": "integer", "default": 200, "maximum": 500},
            "line_offset": {"type": "integer", "description": "Alias of offset (Kimi native Read habit)."},
            "n_lines": {"type": "integer", "description": "Alias of limit (Kimi native Read habit)."},
            "client_context_id": {"type": "string"},
        },
        description="file_path is required; the alias path is also accepted (Kimi/Cursor habit). Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.write": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor native Write habit)."},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "append": {"type": "boolean", "description": "Append content at end of file instead of replacing it (Kimi Write mode=append). Mutually exclusive with overwrite; creates a missing file."},
            "create_dirs": {"type": "boolean"},
            "client_context_id": {"type": "string"},
        },
        ["content"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.edit": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path."},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "client_context_id": {"type": "string"},
        },
        ["old_string", "new_string"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.multi_edit": schema(
        {
            "file_path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string", "default": ""},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["old_string"],
                },
            },
            "path": {"type": "string", "description": "Alias of file_path."},
            "client_context_id": {"type": "string"},
        },
        ["edits"],
        description="file_path is required; the alias path is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.bash": schema(
        {
            "command": {"type": "string"},
            "cmd": {"type": "string", "description": "Alias of command (Codex exec_command habit)."},
            "workdir": {"type": "string", "description": "Alias of cwd (Codex exec_command habit)."},
            "description": {"type": "string"},
            "timeout_ms": {"type": "integer"},
            "timeout": {"type": "integer"},
            "tty": {"type": "boolean", "default": False, "description": "Allocate a remote PTY (24x80). Ctrl-C signals its foreground process group; stderr is merged into stdout."},
            "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 300000, "default": 10000, "description": "Remote wait for output/completion after connection and preparation. Returns session_id when running or unread output remains."},
            "max_output_tokens": {"type": "integer", "minimum": 1, "description": "Approximate output budget at four UTF-8 bytes/token across text plus structured previews, shared by stdout/stderr. Status/refs metadata is separate. Unreturned bytes remain available through the session cursor."},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        description="command is required; the alias cmd is also accepted (Codex habit). Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.glob": schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}, "respect_gitignore": {"type": "boolean", "description": "Omit paths ignored by .gitignore. Uses git check-ignore when the remote tree is a git worktree; otherwise applies .gitignore rules in-process."}}, ["pattern"]),
    "remote.grep": schema(
        {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "type": {"type": "string"},
            "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count", "count_matches"], "description": "count = matching lines per file (Claude habit); count_matches = total matches per file (Kimi habit, rg --count-matches). They differ when one line holds several matches."},
            "multiline": {"type": "boolean"},
            "case_insensitive": {"type": "boolean", "description": "Case-insensitive matching (rg -i / grep -i). Alias: -i."},
            "-i": {"type": "boolean", "description": "Alias of case_insensitive (Claude Code / Kimi native Grep parameter)."},
            "context_lines": {"type": "integer", "description": "Show N lines before and after each match in content mode (rg/grep -C). Alias: -C."},
            "-C": {"type": "integer", "description": "Alias of context_lines."},
            "before_context": {"type": "integer", "description": "Show N lines before each match in content mode (rg/grep -B). Alias: -B."},
            "-B": {"type": "integer", "description": "Alias of before_context."},
            "after_context": {"type": "integer", "description": "Show N lines after each match in content mode (rg/grep -A). Alias: -A."},
            "-A": {"type": "integer", "description": "Alias of after_context."},
            "line_numbers": {"type": "boolean", "description": "Prefix content matches with line numbers. Default true. Alias: -n."},
            "-n": {"type": "boolean", "description": "Alias of line_numbers."},
            "offset": {"type": "integer", "description": "Skip the first N result lines before applying the limit (pagination)."},
            "head_limit": {"type": "integer", "description": "Alias of limit (Kimi/Claude native Grep habit)."},
            "include_ignored": {
                "type": "boolean",
                "description": (
                    "Also search hidden paths and paths ignored by .gitignore "
                    "(rg --no-ignore --hidden). The grep fallback cannot evaluate "
                    ".gitignore rules; there it only lifts the default hidden/.git "
                    "directory excludes and reports a warning."
                ),
            },
            "limit": {"type": "integer", "maximum": 500},
        },
        ["pattern"],
    ),
    "remote.ls": schema({"path": {"type": "string"}, "limit": {"type": "integer"}, "all": {"type": "boolean"}}),
    "remote.apply_patch": schema(
        {
            "patch": {"type": "string", "description": "Codex apply_patch payload or unified diff. Prefer this field."},
            "command": {"type": "string", "description": "Legacy alias for the patch payload, not a shell command."},
            "timeout_ms": {"type": "integer"},
        },
        description="Provide a non-empty patch or command. If both are provided, patch takes precedence. Missing patch content is rejected by the server.",
    ),
    "remote.job_status": schema({"job_id": {"type": "string"}}, ["job_id"], endpoint_selector=False),
    "remote.job_tail": schema({"job_id": {"type": "string"}, "lines": {"type": "integer", "maximum": 500}, "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]}}, ["job_id"], endpoint_selector=False),
    "remote.job_stop": schema({"job_id": {"type": "string"}, "force": {"type": "boolean"}}, ["job_id"], endpoint_selector=False),
    "remote.job_stdin": schema(
        {
            "job_id": {"type": "string"},
            "chars": {"type": "string", "description": "Bytes to write to the job's stdin (Codex write_stdin habit). Omit or pass an empty string to only poll output. When stdin_buffer_full is reported, resend only the unwritten remainder: slice the original string at the returned written_chars character count (a byte count cannot slice a Unicode string)."},
            "eof": {"type": "boolean", "description": "Close the job's stdin after writing chars. Programs waiting for end-of-input then finish. If the write was only partially accepted (stdin_buffer_full), the close is deferred: resend the unwritten remainder with eof=true."},
            "yield_time_ms": {"type": "integer", "description": "After writing, keep polling up to this many milliseconds for output or completion before returning new output."},
            "max_output_tokens": {"type": "integer", "description": "Cap the new output returned by this call, counted as four UTF-8 bytes/token across text and structured previews, shared by stdout/stderr; metadata separate. Skipped bytes are not lost: the read cursor only advances past what was returned."},
        },
        ["job_id"],
        endpoint_selector=False,
        description="Poll any session; non-empty chars requires writable stdin. Endpoint and identity are restored from the job record. Stop via remote.job_stop.",

    ),
    "remote.artifact_manifest": schema({"remote_path": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_pull": schema({"remote_path": {"type": "string"}, "local_dir": {"type": "string"}, "overwrite": {"type": "boolean", "default": False, "description": "Replace existing local files only when explicitly true. Hash-identical files are skipped."}, "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120000}}, ["remote_path"]),
    "remote.artifact_push": schema({"local_path": {"type": "string"}, "remote_path": {"type": "string"}, "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120000}}, ["local_path", "remote_path"]),
    "remote.context_snapshot": schema({"live_probe": {"type": "boolean", "default": True}}),
    "remote.probe": schema({"modules": {"type": "array", "items": {"type": "string"}, "description": "Explicit module imports to check; default empty."}, "diagnose_connection": {"type": "boolean", "default": False, "description": "Compare SSH connections with a fixed read-only probe; never replays a business command."}}),
}

for _job_name in ("remote.job_status", "remote.job_tail", "remote.job_stop", "remote.job_stdin"):
    TOOL_SCHEMAS[_job_name]["properties"]["session_id"] = {"type": "string", "description": "Alias of job_id returned by remote.bash."}
    TOOL_SCHEMAS[_job_name]["required"] = []
    TOOL_SCHEMAS[_job_name]["description"] = "job_id or session_id is required; checked by the server."

ALIASES: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}

# Thin client-native parameter aliases folded into the canonical fields by
# normalize_arguments. One shared mapping serves the MCP dispatcher and the
# CLI --input-json path; there is no per-client execution fork. A canonical
# key always wins over its alias, and unknown keys pass through untouched so
# consumer resolver selectors keep working.
PARAM_ALIASES: dict[str, dict[str, str]] = {
    "remote.read": {"path": "file_path", "line_offset": "offset", "n_lines": "limit"},
    "remote.write": {"path": "file_path"},
    "remote.edit": {"path": "file_path"},
    "remote.multi_edit": {"path": "file_path"},
    "remote.bash": {"cmd": "command", "workdir": "cwd"},
    "remote.grep": {
        "-i": "case_insensitive",
        "-A": "after_context",
        "-B": "before_context",
        "-C": "context_lines",
        "-n": "line_numbers",
        "head_limit": "limit",
    },
}


def normalize_arguments(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Fold known client-native alias keys into their canonical fields."""
    if tool.startswith("remote.job_"):
        args = dict(args)
        if "job_id" not in args and "session_id" in args:
            args["job_id"] = args["session_id"]
    mapping = PARAM_ALIASES.get(tool)
    if not mapping:
        return args
    normalized = dict(args)
    for alias, canonical in mapping.items():
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized[alias]
        normalized.pop(alias, None)
    return normalized
