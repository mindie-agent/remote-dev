# Client Setup & Parameter Compatibility Guide

[中文](./docs/client-compatibility.zh.md) · **English** · [Documentation Index](./docs/README.en.md)

Different AI coding clients (Claude Code, Codex, Kimi Code, Cursor, Grok Build) have slightly different habits when calling tools — some send `path` instead of `file_path`, some send `cmd` instead of `command`, and some enforce strict rules on JSON Schemas.

`remote-dev` handles all of these differences automatically through a shared parameter normalization layer (`remote_dev.mcp.schemas.normalize_arguments`) used by both the MCP server and the CLI `--input-json` interface. One server works seamlessly across every client.

---

## Where to Put Your Client Configuration

Register the MCP server once per project using the ready-made templates in [`examples/`](./examples/):

| Client | Project Config Path | Example Template | Server Name |
| --- | --- | --- | --- |
| **Claude Code** | `.mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Cursor IDE / Agent** | `.cursor/mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| **Kimi Code** | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](./examples/kimi-mcp.example.json) | `remote-dev` |
| **OpenAI Codex** | `.codex/config.toml` | [`examples/codex-config.example.toml`](./examples/codex-config.example.toml) | `remote_dev` |
| **Grok Build** | `.grok/config.toml` | [`examples/grok-config.example.toml`](./examples/grok-config.example.toml) | `remote-dev` |

### Tips for Specific Clients

- **Kimi Code**: Reads `.kimi-code/mcp.json` in your project (or `~/.kimi-code/mcp.json` globally). Use `startupTimeoutMs` / `toolTimeoutMs` and **omit the `type` field**. Start a new session after updating the file.
- **Cursor IDE / Cursor Agent**: Enable the server under **Customize > MCPs > remote-dev** and start a new agent session. For the CLI `cursor-agent`, run `cursor-agent mcp enable remote-dev` first.
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

1. **Both Underscore and Dotted Tool Names Work**:
   During tool discovery, `remote-dev` advertises underscore names (`remote_read`, `remote_apply_patch`, etc.) so every client registers all 19 tools without issue. When calling a tool, both `remote_read` and `remote.read` are accepted.
2. **Strict-Validator Friendly Flat Schemas**:
   Every tool input schema is a flat JSON object with no root `anyOf`, `oneOf`, `allOf`, or `$ref`, ensuring smooth acceptance by strict schema validators (such as Kimi's).
3. **Automatic Parameter Alias Translation**:
   - `path` → `file_path` (`read`, `write`, `edit`, `multi_edit`)
   - `line_offset` / `n_lines` → `offset` / `limit` (`read`; pass a negative `offset` like `-50` to read from the end of a file)
   - `cmd` / `workdir` → `command` / `cwd` (`bash`)
   - `session_id` → `job_id` (`job_stdin`, `job_status`, `job_tail`, `job_stop`)
   - `grep` natively supports `-i`, `-A`, `-B`, `-C`, `-n`, `head_limit`, `offset`, `include_ignored`, as well as both `count` (matching lines per file) and `count_matches` (total matches per file)
4. **Safe Appends & Binary File Guard**:
   - `remote_write` supports `append=true` (mutually exclusive with `overwrite`).
   - `remote_read` checks the first 8192 bytes for NUL bytes and returns a clean `binary_file` status instead of dumping garbled binary data into the model's context.
5. **Interactive Sessions & UTF-8 Safe Cursors**:
   - `remote_bash` waits up to `yield_time_ms` (default `10000` ms) and returns a `session_id` if the process is still running or has unread output.
   - `remote_job_stdin` sends input (`chars`) or polls unread output when `chars` is empty. Output byte cursors never split multi-byte UTF-8 characters across reads.
