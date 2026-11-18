# Trusted Harness Profiles and Native Basic Tools

`remote-dev` exposes 20 canonical `remote.*` tools when no harness profile is
bound. A client that wants the native basic-tool surface binds exactly one
harness profile; the profile is selected only by the server-side environment
variable `REMOTE_DEV_HARNESS_PROFILE` (or an explicit in-process `profile=`
argument), never by inspecting tool arguments.

| Profile | Bound by | Exact advertised surface |
| :--- | :--- | :--- |
| `canonical` | standalone default | only the 20 portable `remote_*` names |
| `codex` | Codex plugin MCP launcher | native `remote_exec_command`, `remote_write_stdin`, `remote_view_image`, `remote_apply_patch`; support `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_stop` (10) |
| `claude` | Claude Code plugin MCP launcher | native `remote_Read`, `remote_Write`, `remote_Edit`, `remote_NotebookEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_TaskStop`; support 4 base (13) |
| `kimi` | Kimi Code plugin MCP launcher | native `remote_Read`, `remote_Write`, `remote_Edit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_ReadMediaFile`, `remote_TaskOutput`, `remote_TaskStop`, `remote_TaskList`; support 4 base (14) |
| `dsh` | DSH plugin launcher | native `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_read_image`, `remote_job_output`, `remote_job_list`, `remote_job_kill`; support 4 base (14) |
| `wb` | WorkBuddy plugin launcher | native `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_NotebookEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_TaskStop`, `remote_ViewImage`; support 4 base (15) |
| `agy` | Google Antigravity MCP launcher | native `remote_read_file`, `remote_write_file`, `remote_edit_file`, `remote_run_command`, `remote_manage_task`, `remote_find_by_name`, `remote_grep_search`, `remote_list_dir`; support 4 base (12) |
| `oc` | OpenCode MCP launcher | native `remote_read`, `remote_write`, `remote_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_list`; support 4 base plus `remote_job_status`, `remote_job_output`, `remote_job_stop` (14) |

The four base support tools are `remote_auth`, `remote_probe`,
`remote_artifact_pull` and `remote_artifact_push`. Every bound profile
advertises only its native-mirrored tools plus the support tools listed above;
cross-harness canonical clutter is not carried, and `tools/call` rejects any
name outside that same set. Retired `remote_BashOutput` / `remote_KillShell`
aliases no longer appear on the Claude Code or WorkBuddy surface:
`TaskOutput` / `TaskStop` are the single job entry points.

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
- Antigravity `run_command.WaitMsBeforeAsync` and OpenCode `bash.timeout` are
  milliseconds. `IsDaemon=true` yields a managed background job after the wait
  budget (default 200 ms); OpenCode `run_in_background` yields immediately.
  OpenCode accepts both `filePath`/`file_path` (and `oldString`/`old_string`)
  spellings, and its `write` defaults to `overwrite=true`.

## Job and stdin semantics

- Claude `TaskOutput` (`block=true`, 30000 ms, max 600000) waits for
  completion when blocking. The returned text is the native
  `<retrieval_status>/<task_id>/<task_type>/<status>/<output>` envelope.
  `TaskStop` treats an already-finished task as a successful no-op.
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
`read_image`, Codex `view_image`) pull the file through the bounded streaming
artifact path, then crop/downscale **locally** with `sips` (optional Pillow or
ImageMagick fallback) and return a bounded MCP image block plus a text
receipt. Kimi `region` and `full_resolution` are honored; `ReadMediaFile` on a
video returns a local QuickLook/ffmpeg thumbnail, explicitly best-effort.
Claude `Read` renders `.ipynb` cells/outputs structurally and `NotebookEdit`
replaces/inserts/deletes cells by reading and atomically rewriting canonical
notebook JSON; the read updates the prior-read ledger. Claude `Read` on PDF
renders selected pages locally with `pdftoppm` (optional pypdfium2/PyMuPDF)
after a bounded pull. When no local renderer/decoder exists the tool returns
an explicit failure; no renderer is installed remotely and no model weights
are loaded.

Claude `TaskList` is the host session task/plan registry, a high-level host interface outside the remote-file/process scope; it is intentionally not exposed as a native projection. Kimi `TaskList` is a background-process roster and remains in scope. `TaskOutput` remains the background-output tool.

Host-local permission/escalation arguments are never interpreted as remote
authorization. Claude `dangerouslyDisableSandbox`, DSH `sandbox_permissions`/
`justification`, and Codex `sandbox_permissions` other than `use_default`,
`additional_permissions`, `justification`, or `prefix_rule` are rejected with
an explicit error. Codex `shell` and `login` are normal command-process
parameters and are executed on the remote endpoint (`shell -c`, `-l` for
login); they are not host-local approval controls.

## Standalone and compatibility

Without `REMOTE_DEV_HARNESS_PROFILE`, only the 20 canonical portable names are
advertised and accepted (`remote_read`, `remote.read`, …). Binding a harness
profile replaces that surface with the exact native-plus-support set in the
table above; native-case aliases such as `remote_Edit`, dotted canonical names
that are not that host's wire name, and any other harness's tools are rejected
before endpoint resolution.

## Scope boundary (user-defined)

Only tools whose operation target is the remote machine are projected: files,
directories, search, edits, patches, processes, commands, stdin, and background
jobs. Subagent creation, plan/task registries, conversation/UI, host approvals
and browser tools stay local to the Harness. Claude `TaskList` is therefore
**not** exposed as a remote-job list; Kimi `TaskList` is a background-process
roster and remains in scope.

Media is different: the remote file is pulled through the bounded streaming
artifact path, then converted/rendered **locally** with `sips`, `pdftoppm`,
`qlmanage`, optional Pillow/pypdfium2/PyMuPDF, or `ffmpeg` when available.
No renderer is installed on the remote host and no model weights are loaded.

## Name collisions and canonical compatibility

Standalone remote-dev keeps the 20 canonical portable `remote_*` names. In a
native profile a wire name that collides with a canonical portable alias (DSH
`remote_read`, OpenCode `remote_read`, Codex `remote_apply_patch`, …) is the
**native** tool with the strict native schema. Canonical dotted names are not a
compatibility bypass under a bound profile: call the advertised native wire
name. Native names do not accept canonical-only fields: `remote_exec_command`
rejects `timeout_ms`, `remote_write_stdin` rejects `eof`, and DSH native names
reject `cmd`/`timeout_ms`/`lines`.
