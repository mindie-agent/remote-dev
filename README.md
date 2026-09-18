# remote-dev

A remote development substrate for coding agents. It makes a remote Linux
host reachable over SSH feel like the local working tree: every native editor
tool has a remote twin with the same semantics plus endpoint fields.

Install the `vaws-remote-dev` package. The import package is `remote_dev`;
the console entry is `remote-dev`.

| Local tool  | Remote tool          | CLI                                      |
|-------------|----------------------|------------------------------------------|
| Read        | `remote.read`        | `remote-dev read`                        |
| Write       | `remote.write`       | `remote-dev write`                       |
| Edit        | `remote.edit`        | `remote-dev edit`                        |
| MultiEdit   | `remote.multi_edit`  | `remote-dev multi-edit`                  |
| Bash        | `remote.bash`        | `remote-dev bash`                        |
| Glob        | `remote.glob`        | `remote-dev glob`                        |
| Grep        | `remote.grep`        | `remote-dev grep`                        |
| LS          | `remote.ls`          | `remote-dev ls`                          |
| apply_patch | `remote.apply_patch` | `remote-dev apply-patch`                 |
| write_stdin | `remote.job_stdin`   | `remote-dev job-stdin`                   |

Plus background jobs (`remote.job_status` / `job_tail` / `job_stop` /
`job_stdin`), artifacts (`remote.artifact_manifest` / `artifact_pull` /
`artifact_push`), and endpoint facts (`remote.probe`,
`remote.context_snapshot`). Nineteen tools in total, served by one stdio MCP
server (`remote-dev server`) and mirrored one-to-one by CLI subcommands.
`python -m remote_dev` is equivalent to `remote-dev`.

Help and parser errors do not import execution backends or contact endpoints.
The selected operation loads its implementation after argument parsing.

`remote-dev probe --host <host> --diagnose-connection` sends one fixed read-only
probe for OS, working directory and Python version. It reports local preparation,
SSH process duration, received TCP/authentication milestones, remote probe
execution and final stream drain/exit timing. Connection time includes client
startup and authentication; TCP time is a subset, not an additional phase.
Absent milestones and pure transfer time remain unknown. The unattributed
remainder includes channel/Python startup and transport overhead. Verbose SSH
lines are interpreted locally and excluded from the returned diagnostic stderr.
On a failed shared connection, the fixed probe may compare an independent
connection. It never retries an arbitrary business command. Ordinary bash
results also include transport timings without enabling verbose SSH tracing.

Local runtime requirements: Python 3.11+, an `ssh` client, and the pinned
`vaws-diagnostics` package installed with the client. Nothing here needs GPU/NPU hardware; the remote host only needs
`bash`, `python3`, and (for `remote.apply_patch` unified diffs) `git`.

## Work inside an existing container

Add `container` to an ordinary SSH host endpoint. Read, edit, search, patch,
commands, jobs and artifacts then operate inside that existing Docker container:

```json
{"host":"lab-host","port":22,"container":"repro-case","cwd":"/work/vllm","command":"bash /work/start-case.sh"}
```

The CLI accepts the same coordinate as `--container repro-case`. No session,
registration, source copy or environment preparation is needed. `user` remains
the SSH user; `docker exec` retains the container's configured user and environment.
The existing container must be running with Python 3 and, for shell commands,
Bash. Jobs also require a writable endpoint `root` for their existing scratch
files; a non-root container user can select a writable code root. Docker access
comes from the SSH account. Missing requirements are errors;
remote-dev does not install packages, start/restart containers or provide managed
device/port allocation. A container already exposing SSH can still use its direct
SSH endpoint without the `container` field.

Your startup script is the command, unchanged. Use `runtime_env_file` only when
you want an environment script sourced before that command. Knowledge and managed
execution systems are independent optional consumers, never prerequisites.

Names and short IDs are resolved by one read-only Docker inspect per operation.
The returned `target.container` is the full Docker ID; `target.container_selector`
preserves the supplied name/short ID for display. Reusing the returned full ID
avoids further inspect calls. Every stage of an operation, job receipt, read ledger
and pooled connection uses this fixed ID. A new name-based operation can select a
replacement container; an old job reference always retains its old ID. There is
no name cache, implicit host fallback or replay after an uncertain submission.
Read-before-write concurrency checks remain optional and scoped to that ID.

For a container PTY use `remote.bash` with `tty=true`. The low-level interactive
bootstrap and SSH local-forward helpers explicitly reject container endpoints;
forward a reachable published port through an explicit host endpoint instead.
`ssh_base_cmd` is a host-only prefix; Python consumers constructing complete
commands use `ssh_command`. A consumer composing several operations can call
`core.container_endpoint.pin_container_endpoint` once and reuse its returned
Endpoint. Ordinary SSH operations perform no Docker lookup.

Workers and owned job logs retain the existing remote-dev scratch files. Stopping
a job stops only its identified process family, never the container or unrelated
processes. Arbitrary side effects in the user's command remain that command's
behavior.

The same public API runs from Windows, macOS and Linux clients. Attached SSH
streams and local forwards launch literal argv under an owned local process
group: a Windows Job Object is assigned before the child starts; POSIX uses an
independent session. Timeout and close include inherited children even after
SSH itself exits, and preserve available UTF-8 output. The internal launcher
accepts native cwd and an environment overlay; it does not interpret shell
syntax or translate Windows/WSL paths. The remote command still runs in Bash
on its Linux endpoint. Local SSH cleanup does not prove remote process quiet;
managed remote jobs use their supervisor's stop and quiet receipt. A POSIX
child that deliberately creates a separate session is outside the local group.

## Install and start the MCP server

From a git ref (no local checkout required):

```bash
uvx --from git+https://github.com/mindie-agent/remote-dev@main remote-dev server
```

From a clone:

```bash
uv pip install -e .
remote-dev server
```

`.mcp.json` (Claude Code / Cursor; Kimi Code uses `.kimi-code/mcp.json` —
see `examples/kimi-mcp.example.json`):

```json
{
  "mcpServers": {
    "remote-dev": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mindie-agent/remote-dev@main",
        "remote-dev",
        "server"
      ],
      "env": {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_DEFAULT_ROOT": "/",
        "REMOTE_DEV_DEFAULT_CWD": "/",
        "REMOTE_DEV_RESOLVERS": "/absolute/path/to/consumer/remote_dev_plugin.py:setup"
      }
    }
  }
}
```

If the package is already installed in the client environment, `command` can
be `remote-dev` with `args: ["server"]`. More client examples live in
`examples/`. See [CLIENT_COMPATIBILITY.md](CLIENT_COMPATIBILITY.md) for
per-client notes.

## Native-habit compatibility layer

One execution kernel serves every client; the differences live in a thin,
shared parameter layer (`remote_dev.mcp.schemas.normalize_arguments`, applied
by both the MCP dispatcher and the CLI `--input-json` path):

- Aliases: `path` for `file_path` (read/write/edit/multi_edit),
  `line_offset`/`n_lines` for `offset`/`limit` (read), `cmd`/`workdir` for
  `command`/`cwd` (bash), `session_id` for `job_id`, `-i`/`-A`/`-B`/`-C`/`-n`/`head_limit`
  (grep). A canonical key always wins; unknown keys pass through. Fields with
  aliases are enforced by the server, not the wire schema's `required` list,
  so alias-only calls pass provider-side validation.
- `remote.read` accepts a negative `offset` to read from the end of the file
  (`offset=-100` reads the last 100 lines). Binary files (NUL byte in the
  first 8192 bytes) fail with an actionable `binary_file` status — there is
  no remote image/media preview; use `remote.bash` or `remote.artifact_pull`.
- `remote.write` gains `append` (Kimi `mode=append`): extends the file
  atomically, creates it when missing, and is mutually exclusive with
  `overwrite`.
- `remote.grep` gains `case_insensitive`, `context_lines`/`before_context`/
  `after_context`, a `line_numbers` toggle, `offset` pagination, and
  `include_ignored` (rg `--no-ignore --hidden`; the grep fallback only lifts
  its hidden/.git excludes and says so). `output_mode` distinguishes `count`
  (matching lines per file, `rg -c`) from `count_matches` (total matches per
  file, `rg --count-matches`; the grep fallback counts `-o` matches per
  file). They differ whenever one line holds several matches.
- `remote.bash command=...` starts with writable stdin and waits up to
  `yield_time_ms` (default 10000) for output or completion. A live process or
  unread output returns `session_id`; completion reports `exit_code` and
  `quiet`. The wait excludes SSH connection and process preparation. Omitted
  `timeout_ms` (or zero) means no command deadline; an explicit value limits
  remote execution. `run_in_background`, `interactive`, and the separate
  monitor tool have been removed.
- `remote.job_stdin session_id=... chars=...` writes input; empty `chars`
  polls any session, including completed sessions with unread output. Per-stream
  byte cursors advance under a cross-process lock, preserving Unicode and
  preventing concurrent polls from replaying bytes. A partial input write
  reports `written_chars`; resend that exact remainder. EOF is deferred until
  all submitted characters are accepted.
- `tty=true` allocates a real 24x80 remote PTY with merged stdout/stderr.
  Ctrl-C signals the terminal foreground group. `eof=true` sends terminal
  Ctrl-D (canonical terminal semantics); pipe EOF closes stdin. In pipe mode,
  control bytes remain data. `remote.job_stop` stops the owned process family.
- `max_output_tokens` budgets approximately four UTF-8 bytes per token across
  both text and structured previews, shared by stdout/stderr. Each preview has
  a four-byte minimum so one Unicode character can progress. Status/refs
  metadata is separate. Unreturned bytes remain at the cursor; full decoded
  logs accumulate in local refs as pages are consumed.
- Developer calls reuse one binary SSH stdio connection on Windows and POSIX,
  independently of OpenSSH ControlMaster. Concurrent first calls to the same
  endpoint share startup; different endpoints do not hold a global startup lock.
  The 32-connection pool evicts idle LRU entries automatically and expires idle
  connections after five minutes (reaped within another minute). Busy connections
  are never evicted; capacity waits respect cancellation and the request deadline.
  Both MCP and remote RPC reserve two workers/eight slots for status, stop, tail
  and short stdin exchanges, alongside eight ordinary workers/32 slots. Long
  waits cannot consume that control capacity. A lost reply remains an unknown
  outcome and is never automatically replayed.
- Explicit `runtime_env_file` runs once per command in that command's Bash
  process, preserving functions, non-exported variables, PATH order and shell
  options. Missing scripts or failed initialization prevent the user command.
  Normal Bash startup (including BASH_ENV and SSH .bashrc behavior) is retained;
  arbitrary dynamic initialization is never cached.
- Managed callers using `processes.control(..., "prepare", spec=...)` can set
  `prepared_timeout_seconds` (default 120, between 1 and 86400 seconds) for their
  bounded queue/activation wait. Expiration cancels the unopened gate without
  running user code. Command `timeout_seconds` starts after activation; a lease
  heartbeat does not implicitly extend the remote prepared deadline.
- Tool arguments outside the published schema, native aliases and registered
  endpoint selectors are rejected before execution. MCP `remote.bash` uses
  `yield_time_ms` and continuation through `session_id`; `wait=True` is an SDK
  option and is rejected on the MCP surface.
- Developer MCP and CLI tools choose pooled connections and keepalives
  internally. Their endpoint arguments no longer include `ssh_mux`,
  `keepalive` or `--long-stream`; low-level Python transport callers retain
  `Endpoint` policy and `Endpoint.for_long_stream`. MCP failure text includes
  the status and recovery detail for clients that do not read structured results.
- Reads scan in bounded memory. `verify_content=false` (CLI
  `--no-verify-content`) stops after a positive-offset log window and omits the
  hash/read ledger; the default retains a full hash and exact line count for
  guarded editing. Search results stream to a bounded page. Artifact batches
  use one SSH stream with 1 MiB chunks, checksum verification and atomic file
  replacement. Default probes avoid importing application modules; request
  explicit `modules` (CLI `--module`) when needed.

## The endpoint-explicit contract

remote-dev resolves endpoints from explicit fields and nothing else:

| Field                | Default              | Meaning                                              |
|----------------------|----------------------|------------------------------------------------------|
| `host`               | required             | SSH host                                             |
| `port`               | required             | SSH port                                             |
| `user`               | `root`               | SSH user (`REMOTE_DEV_DEFAULT_USER`)                 |
| `root`               | `/`                  | Path-policy root (`REMOTE_DEV_DEFAULT_ROOT`)         |
| `cwd`                | same as `root`       | Default working dir (`REMOTE_DEV_DEFAULT_CWD`)       |
| `runtime_env`        | `true`               | Source `runtime_env_file` before commands            |
| `runtime_env_file`   | unset                | Remote profile script (`REMOTE_DEV_RUNTIME_ENV_FILE`)|
| `identity_file`      | unset                | SSH private key                                      |
| `connect_timeout_ms` | `10000`              | SSH connect timeout                                  |
| `alias`              | unset                | Name from the endpoint alias files                   |

The Python `Endpoint` API additionally accepts `ssh_mux` and `keepalive`
for low-level transport callers; these are not developer-tool arguments.

Resolution order in `remote_dev.core.endpoint.resolve_endpoint`:

1. `host` + `port` given: use them directly.
2. `alias` given: look it up in the alias files (`REMOTE_DEV_ENDPOINTS_FILE`,
   then `endpoints.json` and `endpoints.local.json` in the process cwd; both
   are git-ignored) and let explicit caller fields override the alias entry.
3. Otherwise ask each registered resolver plugin, in registration order.
4. Nothing claimed the payload: fail with `EndpointError` listing the known
   selector fields and registered resolvers. remote-dev never guesses a
   target from the working directory or from files it does not own.

`remote.job_status`, `remote.job_tail` and `remote.job_stop` can rebuild
their endpoint from the local job record and therefore resolve only when the
caller supplies a selector.

### Permission model

Direct endpoints default to **full remote-path permission** (`root=/`) with
the same path as the default cwd. Path containment, symlink checks and
cwd validation are still enforced, but against `/` unless a narrower `root`
is set. Pass `--root /srv/app --cwd /srv/app` when a task requires path
isolation. A consumer that wants a project default cwd sets
`REMOTE_DEV_DEFAULT_CWD`; remote-dev does not assume a workspace tree.
Hook guards (`remote_dev.hooks`) default to *allow* and only observe; they
are the place to add policy if you need it.

Read ledgers are optional optimistic-concurrency checks scoped by
`client_context_id`, then `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`,
`CODEX_RUN_ID`, `REMOTE_DEV_SESSION_ID`. The MCP server sets a process-local
`REMOTE_DEV_SESSION_ID` at startup. Each nonempty effective context id is
stored under one filesystem-safe directory `id-<sha256 of the raw id>`. The
no-context fallback remains `default` and does not collide with a caller who
explicitly supplies that word. A ledger is never required for an edit or
write of a file this context has not previously read. After a successful
read, later writes in that context are checked against the recorded SHA.
Ledgers written by earlier encodings are kept in place. A write or edit that
finds only those older files — including a v1 record occupying the current
`id-<sha256>` or `default` path — returns structured `read_required` until
the same context reads the file again. Path or `ledger_scope` matching the
current computed string is not enough: only a record written under the
current encoding (schema `remote-dev.read_ledger.v2`) authorizes a later
write. A legacy shared SHA is not authorization for a distinct context.

## Resolver plugin interface

Anything that is not `host`/`port`/`alias` - session registries, machine
inventories, worktree bindings, coordinator leases - is consumer knowledge.
The consumer registers a resolver with remote-dev; remote-dev never imports
the consumer.

```python
# consumer/remote_dev_plugin.py
from remote_dev.core.endpoint import EndpointError, register_resolver, resolver_setup

def by_lab(payload):
    lab = payload.get("lab")
    if not lab:
        return None                      # not ours: next resolver, please
    record = my_inventory.load(lab)      # consumer-owned lookup
    if record is None:
        raise EndpointError(f"unknown lab {lab!r}")
    return {
        "host": record.host, "port": record.ssh_port,
        "cwd": record.root,
        "runtime_env_file": "/etc/profile.d/toolchain.sh",
        "kind": "lab-endpoint",
        "source": {"lab": lab},
    }

@resolver_setup
def setup():
    register_resolver(by_lab, name="labs", fields=("lab",))
```

Contract:

- `resolve(payload) -> dict | Endpoint | None`. Return `None` to decline.
  A `dict` needs `host` and `port`; remote-dev builds the `Endpoint`, lets
  explicit caller fields (`user`, `root`, `cwd`, `runtime_env`,
  `runtime_env_file`, `identity_file`, `connect_timeout_ms`, `ssh_mux`,
  `keepalive`) override the resolver's values, sets `kind` to
  `resolver:<name>` unless provided, and records `source.resolver`.
- `fields` declares the payload keys the resolver claims. They are added to
  `selector_fields()` so `has_selector()` and the job tools treat them as
  "an endpoint was requested". Tool schemas keep
  `additionalProperties: true`, so consumer keys pass through MCP clients
  untouched.
- Resolvers are consulted even for an empty payload. A consumer that wants
  "zero-argument inside my worktree" behaviour implements it in its resolver
  (return `None` when there is no binding).
- Raise `EndpointError` when you claim a payload but cannot resolve it.
  Other exceptions are wrapped into `EndpointError` with the resolver name.

Loading into a process you do not control (the MCP server is spawned by the
client, CLI wrappers by a shell):

```
REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
REMOTE_DEV_RESOLVERS="consumer.remote_dev_plugin:setup,other.pkg:setup"
```

Entries are `module:callable` or `/path/file.py:callable`. A callable marked
with `@resolver_setup` is invoked once and registers what it likes; any other
callable is registered directly under its spec string. A broken entry fails
every resolution with the same message instead of degrading to "no
resolvers". `examples/resolver_plugin.py` is a complete runnable example.

Programmatic embedding works too: `from remote_dev.core.endpoint import
register_resolver` before invoking `remote_dev.mcp.tools.call_tool`.

The public result envelope is `remote_dev.result` (`schema_version`:
`remote-dev.result.v1`). Schema JSON ships as package data.

Selector keys on the CLI travel through `--selector KEY=VALUE`:

```bash
remote-dev bash --selector lab=gpu-1 --command 'nproc'
```

## What this repository does not hold

- No consumer state. `state/` (job records, read ledgers, logs, artifact
  manifests) is git-ignored and relocatable with `REMOTE_DEV_STATE_DIR`.
  The default is `<cwd>/state`.
- No endpoint data. `endpoints.json` / `endpoints.local.json` are
  git-ignored; ship aliases from your own tree via
  `REMOTE_DEV_ENDPOINTS_FILE`. `examples/endpoints.example.json` shows the
  shape.
- No knowledge of any particular consumer: no session registries, machine
  inventories, coordinators, task facades, or skill catalogues. Those live in
  the consumer and reach remote-dev only through resolvers and environment
  variables listed below.

## Configuration reference

| Variable                        | Purpose                                                   |
|---------------------------------|-----------------------------------------------------------|
| `REMOTE_DEV_DEFAULT_USER`       | Default `user` (`root`)                                   |
| `REMOTE_DEV_DEFAULT_ROOT`       | Default `root` (`/`)                                      |
| `REMOTE_DEV_DEFAULT_CWD`        | Default `cwd` (unset = same as `root`)                    |
| `REMOTE_DEV_RUNTIME_ENV_FILE`   | Default `runtime_env_file` (unset = no preamble)          |
| `REMOTE_DEV_RESOLVERS`          | Comma-separated resolver plugin specs                     |
| `REMOTE_DEV_ENDPOINTS_FILE`     | Alias file(s), `os.pathsep` separated, read first         |
| `REMOTE_DEV_STATE_DIR`          | Local state directory (default `<cwd>/state`)             |
| `REMOTE_DEV_SSH_MUX_DIR`        | OpenSSH ControlMaster dir (default `~/.ssh/remote-dev-mux`)|
| `REMOTE_DEV_SSH_MUX`            | Process-wide SSH multiplexing *default* on POSIX: unset or `1` uses the shared ControlMaster; `0` forces independent connections; other values error. Native Windows has no Client ControlMaster (Win32-OpenSSH); ordinary connections already use the independent triple and do not need this flag. `ssh_mux=True` / `REMOTE_DEV_SSH_MUX=1` on native Windows is a capability error. An endpoint's `ssh_mux` overrides the process default on POSIX. |
| `REMOTE_DEV_SESSION_ID`         | Read-ledger scope when no client id is given              |

For low-level Python SSH calls, `REMOTE_DEV_SSH_MUX` is the process-wide
default and is read without changing
global SSH configuration or the shared ControlMaster socket. Leave it unset or
set it to `1` to keep today's shared-mux path, including the per-identity
`ControlPath` suffix. Set it to exact `0` in a Python process that must not join
the shared master (`ControlMaster=no`, `ControlPath=none`, `ControlPersist=no`
on every SSH invocation from that process that does not set `ssh_mux`).
Accepted values are unset, `1`, and `0`; any other value is a configuration
error. Developer MCP/CLI operations use their own pooled independent
connections with keepalives, regardless of this low-level default.
Native Windows CLI and MCP pipes use UTF-8. Shell script uploads preserve LF
bytes, and nested artifact paths use POSIX separators on the Linux peer.

On native Windows the transport chooses independent connections by itself
because Win32-OpenSSH does not implement Client ControlMaster; do not set
`ssh_mux=true` or `REMOTE_DEV_SSH_MUX=1` there.

A single Python process may do both at once. Set `ssh_mux=false`
on the endpoints that must stay off the shared master, and leave the rest on
the default. This is not optional for long-lived connections such as
`ssh -N -L` tunnels: ControlMaster delegates `-N` forwards to the mux master
and the client exits rc=0 immediately, tearing the tunnel down. OpenSSH
first-option-wins semantics make a later `ControlMaster=no` override
ineffective, so the independent triple has to be chosen before the command is
built. `ControlMaster=no` alone is not enough — a client can still attach to
an existing `ControlPath`.

Low-level `keepalive=true` adds `ServerAliveInterval=30` and
`ServerAliveCountMax=10`. That is a mechanism flag, orthogonal to mux, and
conditional rather than always-on: a slow multi-hour stream otherwise dies
to an idle timeout somewhere in the path, but attaching ServerAlive to short
multiplexed commands would set TCP keepalive policy on the shared
ControlMaster (the master owns the TCP connection; first-option-wins).

Hour-scale streams and `ssh -N -L` tunnels use one named entry point:
`Endpoint.for_long_stream(host, port, ...)`. It always sets `ssh_mux=False`
and `keepalive=True` and cannot be half-configured (`ssh_mux=True` is
refused). `run_stream` /
`stream_ssh_command` refuse any endpoint that would still attach to a
ControlMaster — the silent failure this project recorded is rc=0 with the
tunnel gone, so a docstring is not a control.

Live streaming is the library function `remote_dev.core.ssh_transport.run_stream`.
It stays attached, forwards output as it arrives, and enforces a timeout on
both sides (remote `timeout --preserve-status` plus a local deadline-bounded
reader: `select` on POSIX, reader threads on native Windows). It returns
`RemoteCompleted` (`returncode`, not `exit_code`) and does not emit
`remote-dev.result.v1`. It is not `remote.job_*`.
Scripts travel through binary stdin instead of command-line arguments, so
large generated scripts work on native Windows. Upload and output draining
run concurrently under the same local timeout.

Detached background work uses one process implementation:
`remote_dev.processes.control(endpoint, job_id, action, **parameters)`.
Actions are `prepare`, `go`, `status`, `tail`, `stop`, `stdin`, `launch`, and
`exchange`. The Linux worker
is a child-subreaper with identity/marker checks, a start gate, descendant
drain, and timeout/stop. Ordinary `remote.bash` and
`remote.job_*` call this same boundary. Coordinator may call it directly
with an explicit host+port mapping; remote-dev does not load coordinator
state. The worker is Linux-only; the local transport client supports
macOS, Linux, and native Windows.

Python consumers needing a completed result call `remote_bash(..., wait=True)`.
This waits and drains full log refs through the same supervisor, with stdin
closed unless a PTY is requested. Coordinator retains its separate
`prepare`/authorized `go` gate for managed execution.
Synchronous capture waits past early output until completion or the existing
yield deadline, avoiding an extra round trip just to observe a short command's
exit. Preview budgets, durable output cursors and cancellation remain the same.

Two more transport primitives close the remaining SSH-option gaps. They are
library APIs, not MCP tools, and they do not accept extra `-o` strings.

- `open_local_forward(endpoint, remote_port)` opens `ssh -N -L` on the
  `for_long_stream` shape (`ControlMaster=no`, `ControlPath=none`,
  `ControlPersist=no`, keepalives, `ExitOnForwardFailure=yes`). It refuses
  a multiplexed endpoint the same way `run_stream` does. The handle exposes
  `local_port`, `wait_ready(timeout_s)`, and `close()` (process-group kill;
  a forward that dies is never reported as rc=0 — that silent success is
  the recorded mux-absorbed `-N` failure).
- `run_interactive(endpoint, remote_command)` is a one-off TTY-inherited
  bootstrap (`BatchMode=no`, password/keyboard-interactive only,
  `PubkeyAuthentication=no`). Combining it with multiplexing is impossible:
  a password prompt through a ControlMaster is meaningless and hangs.
  `interactive_ssh_command` returns the argv for wrappers such as
  `SSH_ASKPASS`. This is first-contact bootstrap, not a general PTY
  facility.

Stdin bytes into a remote command are already `run_bytes`. Detached
background work is `remote.bash --run-in-background` / `remote.job_*`.
Directory trees move with `remote.artifact_push` / `artifact_pull`.

## MCP server and clients

`remote-dev server` speaks JSON-RPC over stdio with `Content-Length` framing
(newline-delimited JSON is accepted as a test fallback). Discovery advertises
portable underscore names (`remote_read`, ...); dotted canonical names remain
accepted on `tools/call`. Resources:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/{status,stdout,stderr}`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

Every tool returns `{"text": ..., "result": ...}` where `result` follows
`remote_dev.result` / the packaged `result.schema.json` (`remote-dev.result.v1`):
`outcome` in `success | needs_input | blocked | failed | timeout | cancelled`,
a compact `preview`, and `refs` to full logs on disk. Model-visible text is
capped; full output is reachable through refs and MCP resources.

## Validation

```bash
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

Live checks need a reachable SSH host:

```bash
remote-dev validate --host <host> --port <port> --root /srv/app --cwd /srv/app
remote-dev validate --alias lab --skip-local
```

The validator compile-checks the installed package, reports MCP/CLI burden
metrics, and (with an endpoint) exercises read/edit/write/bash/search, patches,
artifacts, background jobs, MCP resources and parallel scratch workers, then
cleans up after itself.

## Layout

```
remote_dev/  installable package (core, processes, mcp, hooks, tools, schemas)
tests/       unittest suite collected by pytest (mocked transports, no SSH)
examples/    client configs, alias file shape, resolver plugin
```

See [DESIGN.md](DESIGN.md) for the architecture and
[VALIDATION.md](VALIDATION.md) for the evidence record.

## License

MIT. See [LICENSE](LICENSE).

## Runtime feedback and connection checks

MCP responses report the process-start package identity separately from the
currently installed distribution. A `restart_required` status means the native
client must restart that MCP server; changing installed files does not reload it.
The source commit is unknown when distribution metadata does not provide it.

`remote-dev probe --host HOST --port PORT --diagnose-connection` runs a fixed,
read-only SSH probe. After a failed multiplexed probe it compares an independent
connection, and may suggest `ssh_mux=false`. It never replays the business command
or changes global SSH configuration. Foreground bash results expose the actual
connection mode and timeout. A timeout or exit 255 leaves remote outcome unknown.

`remote_dev.diagnostics.open_http` selects `direct` or `environment` proxy use
per call. Its companion diagnostics strip URL credentials and query parameters,
and distinguish HTTP status from DNS, timeout and connection errors.


## Diagnostics

Version 0.9.0 requires Python 3.11 or newer for the local client, including its
shared diagnostics dependency. The self-contained remote helpers retain their
existing interpreter requirements; logging does not install or upgrade anything
on the remote host. Coordinator clients using `remote_dev.observability` require
this release or newer.

Tool replies include `diagnostics` with an operation ID, trace ID, actual UTC
start/end, monotonic duration, bounded phase summaries and a local `record_ref`.
The operation ID exists before work starts. A run admission's duration describes
that call; its asynchronous execution and later wait have separate lifetimes.
Native MCP metadata propagates correlation automatically. Diagnostic IDs never
grant task or endpoint authority and are excluded from launch-content identity.

`VAWS_LOG_LEVEL=INFO` is the default. `DEBUG` adds RPC, lock and transport phase
detail; `WARN`/`WARNING` retains warnings and errors. `VAWS_DIAGNOSTICS_ROOT`
selects a private local log root. The shared diagnostics package rotates bounded
per-process JSONL files and records package versions. Commands, file contents,
environment values and credentials are not logging arguments. Business output
remains in existing job logs and tool output, separate from implementation logs.
MCP reserves its protocol descriptors: Python and native stdout writes go to
stderr, and child processes cannot inherit protocol stdin accidentally.

`error_details` preserves category, retryability and submission certainty.
`not_sent` / `not_executed` indicate a known pre-execution boundary;
`uncertain` requires observing the existing job/execution reference, without
resubmission. `acknowledged` means the remote request was accepted, not that its
business succeeded. Only an explicit `caller` category denotes known caller
input error; a generic validation or permission exception is not that judgment.
Cancellation/observation timeout does not fabricate quiet or resource release.
Logging/export disk errors do not change business results or ownership; failures
to persist authoritative execution state still fail.

For a local issue attachment, the shared `vaws-diagnostics bundle` command makes
an offline, bounded public projection from diagnostic events. Use the returned
operation ID. It excludes raw commands, paths, endpoints and business logs, and
reports missing or truncated evidence. It never replays work or uploads an issue
by itself. Monotonic clocks are process-local: do not subtract remote/local UTC
stamps or sum overlapping RPC, command and parallel-role phase durations.
