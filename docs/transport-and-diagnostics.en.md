# Transport, Containers & Diagnostics Reference

[中文](./transport-and-diagnostics.md) · **English** · [Back to Documentation Index](./README.en.md)

This reference covers existing-container execution, the resolver plugin contract, path permissions and read ledgers, low-level SSH transport and process supervision, and structured diagnostics in `remote-dev`.

---

## 1. Working Inside an Existing Docker Container

Add `container` to an ordinary SSH host endpoint (or pass `--container <name-or-id>` on the CLI) so that file reads, edits, searches, patches, commands, background jobs, and artifact transfers run directly inside that existing container:

```json
{"host": "lab-host", "port": 22, "container": "repro-case", "cwd": "/work/vllm", "command": "bash /work/start-case.sh"}
```

- **Zero-Install Prerequisites**: The target container must be running with `python3` and `bash` (plus `git` for unified diff patches). `user` remains the SSH host user while `docker exec` preserves the container's configured user and environment. `remote-dev` never installs packages, starts/restarts containers, or falls back implicitly to the host.
- **Full Container ID Pinning**: Each new name- or short-ID-based operation performs one read-only `docker inspect` to resolve the full Docker ID (`target.container`, while `target.container_selector` preserves the caller's selector). Every stage of the operation, job receipt, read ledger, and pooled RPC connection uses that fixed full ID. There is no mutable name cache or automatic replay after an uncertain submission.
- **Container PTYs & Boundaries**: Use `remote.bash` with `tty=true` for a container PTY. Low-level interactive bootstrap (`run_interactive`) and SSH local port forwarding (`open_local_forward`) explicitly reject container endpoints; forward reachable published ports through an explicit host endpoint instead.

---

## 2. Resolver Plugin Interface

Anything beyond `host` / `port` / `alias`—such as lab inventories, session registries, worktree bindings, or coordinator leases—is resolved through consumer-registered plugins:

```python
from remote_dev.core.endpoint import EndpointError, register_resolver, resolver_setup

def by_lab(payload):
    lab = payload.get("lab")
    if not lab:
        return None
    record = my_inventory.load(lab)
    if record is None:
        raise EndpointError(f"unknown lab {lab!r}")
    return {
        "host": record.host,
        "port": record.ssh_port,
        "cwd": record.root,
        "runtime_env_file": "/etc/profile.d/toolchain.sh",
        "kind": "lab-endpoint",
        "source": {"lab": lab},
    }

@resolver_setup
def setup():
    register_resolver(by_lab, name="labs", fields=("lab",))
```

Load resolver plugins into the MCP server or CLI via `REMOTE_DEV_RESOLVERS`:

```bash
export REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
remote-dev bash --selector lab=gpu-1 --command "nproc"
```

---

## 3. Permission Model & Read Ledgers

- **Path Policy**: Direct endpoints default to `root=/` and `cwd=/` while still enforcing path containment and symlink escape checks. Pass `--root /srv/app --cwd /srv/app` when a task requires strict directory containment.
- **Optimistic-Concurrency Read Ledgers**: Scoped by `client_context_id` (falling back to `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`, `CODEX_RUN_ID`, `REMOTE_DEV_SESSION_ID`) under `id-<sha256>` directories using schema `remote-dev.read_ledger.v2`. Files not yet read in the current context can be written directly; once read, subsequent edits or writes verify the recorded SHA-256 before modifying the remote file.

---

## 4. SSH Transport, Streaming Primitives & Process Supervision

- **Developer Tool RPC Pool**: MCP and CLI operations reuse a 32-connection binary SSH stdio RPC pool (evicting idle entries after 5 minutes) with 8 ordinary workers (32 slots) and 2 reserved control workers (8 slots) for `status`, `stop`, `tail`, and `stdin`.
- **Low-Level Python Transport Primitives**:
  - `Endpoint.for_long_stream(host, port, ...)`: Enforces `ssh_mux=False` and `keepalive=True` (`ServerAliveInterval=30`, `ServerAliveCountMax=10`) for multi-hour streams and `ssh -N -L` tunnels so OpenSSH `ControlMaster` cannot absorb `-N` forwards and exit `rc=0` prematurely.
  - `remote_dev.core.ssh_transport.run_stream`: Stays attached and streams output live, sending scripts over binary stdin and enforcing optional timeouts on both local and remote sides.
  - `open_local_forward(endpoint, remote_port)`: Opens an `ssh -N -L` tunnel on the `for_long_stream` configuration and exposes `local_port`, `wait_ready(timeout_s)`, and `close()`.
  - `run_interactive(endpoint, remote_command)`: One-off TTY-inherited bootstrap (`BatchMode=no`), mutually exclusive with SSH multiplexing.
- **Managed Process Supervision (`remote_dev.processes.control`)**: Supports `prepare`, `go`, `status`, `tail`, `stop`, `stdin`, `launch`, and `exchange`. The Linux worker acts as a child subreaper with identity/marker verification, start gates, descendant draining, and process-family termination.

---

## 5. Connection Probing & Structured Diagnostics

- **Connection Diagnostics**: `remote-dev probe --host <host> --port <port> --diagnose-connection` runs a fixed read-only probe reporting local preparation, SSH duration, TCP/auth milestones, remote probe execution, and stream drain timing. If a multiplexed probe fails, it compares an independent connection without ever replaying a business command.
- **Structured Diagnostic Envelopes**: Tool responses include `diagnostics` (`operation_id`, `trace_id`, UTC start/end, monotonic duration, bounded phase summaries, and local `record_ref`) and `error_details` (preserving category, retryability, and submission certainty: `not_sent` / `not_executed`, `uncertain`, or `acknowledged`).
- **Redaction & Offline Bundles**: `MINDIE_LOG_LEVEL=INFO` is the default and `MINDIE_DIAGNOSTICS_ROOT` configures the local log directory. Raw commands, file contents, environment values, and credentials are never passed as diagnostic logging arguments. Use `mindie-diagnostics bundle` with an `operation_id` to produce a redacted offline issue attachment.
