"""
[Role] JSON Schema definitions and 6-Harness tool/parameter alias tables (`mcp/schemas.py`).
[Invariants]
- Defines `TOOL_SCHEMAS` for all 18 canonical `remote.*` tools, `ALIASES` (portable `remote_*` wire names), `HARNESS_TOOL_ALIASES` (40 native tool habits across 6 Harnesses), and `PARAM_ALIASES`.
- Keeps aliasable required parameters enforced in `mcp/tools.py` so provider-side JSON Schema validators never reject valid alias-only calls.
[Timeouts & Locks]
- Pure in-memory schema and argument normalization (`normalize_arguments`).
[Side-Effect Policy]
- Side-effect-free dictionary transformation.
"""
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
    "connect_timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional caller connection limit in milliseconds; omitted by default."},
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
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor/DSH native Read habit)."},
            "verify_content": {"type": "boolean", "default": True, "description": "Compute full SHA256 and exact line count for edit concurrency. False bounds positive-offset log reads to the requested window and reports unknown total_lines when more data exists."},
            "allow_symlink": {"type": "boolean", "default": False},
            "offset": {"type": "integer", "default": 1, "description": "1-based first line. Negative values count back from the end of the file."},
            "limit": {"type": "integer", "default": 200, "maximum": 20000},
            "max_chars": {"type": "integer", "minimum": 1, "description": "Optional character budget for the returned text, including line-number prefixes. Native Kimi Read advertises 100000 by default and 500000 maximum; canonical calls leave it unset."},
            "column_offset": {"type": "integer", "minimum": 0, "description": "Zero-based character offset within the first selected line of a forward read, excluding the line-number prefix."},
            "line_offset": {"type": "integer", "description": "Alias of offset (Kimi native Read habit)."},
            "n_lines": {"type": "integer", "description": "Alias of limit (Kimi native Read habit)."},
            "start_line": {"type": "integer", "description": "1-based inclusive start line (Antigravity StartLine / Cursor / DSH habit)."},
            "end_line": {"type": "integer", "description": "1-based inclusive end line (Antigravity EndLine / Cursor / DSH habit)."},
            "content_offset": {"type": "integer", "description": "0-based byte/character offset for reading truncated content (Antigravity ContentOffset habit)."},
            "outline": {"type": "boolean", "default": False, "description": "Return structured class/function symbol outline with line ranges (Antigravity view_file_outline habit)."},
            "item_offset": {"type": "integer", "default": 0, "description": "0-based symbol offset when outline=true (Antigravity ItemOffset habit)."},
            "node_paths": {"type": "array", "items": {"type": "string"}, "description": "Inspect specific class/function symbol paths by AST/definition range (Antigravity view_code_item NodePaths habit)."},
            "client_context_id": {"type": "string"},
        },
        description="file_path is required; aliases path / AbsolutePath / TargetFile / target_file are also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.write": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path (Kimi/Cursor native Write habit)."},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "append": {"type": "boolean", "description": "Append content at end of file instead of replacing it (Kimi Write mode=append / Antigravity Append). Mutually exclusive with overwrite; creates a missing file."},
            "mode": {"type": "string", "enum": ["overwrite", "write", "append"], "description": "Optional Kimi WriteFile mode ('overwrite'/'write' or 'append')."},
            "create_dirs": {"type": "boolean"},
            "description": {"type": "string", "description": "Optional change description (Antigravity write_to_file Description habit)."},
            "client_context_id": {"type": "string"},
        },
        ["content"],
        description="file_path is required; the alias path / TargetFile is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.edit": schema(
        {
            "file_path": {"type": "string"},
            "path": {"type": "string", "description": "Alias of file_path."},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "start_line": {"type": "integer", "description": "Optional 1-based inclusive start line scoping the replacement (Antigravity StartLine habit)."},
            "end_line": {"type": "integer", "description": "Optional 1-based inclusive end line scoping the replacement (Antigravity EndLine habit)."},
            "instruction": {"type": "string", "description": "Optional edit instruction (Antigravity replace_file_content habit)."},
            "description": {"type": "string", "description": "Optional edit description (Antigravity replace_file_content habit)."},
            "client_context_id": {"type": "string"},
        },
        ["old_string", "new_string"],
        description="file_path is required; the alias path / TargetFile is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
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
            "instruction": {"type": "string", "description": "Optional edit instruction (Antigravity multi_replace_file_content habit)."},
            "description": {"type": "string", "description": "Optional edit description (Antigravity multi_replace_file_content habit)."},
            "client_context_id": {"type": "string"},
        },
        ["edits"],
        description="file_path is required; the alias path / TargetFile is also accepted. Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.bash": schema(
        {
            "command": {"type": "string"},
            "cmd": {"type": "string", "description": "Alias of command (Codex exec_command habit)."},
            "workdir": {"type": "string", "description": "Alias of cwd (Codex exec_command habit)."},
            "description": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional explicit execution limit in milliseconds. Omit to wait for completion, failure or cancellation."},
            "timeout": {"type": "integer", "minimum": 1, "description": "Alias of timeout_ms; values must agree if both are supplied."},
            "tty": {"type": "boolean", "default": False, "description": "Allocate a remote PTY (24x80). Ctrl-C signals its foreground process group; stderr is merged into stdout."},
            "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 600000, "default": 10000, "description": "Remote wait for output/completion after connection and preparation. Returns session_id when running or unread output remains."},
            "run_in_background": {"type": "boolean", "description": "Launch in background and return job_id/session_id immediately (Claude Code run_in_background / Antigravity IsDaemon / Cursor is_background habit)."},
            "max_output_tokens": {"type": "integer", "minimum": 1, "description": "Approximate output budget at four UTF-8 bytes/token across text plus structured previews, shared by stdout/stderr. Status/refs metadata is separate. Unreturned bytes remain available through the session cursor."},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        description="command is required; the alias cmd / CommandLine is also accepted (Codex / Antigravity habit). Enforced by the server so alias-only calls pass provider schema validation.",
    ),
    "remote.glob": schema(
        {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 0, "description": "Maximum paths after offset. 0 means no match-count limit for native Kimi Glob; canonical callers normally use a positive page size."},
            "offset": {"type": "integer", "minimum": 0, "description": "Number of matching paths to skip before the limit."},
            "respect_gitignore": {"type": "boolean", "description": "Omit paths ignored by .gitignore. Uses git check-ignore when the remote tree is a git worktree; otherwise applies .gitignore rules in-process."},
            "include_ignored": {"type": "boolean", "description": "Include paths excluded by ignore files; inverse of respect_gitignore, provided for native Kimi/Claude habits."},
            "extensions": {"type": "array", "items": {"type": "string"}, "description": "Optional file extensions filter without leading dot (Antigravity find_by_name Extensions habit)."},
            "excludes": {"type": "array", "items": {"type": "string"}, "description": "Optional glob patterns to exclude (Antigravity Excludes / Cursor exclude_pattern habit)."},
            "entry_type": {"type": "string", "enum": ["file", "directory", "any"], "description": "Filter by entry type: file, directory, or any (Antigravity find_by_name Type habit)."},
            "max_depth": {"type": "integer", "description": "Maximum directory traversal depth (Antigravity MaxDepth / Codex depth habit)."},
            "full_path": {"type": "boolean", "description": "Match pattern against full relative path instead of filename (Antigravity FullPath habit)."},
        },
        ["pattern"],
    ),
    "remote.grep": schema(
        {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "includes": {"type": "array", "items": {"type": "string"}, "description": "Additional glob patterns to include (Antigravity Includes / Cursor include_pattern habit)."},
            "excludes": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to exclude (Antigravity Excludes / Cursor exclude_pattern habit)."},
            "type": {"type": "string"},
            "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count", "count_matches"], "description": "count = matching lines per file (Claude habit); count_matches = total matches per file (Kimi habit, rg --count-matches). They differ when one line holds several matches."},
            "multiline": {"type": "boolean"},
            "fixed_strings": {"type": "boolean", "description": "Treat pattern as a literal string instead of a regular expression (rg -F / grep -F; Antigravity IsRegex=false habit)."},
            "only_matching": {"type": "boolean", "description": "Print only the matched non-empty parts, one per output line (rg -o; Claude Code Grep -o habit). Only applies to output_mode=content."},
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
            "limit": {"type": "integer", "minimum": 0, "maximum": 5000, "description": "Maximum lines/entries after offset. 0 means no match-count limit for native Kimi Grep."},
        },
        ["pattern"],
    ),
    "remote.ls": schema(
        {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
            "all": {"type": "boolean"},
            "max_depth": {"type": "integer", "description": "Maximum directory traversal depth (default 1; Codex/DSH depth habit)."},
            "ignore_globs": {"type": "array", "items": {"type": "string"}, "description": "Optional glob patterns to omit from directory listing (Cursor ignore_globs habit)."},
        }
    ),
    "remote.apply_patch": schema(
        {
            "patch": {"type": "string", "description": "Codex apply_patch payload or unified diff. Prefer this field."},
            "command": {"type": "string", "description": "Legacy alias for the patch payload, not a shell command."},
            "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional explicit execution limit in milliseconds. Omit to wait for completion, failure or cancellation."},
        },
        description="Provide a non-empty patch or command. If both are provided, patch takes precedence. Missing patch content is rejected by the server.",
    ),
    "remote.job_status": schema(
        {
            "job_id": {"type": "string"},
            "action": {"type": "string", "enum": ["status", "list", "kill", "stop", "send_input", "tail"], "description": "Optional task management action (Antigravity manage_task Action habit). Defaults to status."},
            "chars": {"type": "string", "description": "Input bytes when action='send_input' (Antigravity manage_task Input habit)."},
            "force": {"type": "boolean", "description": "Force kill when action='kill'/'stop'."},
            "lines": {"type": "integer", "maximum": 500, "description": "Line count when action='tail'."},
        },
        ["job_id"],
        endpoint_selector=False,
    ),
    "remote.job_tail": schema(
        {
            "job_id": {"type": "string"},
            "lines": {"type": "integer", "maximum": 500},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]},
            "yield_time_ms": {"type": "integer", "description": "Optional poll wait in milliseconds (DSH/Codex job_output/write_stdin habit)."},
            "max_output_tokens": {"type": "integer", "description": "Optional output token cap (DSH/Codex habit)."},
            "timeout_ms": {"type": "integer", "description": "Optional timeout in milliseconds."},
        },
        ["job_id"],
        endpoint_selector=False,
    ),
    "remote.job_output": schema(
        {
            "job_id": {"type": "string"},
            "lines": {"type": "integer", "maximum": 500},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]},
            "yield_time_ms": {"type": "integer", "description": "Optional poll wait in milliseconds (DSH/Codex job_output/write_stdin habit)."},
            "max_output_tokens": {"type": "integer", "description": "Optional output token cap (DSH/Codex habit)."},
            "timeout_ms": {"type": "integer", "description": "Optional timeout in milliseconds."},
        },
        ["job_id"],
        endpoint_selector=False,
    ),
    "remote.job_stop": schema({"job_id": {"type": "string"}, "force": {"type": "boolean"}}, ["job_id"], endpoint_selector=False),
    "remote.job_stdin": schema(
        {
            "job_id": {"type": "string"},
            "chars": {"type": "string", "description": "Bytes to write to the job's stdin (Codex write_stdin / Antigravity manage_task Input habit). Omit or pass an empty string to only poll output. When stdin_buffer_full is reported, resend only the unwritten remainder: slice the original string at the returned written_chars character count (a byte count cannot slice a Unicode string)."},
            "eof": {"type": "boolean", "description": "Close the job's stdin after writing chars. Programs waiting for end-of-input then finish. If the write was only partially accepted (stdin_buffer_full), the close is deferred: resend the unwritten remainder with eof=true."},
            "yield_time_ms": {"type": "integer", "description": "After writing, keep polling up to this many milliseconds for output or completion before returning new output."},
            "max_output_tokens": {"type": "integer", "description": "Cap the new output returned by this call, counted as four UTF-8 bytes/token across text and structured previews, shared by stdout/stderr; metadata separate. Skipped bytes are not lost: the read cursor only advances past what was returned."},
        },
        ["job_id"],
        endpoint_selector=False,
        description="Poll any session; non-empty chars requires writable stdin. Endpoint and identity are restored from the job record. Stop via remote.job_stop.",
    ),
    "remote.artifact_manifest": schema({"remote_path": {"type": "string"}}, ["remote_path"]),
    "remote.artifact_pull": schema({"remote_path": {"type": "string"}, "local_dir": {"type": "string"}, "overwrite": {"type": "boolean", "default": False, "description": "Replace existing local files only when explicitly true. Hash-identical files are skipped."}}, ["remote_path"]),
    "remote.artifact_push": schema({"local_path": {"type": "string"}, "remote_path": {"type": "string"}}, ["local_path", "remote_path"]),
    "remote.context_snapshot": schema({"live_probe": {"type": "boolean", "default": True}}),
    "remote.probe": schema({"modules": {"type": "array", "items": {"type": "string"}, "description": "Explicit module imports to check; default empty."}, "diagnose_connection": {"type": "boolean", "default": False, "description": "Compare SSH connections with a fixed read-only probe; never replays a business command."}}),
}

for _job_name in ("remote.job_status", "remote.job_tail", "remote.job_output", "remote.job_stop", "remote.job_stdin"):
    TOOL_SCHEMAS[_job_name]["properties"]["session_id"] = {"type": "string", "description": "Alias of job_id returned by remote.bash."}
    TOOL_SCHEMAS[_job_name]["required"] = []
    TOOL_SCHEMAS[_job_name]["description"] = "job_id or session_id is required; checked by the server."

ALIASES: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_SCHEMAS}

# Additional Harness-native tool name aliases accepted by canonical_name() / call_tool()
# across Codex, Claude Code, Cursor, Kimi Code, DSH, and Antigravity.
HARNESS_TOOL_ALIASES: dict[str, str] = {
    "remote.view_file": "remote.read",
    "remote_view_file": "remote.read",
    "remote.read_file": "remote.read",
    "remote_read_file": "remote.read",
    "remote.view_file_outline": "remote.read",
    "remote_view_file_outline": "remote.read",
    "remote.view_code_item": "remote.read",
    "remote_view_code_item": "remote.read",
    "remote.write_to_file": "remote.write",
    "remote_write_to_file": "remote.write",
    "remote.write_file": "remote.write",
    "remote_write_file": "remote.write",
    "remote.replace_file_content": "remote.edit",
    "remote_replace_file_content": "remote.edit",
    "remote.str_replace_file": "remote.edit",
    "remote_str_replace_file": "remote.edit",
    "remote.multi_replace_file_content": "remote.multi_edit",
    "remote_multi_replace_file_content": "remote.multi_edit",
    "remote.exec_command": "remote.bash",
    "remote_exec_command": "remote.bash",
    "remote.run_command": "remote.bash",
    "remote_run_command": "remote.bash",
    "remote.run_terminal_cmd": "remote.bash",
    "remote_run_terminal_cmd": "remote.bash",
    "remote.shell": "remote.bash",
    "remote_shell": "remote.bash",
    "remote.write_stdin": "remote.job_stdin",
    "remote_write_stdin": "remote.job_stdin",
    "remote.send_command_input": "remote.job_stdin",
    "remote_send_command_input": "remote.job_stdin",
    "remote.manage_task": "remote.job_status",
    "remote_manage_task": "remote.job_status",
    "remote.command_status": "remote.job_status",
    "remote_command_status": "remote.job_status",
    "remote.bash_output": "remote.job_status",
    "remote_bash_output": "remote.job_status",
    "remote.task_output": "remote.job_status",
    "remote_task_output": "remote.job_status",
    "remote.kill_shell": "remote.job_stop",
    "remote_kill_shell": "remote.job_stop",
    "remote.task_stop": "remote.job_stop",
    "remote_task_stop": "remote.job_stop",
    "remote.find_by_name": "remote.glob",
    "remote_find_by_name": "remote.glob",
    "remote.file_search": "remote.glob",
    "remote_file_search": "remote.glob",
    "remote.grep_search": "remote.grep",
    "remote_grep_search": "remote.grep",
    "remote.list_dir": "remote.ls",
    "remote_list_dir": "remote.ls",
    "remote.Read": "remote.read",
    "remote_Read": "remote.read",
    "remote.ReadFile": "remote.read",
    "remote_ReadFile": "remote.read",
    "remote.Write": "remote.write",
    "remote_Write": "remote.write",
    "remote.WriteFile": "remote.write",
    "remote_WriteFile": "remote.write",
    "remote.Edit": "remote.edit",
    "remote_Edit": "remote.edit",
    "remote.StrReplaceFile": "remote.edit",
    "remote_StrReplaceFile": "remote.edit",
    "remote.MultiEdit": "remote.multi_edit",
    "remote_MultiEdit": "remote.multi_edit",
    "remote.Bash": "remote.bash",
    "remote_Bash": "remote.bash",
    "remote.Shell": "remote.bash",
    "remote_Shell": "remote.bash",
    "remote.Glob": "remote.glob",
    "remote_Glob": "remote.glob",
    "remote.Grep": "remote.grep",
    "remote_Grep": "remote.grep",
    "remote.LS": "remote.ls",
    "remote_LS": "remote.ls",
    "remote.JobOutput": "remote.job_output",
    "remote_JobOutput": "remote.job_output",
    "remote.BashOutput": "remote.job_status",
    "remote_BashOutput": "remote.job_status",
    "remote.TaskOutput": "remote.job_status",
    "remote_TaskOutput": "remote.job_status",
    "remote.KillShell": "remote.job_stop",
    "remote_KillShell": "remote.job_stop",
    "remote.TaskStop": "remote.job_stop",
    "remote_TaskStop": "remote.job_stop",
}

# Thin client-native parameter aliases folded into the canonical fields by
# normalize_arguments. One shared mapping serves the MCP dispatcher and the
# CLI --input-json path; there is no per-client execution fork. A canonical
# key always wins over its alias, and unknown keys pass through untouched so
# consumer resolver selectors keep working.
PARAM_ALIASES: dict[str, dict[str, str]] = {
    "remote.read": {
        "path": "file_path",
        "TargetFile": "file_path",
        "target_file": "file_path",
        "AbsolutePath": "file_path",
        "absolute_path": "file_path",
        "File": "file_path",
        "line_offset": "offset",
        "n_lines": "limit",
        "StartLine": "start_line",
        "start_line_one_indexed": "start_line",
        "EndLine": "end_line",
        "end_line_one_indexed_inclusive": "end_line",
        "ContentOffset": "content_offset",
        "ItemOffset": "item_offset",
        "NodePaths": "node_paths",
        "symbol": "node_paths",
        "symbols": "node_paths",
        "should_read_entire_file": "_should_read_entire_file",
        "explanation": "_explanation",
    },
    "remote.write": {
        "path": "file_path",
        "TargetFile": "file_path",
        "target_file": "file_path",
        "AbsolutePath": "file_path",
        "absolute_path": "file_path",
        "CodeContent": "content",
        "code_content": "content",
        "contents": "content",
        "Overwrite": "overwrite",
        "Append": "append",
        "Description": "description",
        "explanation": "description",
    },
    "remote.edit": {
        "path": "file_path",
        "TargetFile": "file_path",
        "target_file": "file_path",
        "AbsolutePath": "file_path",
        "absolute_path": "file_path",
        "TargetContent": "old_string",
        "target_content": "old_string",
        "old_str": "old_string",
        "ReplacementContent": "new_string",
        "replacement_content": "new_string",
        "new_str": "new_string",
        "AllowMultiple": "replace_all",
        "allow_multiple": "replace_all",
        "StartLine": "start_line",
        "start_line_one_indexed": "start_line",
        "EndLine": "end_line",
        "end_line_one_indexed_inclusive": "end_line",
        "Instruction": "instruction",
        "Description": "description",
        "explanation": "description",
    },
    "remote.multi_edit": {
        "path": "file_path",
        "TargetFile": "file_path",
        "target_file": "file_path",
        "AbsolutePath": "file_path",
        "absolute_path": "file_path",
        "ReplacementChunks": "edits",
        "replacement_chunks": "edits",
        "Instruction": "instruction",
        "Description": "description",
        "explanation": "description",
    },
    "remote.bash": {
        "cmd": "command",
        "CommandLine": "command",
        "command_line": "command",
        "workdir": "cwd",
        "working_dir": "cwd",
        "Cwd": "cwd",
        "WaitMsBeforeAsync": "yield_time_ms",
        "wait_ms_before_async": "yield_time_ms",
        "is_background": "run_in_background",
        "IsDaemon": "run_in_background",
        "is_daemon": "run_in_background",
        "explanation": "description",
    },
    "remote.glob": {
        "Pattern": "pattern",
        "glob_pattern": "pattern",
        "file_pattern": "pattern",
        "query": "pattern",
        "SearchDirectory": "path",
        "search_directory": "path",
        "target_directory": "path",
        "DirectoryPath": "path",
        "dir_path": "path",
        "Extensions": "extensions",
        "Excludes": "excludes",
        "exclude_pattern": "excludes",
        "Type": "entry_type",
        "type": "entry_type",
        "file_type": "entry_type",
        "MaxDepth": "max_depth",
        "depth": "max_depth",
        "FullPath": "full_path",
        "explanation": "_explanation",
    },
    "remote.grep": {
        "Query": "pattern",
        "query": "pattern",
        "SearchPath": "path",
        "search_path": "path",
        "target_directory": "path",
        "-i": "case_insensitive",
        "CaseInsensitive": "case_insensitive",
        "ignore_case": "case_insensitive",
        "case_sensitive": "_case_sensitive",
        "IsRegex": "_is_regex",
        "is_regex": "_is_regex",
        "literal": "fixed_strings",
        "-F": "fixed_strings",
        "MatchPerLine": "_match_per_line",
        "match_per_line": "_match_per_line",
        "Includes": "includes",
        "include_pattern": "includes",
        "Excludes": "excludes",
        "exclude_pattern": "excludes",
        "-A": "after_context",
        "-B": "before_context",
        "-C": "context_lines",
        "-n": "line_numbers",
        "head_limit": "limit",
        "explanation": "_explanation",
    },
    "remote.ls": {
        "DirectoryPath": "path",
        "directory_path": "path",
        "dir_path": "path",
        "target_directory": "path",
        "MaxDepth": "max_depth",
        "depth": "max_depth",
        "recursive": "_recursive",
        "Excludes": "ignore_globs",
        "excludes": "ignore_globs",
        "explanation": "_explanation",
    },
    "remote.job_status": {
        "TaskId": "job_id",
        "task_id": "job_id",
        "bash_id": "job_id",
        "shell_id": "job_id",
        "Action": "action",
        "Input": "chars",
        "input": "chars",
        "stdin": "chars",
    },
    "remote.job_tail": {
        "TaskId": "job_id",
        "task_id": "job_id",
        "bash_id": "job_id",
        "shell_id": "job_id",
    },
    "remote.job_output": {
        "TaskId": "job_id",
        "task_id": "job_id",
        "bash_id": "job_id",
        "shell_id": "job_id",
    },
    "remote.job_stop": {
        "TaskId": "job_id",
        "task_id": "job_id",
        "bash_id": "job_id",
        "shell_id": "job_id",
    },
    "remote.job_stdin": {
        "TaskId": "job_id",
        "task_id": "job_id",
        "bash_id": "job_id",
        "shell_id": "job_id",
        "Input": "chars",
        "input": "chars",
        "stdin": "chars",
        "WaitMsBeforeAsync": "yield_time_ms",
        "wait_ms_before_async": "yield_time_ms",
    },
}


def _normalize_edit_chunk(chunk: Any) -> Any:
    if not isinstance(chunk, dict):
        return chunk
    out = dict(chunk)
    chunk_map = {
        "TargetContent": "old_string",
        "target_content": "old_string",
        "old_str": "old_string",
        "ReplacementContent": "new_string",
        "replacement_content": "new_string",
        "new_str": "new_string",
        "AllowMultiple": "replace_all",
        "allow_multiple": "replace_all",
        "StartLine": "start_line",
        "start_line_one_indexed": "start_line",
        "EndLine": "end_line",
        "end_line_one_indexed_inclusive": "end_line",
    }
    for alias, canonical in chunk_map.items():
        if alias in out and canonical not in out:
            out[canonical] = out[alias]
        out.pop(alias, None)
    return out


def normalize_arguments(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Fold known client-native alias keys into their canonical fields."""
    had_target_file = "TargetFile" in args
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
    normalized.pop("_explanation", None)

    if tool == "remote.read":
        should_entire = normalized.pop("_should_read_entire_file", None)
        if should_entire and "limit" not in normalized and "end_line" not in normalized:
            normalized["limit"] = 500
        if "node_paths" in normalized and isinstance(normalized["node_paths"], str):
            normalized["node_paths"] = [normalized["node_paths"]]
    elif tool == "remote.write":
        mode = normalized.pop("mode", None)
        if isinstance(mode, str):
            mode_low = mode.strip().lower()
            if mode_low in {"append", "a"} and "append" not in normalized:
                normalized["append"] = True
            elif mode_low in {"overwrite", "write", "w"} and "overwrite" not in normalized:
                normalized["overwrite"] = True
        if had_target_file and "create_dirs" not in normalized:
            normalized["create_dirs"] = True
    elif tool == "remote.multi_edit":
        if isinstance(normalized.get("edits"), list):
            normalized["edits"] = [_normalize_edit_chunk(item) for item in normalized["edits"]]
    elif tool == "remote.bash":
        bg = normalized.pop("run_in_background", None)
        if bg and "yield_time_ms" not in normalized:
            normalized["yield_time_ms"] = 200
    elif tool == "remote.glob":
        for list_key in ("extensions", "excludes"):
            if isinstance(normalized.get(list_key), str):
                normalized[list_key] = [normalized[list_key]]
        if "pattern" not in normalized and any(
            k in normalized for k in ("extensions", "excludes", "entry_type", "max_depth", "full_path")
        ):
            normalized["pattern"] = "*"
    elif tool == "remote.grep":
        case_sens = normalized.pop("_case_sensitive", None)
        if case_sens is not None and "case_insensitive" not in normalized:
            normalized["case_insensitive"] = not bool(case_sens)
        is_regex = normalized.pop("_is_regex", None)
        if is_regex is not None and "fixed_strings" not in normalized:
            normalized["fixed_strings"] = not bool(is_regex)
        match_per_line = normalized.pop("_match_per_line", None)
        if match_per_line is not None and "output_mode" not in normalized:
            normalized["output_mode"] = "content" if bool(match_per_line) else "files_with_matches"
        for list_key in ("includes", "excludes"):
            if isinstance(normalized.get(list_key), str):
                normalized[list_key] = [normalized[list_key]]
    elif tool == "remote.ls":
        recursive = normalized.pop("_recursive", None)
        if recursive and "max_depth" not in normalized:
            normalized["max_depth"] = 3
        if isinstance(normalized.get("ignore_globs"), str):
            normalized["ignore_globs"] = [normalized["ignore_globs"]]

    return normalized


CANONICAL_DESCRIPTIONS: dict[str, str] = {
    "remote.read": "Read a remote file with native Read-like pagination and a read ledger.",
    "remote.write": "Write a remote file with overwrite support and optional read-ledger concurrency checks.",
    "remote.edit": "Edit a remote file with exact string replacement and optional read-ledger concurrency checks.",
    "remote.multi_edit": "Apply multiple exact edits atomically to one remote file.",
    "remote.bash": "Run a remote shell command with Bash-like semantics, logs, preview, and automatic yield, writable stdin and optional PTY.",
    "remote.glob": "Find remote paths with ** glob semantics.",
    "remote.grep": "Search remote files with rg-compatible semantics and a grep -E (POSIX ERE) fallback.",
    "remote.ls": "List a remote directory without reading file contents.",
    "remote.apply_patch": "Apply a Codex apply_patch payload or unified diff on a remote endpoint.",
    "remote.job_status": "Check a remote background job through the shared process supervisor.",
    "remote.job_tail": "Tail remote background job logs through the shared process supervisor.",
    "remote.job_output": "Read remote background job output through the shared process supervisor.",
    "remote.job_stop": "Stop a remote background job through the shared process supervisor.",
    "remote.job_stdin": "Write to a remote session's stdin (Codex write_stdin habit), with optional EOF and output polling.",
    "remote.artifact_manifest": "Build a remote artifact sha256 manifest.",
    "remote.artifact_pull": "Pull a remote artifact through SSH streaming with hash verification; reject local overwrite by default.",
    "remote.artifact_push": "Push a local artifact through SSH streaming with hash verification.",
    "remote.context_snapshot": "Write a compact endpoint context snapshot.",
    "remote.probe": "Probe basic endpoint facts.",
}


def harness_catalog(profile: str | None = None) -> list[dict[str, Any]]:
    """Return the exact tools/list payload for a trusted harness profile.

    Build-time catalog generators call this function; the MCP server and CLI
    use the same function so discovery cannot drift from dispatch.
    """
    from .harness_profiles import (
        CANONICAL_PROFILE,
        native_input_schema,
        resolve_profile,
        specs_for,
    )

    resolved = resolve_profile(profile)
    # Grok's session registry rejects dotted names even when its MCP doctor
    # successfully lists them. Advertise the portable aliases to every client;
    # call_tool still accepts canonical dotted names for existing integrations.
    tools = [
        {"name": alias, "description": CANONICAL_DESCRIPTIONS.get(name, name), "inputSchema": TOOL_SCHEMAS[name]}
        for alias, name in ALIASES.items()
    ]
    if resolved == CANONICAL_PROFILE:
        return tools
    by_name = {tool["name"]: index for index, tool in enumerate(tools)}
    for spec in specs_for(resolved).values():
        entry = {
            "name": spec.wire,
            "description": spec.description,
            "inputSchema": native_input_schema(spec, ENDPOINT_PROPS),
        }
        # A native name that equals a portable canonical alias (for example
        # DSH ``read`` -> ``remote_read``) is the same wire name. Native
        # semantics win on the advertised schema; the server still accepts the
        # canonical fields for compatibility.
        if spec.wire in by_name:
            tools[by_name[spec.wire]] = entry
        else:
            by_name[spec.wire] = len(tools)
            tools.append(entry)
    return tools
