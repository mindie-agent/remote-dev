<p align="center">
  <img src="assets/brand/remote-dev-logo.png" alt="remote-dev logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent Remote Dev (<code>remote-dev</code>)</h1>

<p align="center">
  <strong>SSH-backed remote development substrate for coding agents — making remote Linux hosts and containers feel like the local working tree</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <strong>English</strong> · <a href="./docs/README.en.md">Documentation</a> · <a href="https://github.com/mindie-agent/mindie-agent">MindIE Agent Main Repo</a>
</p>

---

## Overview

`remote-dev` is the remote development substrate of [MindIE Agent](https://github.com/mindie-agent/mindie-agent) (distribution `remote-dev`, import package `remote_dev`, console entry `remote-dev`). It gives coding agents (Claude Code, Codex, Kimi Code, Cursor, Grok Build) remote twins of their native local tools with identical semantics plus explicit endpoint fields (`host` / `port` / `container` / `alias`).

- **19 Symmetric Remote Tools**: Served by a single stdio MCP server (`remote-dev server`) and mirrored one-to-one by CLI subcommands (`python -m remote_dev` or `remote-dev`).
- **Seamless Host & Existing Container Execution**: Add `container` to any SSH host endpoint to read, edit, search, patch, run commands, manage background jobs, and transfer artifacts directly inside a running Docker container without installing a container SSH daemon.
- **Pooled Binary SSH Stdio RPC**: Reuses persistent SSH RPC connections across macOS, Linux, and native Windows, with dedicated control workers (`status` / `tail` / `stop` / `stdin`) so long-running commands never block control operations.
- **Explicit Endpoint Contract & Pluggable Resolvers**: Never guesses targets from the local directory; supports consumer-owned selector plugins via `REMOTE_DEV_RESOLVERS`.

---

## Local-to-Remote Tool Mapping

| Local Tool | Remote MCP Tool (`remote.*` / `remote_*`) | CLI Subcommand | Description |
| --- | --- | --- | --- |
| `Read` | `remote.read` (`remote_read`) | `remote-dev read` | Paginated file read (supports negative `offset` tail reads and optional read-ledger checks) |
| `Write` | `remote.write` (`remote_write`) | `remote-dev write` | Atomic file write or append (`append=true`) |
| `Edit` | `remote.edit` (`remote_edit`) | `remote-dev edit` | Single-occurrence exact string replacement |
| `MultiEdit` | `remote.multi_edit` (`remote_multi_edit`) | `remote-dev multi-edit` | Atomic multi-hunk file replacement |
| `Bash` | `remote.bash` (`remote_bash`) | `remote-dev bash` | Execute remote shell commands with `yield_time_ms` session continuation and `tty=true` PTY |
| `Glob` | `remote.glob` (`remote_glob`) | `remote-dev glob` | Remote file path globbing |
| `Grep` | `remote.grep` (`remote_grep`) | `remote-dev grep` | Remote content search (`rg` with `grep` fallback, context lines, line/match counts) |
| `LS` | `remote.ls` (`remote_ls`) | `remote-dev ls` | List remote directory entries |
| `apply_patch` | `remote.apply_patch` (`remote_apply_patch`) | `remote-dev apply-patch` | Atomically apply Codex patches or unified diffs |
| `write_stdin` | `remote.job_stdin` (`remote_job_stdin`) | `remote-dev job-stdin` | Write stdin or poll incremental output on an active or unread session |
| *(Background Jobs)* | `remote.job_status` / `job_tail` / `job_stop` | `remote-dev job-status` / `job-tail` / `job-stop` | Inspect status, tail incremental output, or stop an owned process family |
| *(Artifacts)* | `remote.artifact_manifest` / `artifact_pull` / `artifact_push` | `remote-dev artifact-manifest` / `artifact-pull` / `artifact-push` | 1 MiB chunked binary streaming with SHA-256 verification and atomic replacement |
| *(Endpoint Facts)* | `remote.probe` / `remote.context_snapshot` | `remote-dev probe` / `context-snapshot` | Inspect remote OS, Python, working directory, and connection timing milestones |

---

## Quick Start

### Requirements

- **Local Client**: Python `>= 3.11`, an `ssh` client (native Windows uses `usr/bin/ssh.exe` from Git for Windows), and the pinned `mindie-diagnostics` package. No local GPU/NPU hardware is required.
- **Remote Host**: Linux reachable over SSH with `bash`, `python3`, and (for unified diff patches) `git`.

### 1. Install and Start the MCP Server

From a Git reference (no local checkout required):

```bash
uvx --from git+https://github.com/mindie-agent/remote-dev@main remote-dev server
```

From a local clone:

```bash
uv pip install -e .
remote-dev server
```

### 2. Client MCP Configuration

Example `.mcp.json` for Claude Code / Cursor (Kimi Code uses `.kimi-code/mcp.json`, Codex uses `.codex/config.toml`; see [`examples/`](./examples/) and [CLIENT_COMPATIBILITY.md](./CLIENT_COMPATIBILITY.md)):

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
        "REMOTE_DEV_DEFAULT_CWD": "/"
      }
    }
  }
}
```

### 3. CLI & Container Examples

```bash
# Run a read-only connection probe with phase timing diagnostics
remote-dev probe --host lab-host --port 22 --diagnose-connection

# Run a command directly inside an existing Docker container on the remote host
remote-dev bash --host lab-host --port 22 --container repro-case --cwd /work/vllm --command "pytest -q"

# Run the unit test suite and local validator
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

---

## Explicit Endpoint Contract & Configuration

`remote-dev` resolves endpoints strictly from explicit fields and never guesses targets from the working directory:

1. Explicit `host` + `port` (optionally with `container`, `user`, `root`, `cwd`, `runtime_env_file`, `identity_file`).
2. `alias` looked up in `REMOTE_DEV_ENDPOINTS_FILE` or `endpoints.json` / `endpoints.local.json`.
3. Consumer-registered resolver plugins loaded via `REMOTE_DEV_RESOLVERS` (e.g., `--selector lab=gpu-1`).

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `REMOTE_DEV_DEFAULT_USER` | `root` | Default SSH user |
| `REMOTE_DEV_DEFAULT_ROOT` | `/` | Default path-policy root |
| `REMOTE_DEV_DEFAULT_CWD` | same as `root` | Default remote working directory |
| `REMOTE_DEV_RUNTIME_ENV_FILE` | unset | Remote profile script sourced before commands |
| `REMOTE_DEV_RESOLVERS` | unset | Comma-separated resolver plugin specs (`module:callable` or `/path/file.py:callable`) |
| `REMOTE_DEV_ENDPOINTS_FILE` | unset | Alias file path(s), separated by `os.pathsep` |
| `REMOTE_DEV_STATE_DIR` | `<cwd>/state` | Local state directory for job records, read ledgers, and manifests |

---

## Documentation

| Topic | 中文 | English |
| --- | --- | --- |
| Documentation Index | [docs/README.md](./docs/README.md) | [docs/README.en.md](./docs/README.en.md) |
| Architecture & Substrate Design | [docs/design.zh.md](./docs/design.zh.md) | [DESIGN.md](./DESIGN.md) |
| Client Compatibility & Aliases | [docs/client-compatibility.zh.md](./docs/client-compatibility.zh.md) | [CLIENT_COMPATIBILITY.md](./CLIENT_COMPATIBILITY.md) |
| Transport, Containers & Diagnostics | [docs/transport-and-diagnostics.md](./docs/transport-and-diagnostics.md) | [docs/transport-and-diagnostics.en.md](./docs/transport-and-diagnostics.en.md) |

---

## Repository Layout

```text
remote-dev/
├── remote_dev/              # Installable Python package (core, processes, mcp, hooks, tools, schemas)
├── examples/                # Client MCP configs, alias file template, and resolver plugin example
├── docs/                    # Bilingual architecture, client compatibility, and transport references
├── tests/                   # Unit test suite collected by pytest (mocked transports, no SSH required)
├── DESIGN.md                # Substrate design and dependency boundaries (English)
└── CLIENT_COMPATIBILITY.md  # Per-client MCP configuration and parameter normalization (English)
```

---

## License

Released under the [MIT License](./LICENSE).
