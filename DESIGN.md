# Remote Developer Substrate Design

[中文](./docs/design.zh.md) · **English** · [Documentation Index](./docs/README.en.md)

`remote-dev` makes remote development feel like local development for Codex,
Claude Code, Cursor, Kimi Code, Grok Build, and other MCP-capable coding agents.
Local work keeps using the client's native `Read` / `Edit` / `Write` / `Bash` /
`Glob` / `Grep` / `apply_patch` tools; remote work uses the matching `remote.*`
tool and only adds explicit endpoint fields.

## Three-Layer Architecture

### Layer A: Remote-Native Developer Tool Surface

- `remote.read` (`RemoteRead`)
- `remote.write` (`RemoteWrite`)
- `remote.edit` (`RemoteEdit`)
- `remote.multi_edit` (`RemoteMultiEdit`)
- `remote.bash` (`RemoteBash`)
- `remote.glob` (`RemoteGlob`)
- `remote.grep` (`RemoteGrep`)
- `remote.ls` (`RemoteLS`)
- `remote.apply_patch` (`RemoteApplyPatch`)

Plus background job control (`remote.job_status` / `job_tail` / `job_stop` /
`job_stdin`), artifact sync (`remote.artifact_manifest` / `artifact_pull` /
`artifact_push`), and endpoint facts (`remote.probe`, `remote.context_snapshot`).

### Layer B: Shared Substrate

- **Endpoint identity and explicit resolution** (`remote_dev.core.endpoint`).
- **Existing Docker container coordinates** (`remote_dev.core.container_endpoint`):
  resolve a container name or short ID to a running full container ID through the
  host's existing RPC, then freeze that full ID before operation records or
  concurrency checks; full IDs require no lookup.
- **Resolver plugin interface** for consumer-owned selectors (`remote_dev.core.endpoint`).
- **Binary SSH stdio RPC pooling** on Windows and POSIX (`remote_dev.core.rpc_transport`),
  with request IDs, bounded code caches, scoped cancellation, concurrent requests,
  and no replay after unknown outcomes.
- **Low-level SSH options, attached streams, port forwards, and first-contact
  bootstrap** (`remote_dev.core.ssh_transport`), retaining explicit ControlMaster policy.
- **Full-permission default root with optional explicit root/cwd path policy**
  (`remote_dev.core.path_policy`, `remote_dev.core.permissions`).
- **Optional read-ledger optimistic concurrency checks** (`remote_dev.core.read_ledger`,
  `remote_dev.core.state_store`).
- **Compact previews plus full local refs** (`remote_dev.core.preview`, `remote_dev.result`).
- **Generic remote process supervision** (`remote_dev.processes.control`) and
  execute/yield/poll sessions with durable byte cursors (`remote_dev.core.job_ops`),
  supporting pipe stdin, real PTYs, and synchronous library waits.
- **Artifact manifests and bounded binary batch streams** (`remote_dev.core.artifact_ops`,
  `remote_dev.core.artifact_transport`) with atomic replacement after SHA-256 verification.
- **Local record locks and shared remote mutation locks** across file tools,
  patches, and artifact commits while keeping concurrent reads independent.
- **Hook guards** (`remote_dev.hooks`).
- **Concurrent MCP server and resources** with eight ordinary workers (32 slots)
  and two reserved control workers (8 slots) (`remote_dev.mcp`).

`ssh_command` is the shared complete-command constructor. Container operations
invoke `docker exec -i <full-id>` for the existing Python RPC worker, binary
artifact worker, Bash scripts, and streams. No container daemon, SSH server,
separate supervisor, dependency installer, or managed resource authority is added.
Host-only prefixes, interactive bootstrap, and port forwarding reject container
endpoints before launching a process. Container IDs partition local state,
mutation locks, and connection pools; retained job records never re-resolve a
mutable container name.

### Layer C: Consumer Layer

Workflow skills, session managers, and coordinators live in their own consumer
repositories and interact with `remote-dev` strictly through tool calls,
environment variables, and registered resolver plugins. `remote-dev` never
imports Layer C.

## Dependency Direction

The substrate depends only on the Python standard library, `mindie-diagnostics`,
and an `ssh` client binary. Consumers depend on the substrate:

- **Explicit endpoint resolution**: accepts `host` / `port` / `user` / `root` /
  `cwd` / `container` and `alias`. Every other selector is resolved by a plugin
  registered via `remote_dev.core.endpoint.register_resolver` or `REMOTE_DEV_RESOLVERS`.
- **Generic process control**: `remote_dev.processes.control` serves both ordinary
  background jobs and coordinator-managed executions without requiring a
  coordinator installation on the remote host.
- **Environment-driven configuration**: `REMOTE_DEV_RUNTIME_ENV_FILE`,
  `REMOTE_DEV_SSH_MUX_DIR`, `REMOTE_DEV_STATE_DIR`, and `REMOTE_DEV_ENDPOINTS_FILE`.

## MCP Transport & Resources

The MCP server supports standard stdio `Content-Length` JSON-RPC framing (with
newline-delimited JSON-RPC retained as a local test fallback).

Exposed MCP resources:

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/status`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stdout`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stderr`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

## Validation

```bash
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

For live endpoint verification against a reachable SSH host:

```bash
remote-dev validate --host <host> --port <port> --root /srv/app --cwd /srv/app
```

See [Transport, Containers & Diagnostics](./docs/transport-and-diagnostics.en.md) for detailed transport and observability specifications.
