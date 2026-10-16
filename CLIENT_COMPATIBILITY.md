# Remote-dev Client Compatibility

[中文](./docs/client-compatibility.zh.md) · **English** · [Documentation Index](./docs/README.en.md)

`remote-dev` serves every MCP-capable coding agent from a single server and schema
set. Client-specific habits are handled by a shared parameter normalization layer
(`remote_dev.mcp.schemas.normalize_arguments`) applied by both the MCP dispatcher
and the CLI `--input-json` path.

## Client Configuration Matrix

Register the MCP server once per client in the consuming project. Ready-to-use
templates live in [`examples/`](./examples/):

| Client | Project Configuration Path | Example Template | Server Identifier |
| --- | --- | --- | --- |
| Kimi Code | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](./examples/kimi-mcp.example.json) | `remote-dev` |
| Claude Code | `.mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| Cursor IDE / Cursor Agent | `.cursor/mcp.json` | [`examples/mcp.json`](./examples/mcp.json) | `remote-dev` |
| OpenAI Codex | `.codex/config.toml` | [`examples/codex-config.example.toml`](./examples/codex-config.example.toml) | `remote_dev` |
| Grok Build | `.grok/config.toml` | [`examples/grok-config.example.toml`](./examples/grok-config.example.toml) | `remote-dev` |

### Per-Client Setup Notes

- **Kimi Code**: Reads project MCP servers from `.kimi-code/mcp.json` (user-level:
  `~/.kimi-code/mcp.json`), using `startupTimeoutMs` / `toolTimeoutMs` without a
  `type` field. Trusting the project folder enables project-level servers; start a
  new session after adding or modifying servers.
- **Cursor IDE / Cursor Agent**: Enable the repository MCP source under
  **Customize > MCPs > remote-dev**, then start a new agent session. For headless
  `cursor-agent`, run `cursor-agent mcp enable remote-dev` first.
- **Grok Build**: Prefer a native `.grok/config.toml` over relying on automatic
  `.mcp.json` import. Verify registration with `grok inspect --json` and
  `grok mcp doctor remote-dev --json`.
- **OpenAI Codex**: Uses `.codex/config.toml` with `approval_policy = "on-request"`
  so interactive confirmations work normally. For pre-authorized headless
  `codex exec` runs, pass invocation-scoped tool approvals on the command line
  rather than persisting blanket remote-command approvals in shared files:
  ```sh
  codex exec \
    -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
    -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
    '<explicitly authorized task with a bounded remote root>'
  ```

## Shared Compatibility Contract

- **Portable Tool Naming**: Discovery advertises underscore names (`remote_read`,
  `remote_apply_patch`, etc.) so all clients register every tool reliably. The
  dispatcher accepts both underscore (`remote_read`) and canonical dotted
  (`remote.read`) names on `tools/call`, and result envelopes retain canonical
  dotted names.
- **Flat Object Input Schemas**: Tool input schemas are plain typed JSON objects
  without root `anyOf`, `oneOf`, `allOf`, or `$ref`, ensuring compatibility with
  strict provider validators (such as Kimi's schema validator).
- **Server-Side Endpoint & Alias Validation**: Supply `host` + `port`, an `alias`,
  or a selector field claimed by a registered resolver plugin. Fields with
  client-native aliases (`file_path`, `command`) are enforced by the server after
  alias normalization rather than in the wire schema's `required` array, so
  alias-only calls pass provider-side validation.
- **Client-Native Parameter Aliases**:
  - `path` → `file_path` (`read`, `write`, `edit`, `multi_edit`)
  - `line_offset` / `n_lines` → `offset` / `limit` (`read`; negative `offset` reads from the end of the file)
  - `cmd` / `workdir` → `command` / `cwd` (`bash`)
  - `session_id` → `job_id` (`job_stdin`, `job_status`, `job_tail`, `job_stop`)
  - `-i`, `-A`, `-B`, `-C`, `-n`, `head_limit`, `offset`, `include_ignored` (`grep`), supporting both `count` (matching lines per file) and `count_matches` (total matches per file)
- **Atomic Append & Binary Guard**:
  - `remote_write` supports `append=true` (mutually exclusive with `overwrite`).
  - `remote_read` detects binary files (NUL byte in the first 8192 bytes) and
    returns an actionable `binary_file` status instead of corrupting output.
- **Interactive Sessions & Output Cursors**:
  - `remote_bash` waits up to `yield_time_ms` (default `10000`) and returns a
    `session_id` whenever the process is still running or unread output remains.
  - `remote_job_stdin` writes input (`chars`) or polls unread output when `chars`
    is empty. Partial writes report `written_chars` and defer `eof=true` until all
    characters are accepted.
  - Byte cursors advance under a cross-process lock and never split multi-byte
    UTF-8 characters across page boundaries.
