# Remote-dev client compatibility

Updated 2026-09-11 for the session-semantics review batch: deferred EOF on
partially accepted stdin writes, character-exact retry accounting
(`written_chars`), UTF-8-safe incremental paging, and initial-yield cursor
continuation. Originally written 2026-09-10 for the client-parity batch: nineteen `remote_*` tools
(`remote_job_stdin` is new), a shared alias layer for client-native parameter
names, wider grep coverage, write append, read-from-end, and interactive
pipe sessions with writable stdin. One server and one schema set serve every
client; model credentials and provider settings are separate and are not
changed by this setup.

> Evidence below dated 2026-08-27 was gathered while remote-dev lived at
> `.remote-dev/` inside the vllm-ascend-workspace scaffold. Paths in that
> section refer to that layout. The tool names, schemas, framing and dispatch
> rules it exercised are unchanged here; the four `vaws_*` task tools it
> mentions have since been removed, and the scaffold-specific selectors
> (`session_id`, `session_file`, `machine`) are now consumer resolver fields
> rather than built-in schema properties. Its "18 tools" counts predate the
> 2026-09-10 batch, which added `remote_job_stdin` (19 tools).

## Configuration

Register the MCP once per client in the *consuming* project, not once per
model. Starting points live in `examples/`:

| Client | Project configuration | Example | Server identifier |
| --- | --- | --- | --- |
| Kimi Code | `.kimi-code/mcp.json` | `examples/kimi-mcp.example.json` | `remote-dev` |
| Claude Code, including DeepSeek V4 | `.mcp.json` | `examples/mcp.json` | `remote-dev` |
| Cursor IDE / Cursor Agent | `.cursor/mcp.json` | `examples/mcp.json` | `remote-dev` |
| Codex | `.codex/config.toml` | `examples/codex-config.example.toml` | `remote_dev` |
| Grok Build | `.grok/config.toml` | `examples/grok-config.example.toml` | `remote-dev` |

Kimi Code reads project MCP servers from `.kimi-code/mcp.json` (user level:
`~/.kimi-code/mcp.json`), not `.mcp.json`; its entry shape uses
`startupTimeoutMs` / `toolTimeoutMs` and no `type` field. Trusting the folder
enables the project-level servers; servers added mid-session only join new
sessions.

All entries launch `remote-dev server` from the installed `vaws-remote-dev`
package (or `uvx --from git+https://github.com/mindie-agent/remote-dev@<ref>
remote-dev server`). Add `REMOTE_DEV_RESOLVERS`, `REMOTE_DEV_STATE_DIR`,
`REMOTE_DEV_ENDPOINTS_FILE` and `REMOTE_DEV_RUNTIME_ENV_FILE` to the server
`env` when the consumer needs them (see README). Do not add model credentials
or real endpoint data to these files. Restart existing sessions after
schema/config changes so they rediscover tools.

Use `cursor-agent` explicitly when Grok also provides an `agent` command; the
shared MCP does not require changing either client's shell aliases.

If Cursor's model cannot discover the tools, check **Customize > MCPs >
remote-dev** and enable the repository source. A disabled source can still be
listed in the UI; it is not evidence that the model can call it. Start a new
agent after enabling it. Cursor Agent separately requires server approval
(`cursor-agent mcp enable remote-dev`) and a valid client login; these are not
MCP schema problems.

Grok's automatic `.mcp.json` import depends on its Claude-import state; a native
`.grok/config.toml` avoids that ambiguity. Confirm project trust on first use.
Check registration with `grok inspect --json` and `grok mcp doctor remote-dev --json`,
then test `search_tool` in a real session: doctor success alone is insufficient.

Codex uses its native project configuration, following the
[official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
The project example uses `approval_policy = "on-request"` so interactive MCP
confirmation is possible. It does **not** automatically approve remote writes.
In a headless `codex exec` run, a pending MCP confirmation can appear as
`user cancelled MCP tool call`; that is not a schema rejection. A bounded,
pre-authorized automation can supply invocation-only approvals for its exact
tools, for example:

```sh
codex exec \
  -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
  -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
  '<explicitly authorized task with a bounded remote root>'
```

Do not put blanket remote-command approvals in shared configuration. Normal
interactive calls should use the client's confirmation flow.

## Shared compatibility contract

- Advertised tool names use `remote_read`, `remote_apply_patch`, etc. Grok 1.0.5
  can complete the MCP handshake and list dotted names in doctor, yet fail to
  register them in a model session. An isolated naming-only comparison confirmed
  that underscore names expose all tools to `search_tool` (18 at the time; 19
  since 2026-09-10).
- The dispatcher still accepts legacy `remote.read` and `remote.apply_patch`
  names. Result envelopes retain canonical dotted tool names. Kimi/Claude
  namespaced names remain `mcp__remote-dev__remote_read`; Grok discovers
  `remote-dev__remote_read`; Codex registers the same tool under `remote_dev`.
- Input schemas are ordinary typed objects without `anyOf`, `oneOf`, `allOf`,
  or `$ref`. Kimi's provider rejects the former root `type` plus `anyOf` shape
  before the requested tool is executed, including on an ordinary chat prompt.
- Conditional requirements are documented in schema descriptions and enforced
  by the existing server: supply `host` + `port`, an alias, or a selector
  field claimed by a registered resolver plugin; supply non-empty `patch` or
  legacy `command` for patches. `patch` takes
  precedence. Missing endpoint/payload is rejected before remote execution.
  Fields that have client-native aliases (`file_path`, `command`) are also
  enforced server-side rather than through the wire schema's `required` list,
  so a provider cannot reject an alias-only call before normalization runs.
- Client-native habit mapping (one shared alias layer, not five
  implementations): `path` → `file_path`; `line_offset`/`n_lines` →
  `offset`/`limit` (a negative offset reads from the end of the file, Kimi
  style); `cmd`/`workdir` → `command`/`cwd`; grep accepts `-i`, `-A`, `-B`,
  `-C`, `-n`, `head_limit`, `offset`, `include_ignored`, and both `count`
  (matching lines per file, Claude style) and `count_matches` (total matches
  per file, Kimi style — `rg --count-matches`, with a per-file `-o` counting
  fallback; the two modes differ whenever one line holds several matches).
- `remote_write append=true` appends atomically (Kimi `mode=append`);
  `remote_read` on a binary file returns an actionable `binary_file` status —
  there is no remote image/media preview tool.
- In 0.6, `remote_bash command=... yield_time_ms=...` automatically yields
  `session_id` when the process or output remains. Stdin is writable by default;
  `remote_job_stdin session_id=... chars=...` writes input or polls when empty.
  `eof=true` closes pipe input. `tty=true` allocates a 24x80 remote terminal,
  merges stderr into stdout, and gives Ctrl-C/Ctrl-D terminal semantics.
  The old `run_in_background`/`interactive` flags and monitor tool are removed.
- Output cursors survive process restarts and concurrent polls. Unicode
  characters split at a page boundary remain for the next page. Partial writes
  report `written_chars`; retry the exact remainder. EOF is deferred until that
  remainder is accepted. `max_output_tokens` uses four UTF-8 bytes per token
  across text plus structured previews, shared by stdout/stderr; each preview
  has a four-byte minimum and bounded status/refs metadata is separate.
- MCP accepts concurrent calls and scoped cancellation in both framing modes.
  Windows and WSL use the same pooled binary SSH RPC, without depending on
  OpenSSH ControlMaster. The remote execution shell remains Bash on Linux;
  local PowerShell/WSL quoting is avoided by JSON tool arguments. Connection
  startup and remote shell profiles still contribute latency.
- `remote_multi_edit.edits` exposes typed item fields; omitted `new_string`
  retains the existing empty-string deletion behavior.
- Path containment, symlink checks, read ledgers, patch atomicity, and SSH
  execution behavior are unchanged for client wiring. Read-ledger directory
  names now use a uniform `id-<sha256>` encoding per nonempty context id,
  and current-encoding records use schema `remote-dev.read_ledger.v2`; see
  README. No client-specific schema fork is needed.

## Transport/MCP compatibility verification (pre-lifecycle), 2026-08-27

PR #64 code/config revision `1f93400` was exercised from the VAWS checkout. Each
model performed four sequential MCP calls in a newly created remote scratch
directory: patch a new file, read it, replace its content with `remote_multi_edit`,
and read it again. Final content and SHA256 were independently checked remotely.
A successful process exit or a server listed as connected does not count as a
passing test.

This section is the earlier remote-dev transport/schema contract evidence. Its
four-call rows do not establish the later native-session lifecycle. In
particular, a Kimi fallback call, a Cursor IDE result, or a client that still
needs native hook/workspace/MCP approval must not be reported as a native
lifecycle pass.

| Client / model | Version | Four-call result |
| --- | --- | --- |
| Kimi Code / `kimi-for-coding` | 0.38.0 | Passed, `VAWS_PR64_kimi_clean_OK` |
| Claude Code / `deepseek-v4-flash` | 2.1.143 | Passed, `VAWS_PR64_claude_OK` |
| Codex / `gpt-5.6-sol` | 0.147.0 | Passed, `VAWS_PR64_codex_OK` |
| Grok Build / `grok-4.6` (`grok-4.6-build` usage ID) | 1.0.5 | Discovery and calls passed, `VAWS_PR64_grok_OK` |
| Cursor IDE / Cursor Grok 4.6 | 3.17.19 | Discovery and calls passed, `VAWS_PR64_cursor_OK` |

The initial Kimi attempt incorrectly escaped patch newlines; the server rejected
that payload without writing, and the model corrected it. A fresh-file rerun
with an explicit multiline patch passed all four calls without a retry.

Cursor Agent `2026.08.25-3e8eec8` loaded all 18 tools with the relative path, but
its model request failed because its saved login had expired. That CLI model run
is **not** counted as passing: the completed Cursor row is the native IDE test,
using its existing authenticated session after enabling the workspace MCP source.

Grok and Codex headless checks used invocation-scoped approvals for only
`remote_apply_patch`, `remote_read`, and `remote_multi_edit`. No blanket approvals
were persisted. The same server code also passed a separate Codex patch/read
test with normal allow-once interactive confirmations.

Remote Python 3.9.9 unit validation:

- 63 focused tests pass: MCP schema/framing/aliases, native Cursor entry, missing
  arguments, patch atomicity/path guards, endpoints, read/write/edit, artifacts,
  hooks, and CLI wrappers/errors. The same 63 tests pass in GitHub Actions on
  both Python 3.9 and 3.12.
- MCP burden validation passes: 18 tools, 18 CLI wrappers, no missing documented
  endpoint requirements, maximum 3 tool-specific required arguments.
- Full suite: 82 tests, 8 failures within the two pre-existing Claude skill-shim
  checks in `test_cli_help.py`. Base commit `2176b48` runs 73 tests and reproduces
  exactly the same 8 failures. No unrelated skill packages were changed to mask
  this baseline.

The focused CI command was in the scaffold's `.github/workflows/remote-dev.yml`;
this repository's `.github/workflows/ci.yml` runs the whole suite with
`python3 -m unittest discover -s tests`. These unit tests use temporary files and mocked
transports and do not require NPU access.

These checks establish this MCP integration at the versions above, not every
provider feature, arbitrary future versions, or NPU/model-serving correctness.
No existing containers, NPU workloads, model weights, or runtime checkouts were
modified by the smoke tests.
