# Client Setup & Parameter Compatibility Guide

[中文](./docs/client-compatibility.zh.md) · **English** · [Documentation Index](./docs/README.en.md)

Different AI coding Harnesses (**OpenAI Codex**, **Claude Code**, **Cursor**, **Kimi Code**, **DeepSeek Harness / DSH**, **Google Antigravity**, and **Grok Build**) have distinct native local tool names, parameter shapes, and behavioral expectations — some send `path` or `AbsolutePath` / `TargetFile` instead of `file_path`, some slice files with `StartLine` / `EndLine` or `line_offset` / `n_lines`, some scope file replacements to `[StartLine, EndLine]`, and some inspect AST symbols (`view_file_outline` / `view_code_item`).

`remote-dev` handles all of these differences automatically through a unified normalization and execution layer (`remote_dev.mcp.schemas.normalize_arguments` + `HARNESS_TOOL_ALIASES`) shared by both the MCP server and the CLI `--input-json` interface. One server provides identical remote twins across every supported Harness.

---

## 6-Harness Native Tool & Parameter Mapping

| Capability | Canonical `remote-dev` Tool | Accepted Harness Tool Aliases | Supported Native Parameter Habits Across All 6 Harnesses |
| --- | --- | --- | --- |
| **Read / Slice / Outline / Code Item** | `remote.read` (`remote_read`) | `remote_view_file`, `remote_read_file`, `remote_view_file_outline`, `remote_view_code_item` | `file_path` / `path` / `AbsolutePath` / `TargetFile` / `target_file` / `File`; `offset` + `limit` (CC), `line_offset` + `n_lines` including negative tail offsets (Kimi), `StartLine` + `EndLine` / `start_line_one_indexed` + `end_line_one_indexed_inclusive` (Antigravity / Cursor / DSH), `ContentOffset` byte/char offset (Antigravity), `should_read_entire_file` (Cursor), `outline=true` + `ItemOffset` (Antigravity `view_file_outline`), `NodePaths` / `symbol` AST & regex symbol extraction (Antigravity `view_code_item`). |
| **Write / Overwrite / Append** | `remote.write` (`remote_write`) | `remote_write_to_file`, `remote_write_file` | `file_path` / `path` / `TargetFile` / `target_file` / `AbsolutePath`; `content` / `CodeContent` / `contents`; `overwrite` / `Overwrite`, `append` / `Append`, `mode="overwrite"\|"write"\|"append"` (Kimi), automatic `create_dirs=true` when `TargetFile` is used (Antigravity), optional `Description` / `explanation`. |
| **Single Edit (Global or Line-Scoped)** | `remote.edit` (`remote_edit`) | `remote_replace_file_content`, `remote_str_replace_file` | `file_path` / `path` / `TargetFile` / `target_file`; `old_string` / `TargetContent` / `old_str`, `new_string` / `ReplacementContent` / `new_str`, `replace_all` / `AllowMultiple`; optional 1-indexed inclusive `StartLine` (`start_line`) and `EndLine` (`end_line`) scoping the match to that line range even when `old_string` appears elsewhere in the file (Antigravity). |
| **Atomic Multi-Chunk Edit** | `remote.multi_edit` (`remote_multi_edit`) | `remote_multi_replace_file_content` | `file_path` / `path` / `TargetFile`; `edits` / `ReplacementChunks` (each chunk supports `old_string`/`TargetContent`/`old_str`, `new_string`/`ReplacementContent`/`new_str`, `replace_all`/`AllowMultiple`, and line-delta-aware `StartLine`/`EndLine`). |
| **Patch Application** | `remote.apply_patch` (`remote_apply_patch`) | `remote.apply_patch` | `patch` or `command` supporting both Codex `*** Begin Patch` grammar and standard Git unified diffs. |
| **Content Search (Regex & Literal)** | `remote.grep` (`remote_grep`) | `remote_grep_search` | `pattern` / `Query` / `query`; `path` / `SearchPath` / `target_directory`; `fixed_strings` / `-F` / `literal` / `IsRegex=false` (literal substring search in both `rg` and `grep -F` fallback); `MatchPerLine` (`true` → `content`, `false` → `files_with_matches`); `Includes` / `include_pattern` and `Excludes` / `exclude_pattern`; `-i` / `CaseInsensitive` / `case_sensitive`; `-A`, `-B`, `-C`, `-n`, `head_limit`, `offset`, `count`, `count_matches`. |
| **Path & File Search** | `remote.glob` (`remote_glob`) | `remote_find_by_name`, `remote_file_search` | `pattern` / `Pattern` / `query` (optional when filters are given); `path` / `SearchDirectory` / `target_directory`; `Extensions` / `extensions`, `Excludes` / `exclude_pattern`, `Type` (`file` / `directory` / `any`), `MaxDepth` / `depth`, `FullPath` / `full_path`, `respect_gitignore`. |
| **Directory Listing** | `remote.ls` (`remote_ls`) | `remote_list_dir` | `path` / `DirectoryPath` / `dir_path` / `target_directory`; `MaxDepth` / `depth` / `recursive`, `Excludes` / `ignore_globs`, `all`, `limit`. |
| **Shell Execution & Background Tasks** | `remote.bash` (`remote_bash`) | `remote_exec_command`, `remote_run_command`, `remote_run_terminal_cmd`, `remote_shell` | `command` / `cmd` / `CommandLine`; `cwd` / `workdir` / `working_dir` / `Cwd`; `yield_time_ms` / `WaitMsBeforeAsync`; `run_in_background` / `is_background` / `IsDaemon` (launches immediately as a managed background job and returns `job_id` / `session_id`); `tty`, `max_output_tokens`, `env`. |
| **Interactive Stdin & Task Control** | `remote.job_status` / `job_stdin` / `job_tail` / `job_stop` | `remote_manage_task`, `remote_write_stdin`, `remote_bash_output`, `remote_task_output`, `remote_kill_shell`, `remote_task_stop` | `job_id` / `session_id` / `TaskId` / `task_id` / `bash_id` / `shell_id`; `chars` / `Input` / `input` / `stdin`; `Action="list"\|"status"\|"kill"\|"stop"\|"send_input"\|"tail"` (Antigravity `manage_task` parity). |

---

## Where to Put Your Client Configuration

Register the MCP server once per project using the ready-made templates in [`examples/`](./examples/):

| Client | Project Config Path | Example Template | Server Name |
| --- | --- | --- | --- |
| **Claude Code** | `.mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Cursor IDE / Agent** | `.cursor/mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Kimi Code** | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](./examples/kimi-mcp.example.json) | `remote-dev` |
| **OpenAI Codex** | `.codex/config.toml` | [`examples/codex-config.example.toml`](./examples/codex-config.example.toml) | `remote_dev` |
| **DeepSeek Harness (DSH)** | `.mcp.json` / DSH MCP config | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Google Antigravity** | `mcp_config.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Grok Build** | `.grok/config.toml` | [`examples/grok-config.example.toml`](./examples/grok-config.example.toml) | `remote-dev` |

### Tips for Specific Clients

- **Kimi Code**: Reads `.kimi-code/mcp.json` in your project (or `~/.kimi-code/mcp.json` globally). Use `startupTimeoutMs` / `toolTimeoutMs` and **omit the `type` field**. Start a new session after updating the file.
- **Cursor IDE / Cursor Agent**: Enable the server under **Customize > MCPs > remote-dev** and start a new agent session. For the CLI `cursor-agent`, run `cursor-agent mcp enable remote-dev` first.
- **Google Antigravity**: Supports `StartLine`/`EndLine`, `ContentOffset`, `outline=true` (`view_file_outline`), `NodePaths` (`view_code_item`), line-scoped `replace_file_content` / `multi_replace_file_content`, `IsRegex=false` literal grep, `find_by_name` filters, and `manage_task(Action=...)`.
- **Grok Build**: Use `.grok/config.toml` directly and verify setup with `grok inspect --json` and `grok mcp doctor remote-dev --json`.
- **OpenAI Codex**: Uses `.codex/config.toml` with `approval_policy = "on-request"` for interactive prompts. When running headless `codex exec` automation, pass tool approvals on the command line for that specific run rather than saving blanket remote-execution approvals in shared config files:
  ```sh
  codex exec \
    -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
    -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
    '<explicitly authorized task with a bounded remote root>'
  ```

---

## Built-In Compatibility Features

1. **Underscore, Dotted, and Harness-Native Tool Names Work**:
   During tool discovery, `remote-dev` advertises portable underscore names (`remote_read`, `remote_apply_patch`, etc.). When calling a tool, canonical dotted names (`remote.read`), underscore names (`remote_read`), and Harness-native remote aliases (`remote_view_file`, `remote_replace_file_content`, `remote_exec_command`, `remote_manage_task`, etc.) are all accepted.
2. **Strict-Validator Friendly Flat Schemas**:
   Every tool input schema is a flat JSON object with no root `anyOf`, `oneOf`, `allOf`, or `$ref`, ensuring smooth acceptance by strict schema validators.
3. **Coordinator Zero-Ceremony Endpoint Resolution**:
   When paired with `mindie-coordinator` (`REMOTE_DEV_RESOLVERS="mindie_coordinator.endpoint_bridge:setup_resolvers"`), `remote-dev` automatically resolves machine aliases (`<alias>`, `<alias>-host`, `<alias>-docker`, and `default_alias`) directly from `coordinator`'s `machines.json` / `endpoints.json`.
4. **Safe Appends, Line-Scoped Edits & Binary File Guard**:
   - `remote_write` supports `append=true` / `mode="append"` (mutually exclusive with `overwrite`).
   - `remote_edit` and `remote_multi_edit` support `start_line` / `end_line` (`StartLine` / `EndLine`) to disambiguate replacements within a specific line range.
   - `remote_read` checks the first 8192 bytes for NUL bytes and returns a clean `binary_file` status instead of dumping garbled binary data into the model's context.
5. **Interactive Sessions & UTF-8 Safe Cursors**:
   - `remote_bash` waits up to `yield_time_ms` (default `10000` ms, or `200` ms when `run_in_background=true` / `IsDaemon=true`) and returns a `session_id` if the process is still running or has unread output.
   - `remote_job_stdin` sends input (`chars` / `Input`) or polls unread output when `chars` is empty. Output byte cursors never split multi-byte UTF-8 characters across reads.
