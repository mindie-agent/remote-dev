# Trusted Harness Profiles and Native Basic Tools

`remote-dev` exposes 19 canonical `remote.*` tools. A client that wants the
native basic-tool surface binds exactly one harness profile; the profile is
selected only by the server-side environment variable
`REMOTE_DEV_HARNESS_PROFILE` (or an explicit in-process `profile=` argument),
never by inspecting tool arguments.

| Profile | Bound by | Native wire names additionally exposed |
| :--- | :--- | :--- |
| `canonical` | standalone default | none; 19 portable `remote_*` names |
| `codex` | Codex plugin MCP launcher | `remote_exec_command`, `remote_write_stdin`, `remote_view_image`; `remote_apply_patch` is the canonical tool with a `patch` string because native `apply_patch` is freeform |
| `claude` | Claude Code plugin MCP launcher | `remote_Read`, `remote_Write`, `remote_Edit`, `remote_NotebookEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_BashOutput`, `remote_TaskList`, `remote_TaskStop`, `remote_KillShell` |
| `kimi` | Kimi Code plugin MCP launcher | `remote_Read`, `remote_Write`, `remote_Edit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_ReadMediaFile`, `remote_TaskOutput`, `remote_TaskStop`, `remote_TaskList` |
| `dsh` | DSH plugin launcher | `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_read_image`, `remote_job_output`, `remote_job_list`, `remote_job_kill` |

The canonical 19 tools remain callable in every profile. A native wire name
that collides with a portable canonical alias (for example DSH `remote_read`)
advertises the native schema and still accepts the canonical fields, so an
existing `remote_*` call does not break.

## Units, defaults and pagination are not interchangeable

- Claude `Bash.timeout` and Codex `exec_command.yield_time_ms` are
  milliseconds. Kimi `Bash.timeout` is seconds. The profile converts each
  field to canonical `timeout_ms`/`yield_time_ms`; no code guesses the host
  from a numeric value.
- Claude Bash defaults to 120000 ms (max 600000). Kimi Bash defaults to 60 s
  foreground / 600 s background (foreground max 300 s, background max 86400 s)
  and supports `disable_timeout` for background jobs. DSH `bash.timeoutMs`
  defaults to 120000 (max 600000) and promotes a foreground command to a
  background job instead of killing it. Codex `exec_command` has no
  model-facing timeout parameter; `yield_time_ms` defaults to 10000 and is
  clamped to 250-30000.
- DSH `read` defaults to and caps at 2000 lines. Claude `Read` defaults to
  2000 lines and also supports images, PDF page ranges (up to 20 pages) and
  notebooks. Kimi `Read` is text-only, has no fixed line cap, defaults to
  100000 characters and allows 500000; images/videos use `ReadMediaFile`.
- DSH `write`/`edit` and Claude `Write`/`Edit` require a prior read of an
  existing file; Kimi does not. Kimi `Write` defaults to `mode=overwrite` and
  creates parents; DSH/Claude writes also create parents and replace.
- Kimi `Glob` has `head_limit` (0 = no match-count limit) and `offset`.
  Claude `Glob` is a hard 100-file page with no pagination. DSH `glob` is a
  100-path page with no offset. DSH and Kimi include hidden/ignored files by
  default; Claude's glob also defaults to hidden/no-ignore.
- Kimi and Claude `Grep` default to 250 entries and accept `head_limit=0`;
  Kimi uses `count_matches`, Claude uses `count`; DSH `grep` returns 250
  matching lines with no offset/limit parameters.

## Job and stdin semantics

- Claude `TaskOutput` (`block=true`, 30000 ms, max 600000) and its
  `BashOutput` alias wait for completion when blocking. The returned text is
  the native `<retrieval_status>/<task_id>/<task_type>/<status>/<output>`
  envelope. `TaskStop`/`KillShell` treats an already-finished task as a
  successful no-op.
- Kimi `TaskOutput` is always non-blocking: it returns a status, the last
  32768-byte preview and the full log path. `TaskStop` accepts a reason and
  returns the current status for an already-finished task. `TaskList` lists
  remote jobs, not host subagents.
- DSH `job_output` is a consumptive incremental read (`wait=false` by
  default; `timeout_ms` defaults to 30000 and caps at 600000). It returns only
  bytes after the previous read, then a public job projection. `job_list`
  returns the scoped roster and `job_kill` reports
  `cancellation-requested`/`already-finished`.
- Codex `write_stdin` maps `session_id` to the remote job id, writes `chars`
  and returns the recent output; an empty `chars` polls without writing.
- Native job tools enforce the trusted `REMOTE_DEV_SESSION_ID` scope recorded
  on the job. A job created under another session is not listed, polled,
  written to, or stopped.

## Media and host-only fields

Image reads (Claude `Read` on PNG/JPEG/GIF/WebP, Kimi `ReadMediaFile`, DSH
`read_image`, Codex `view_image`) return a bounded MCP image block plus a text
receipt. Claude `Read` renders `.ipynb` cells and outputs as text, and
`NotebookEdit` replaces/inserts/deletes cells by reading and atomically
rewriting the notebook JSON. PDF page rendering and Kimi image-region crops
are not implemented remotely; the tools return an explicit error instead of
pretending success. Images are returned at original resolution unless the
native size bound is exceeded; automatic downscaling is not replicated.

Claude `TaskList` is the host task registry in the native client; over
remote-dev it lists the scoped remote jobs (the remote task registry), not
unrelated host subagent tasks. `TaskOutput` remains the background-output
tool.

Host-local permission/escalation arguments are never interpreted as remote
authorization. Claude `dangerouslyDisableSandbox`, DSH `sandbox_permissions`/
`justification`, and Codex `sandbox_permissions` other than `use_default`,
`additional_permissions`, `justification`, or `prefix_rule` are rejected with
an explicit error. Codex `shell`/`login`, which select a host shell, are also
rejected; the remote endpoint's configured shell is used.

## Standalone and compatibility

Without `REMOTE_DEV_HARNESS_PROFILE`, only the 19 canonical portable names are
guaranteed. Native-case aliases such as `remote_Edit` require the owning
profile; legacy aliases that do not belong to one of the four profiles (for
example `remote_replace_file_content`) keep their canonical mapping.
