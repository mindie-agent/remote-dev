# Containers, SSH Transport & Diagnostics Reference

[中文](./transport-and-diagnostics.md) · **English** · [Back to Documentation Index](./README.en.md)

This guide covers five practical topics in `remote-dev`: **working inside running Docker containers, writing custom endpoint resolver plugins, preventing concurrent file overwrites, understanding the SSH connection pool, and diagnosing slow or broken connections.**

---

## 1. Working Inside an Existing Docker Container

On shared GPU/NPU servers, your development environment often lives inside a running Docker container that does not run its own `sshd` or expose a separate SSH port.

With `remote-dev`, simply add `container` to any SSH host endpoint (or pass `--container <name-or-id>` on the CLI). All file reads, edits, searches, patches, commands, background jobs, and file transfers will execute directly inside that container:

```json
{"host": "lab-host", "port": 22, "container": "repro-case", "cwd": "/work/vllm", "command": "bash /work/start-case.sh"}
```

- **Zero Container Setup**: The container just needs to be running with `python3` and `bash` installed (plus `git` if you apply unified diff patches). `remote-dev` never installs packages inside your container, never starts/stops containers, and never silently falls back to running on the host.
- **Full Container ID Pinning**: When you specify a container name (like `repro-case`) or short ID, `remote-dev` runs a single read-only `docker inspect` on the host to resolve the **full 64-character container ID** (`target.container`, keeping your original name in `target.container_selector`). All job records, read ledgers, and pooled connections lock onto that full ID — so even if a container is recreated under the same name, operations never hit the wrong container.
- **Container PTYs & Host-Only Operations**: Need a terminal inside the container? Pass `tty=true` to `remote.bash`. Low-level interactive password bootstrap (`run_interactive`) and SSH local port forwarding (`open_local_forward`) are host-level operations and reject container endpoints (forward published ports through the host endpoint instead).

---

## 2. Writing Custom Endpoint Resolver Plugins

If your team maintains a lab machine inventory or cluster lease table, you can register a custom Python resolver plugin so callers can pass semantic selectors like `--selector lab=gpu-1` instead of raw IP addresses:

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

Load your plugin into the MCP server or CLI using `REMOTE_DEV_RESOLVERS`:

```bash
export REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
remote-dev bash --selector lab=gpu-1 --command "nproc"
```

---

## 3. Directory Boundaries & Read-Before-Write Protection

- **Directory Boundaries (`root` & `cwd`)**: Direct endpoints default to `root=/` and `cwd=/` while still blocking symlink escapes. When you want to restrict an agent to a specific workspace directory, pass `--root /srv/app --cwd /srv/app` — any read or write outside `/srv/app` will be rejected.
- **Read-Before-Write Conflict Ledger**:
  - Ledgers are isolated per client session (`client_context_id`, `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`, `CODEX_RUN_ID`, or `REMOTE_DEV_SESSION_ID`) under `state/id-<sha256>`.
  - Brand-new or unread files can be written directly. Once a file has been read in the current session, any subsequent `edit` or `write` checks that the remote file's SHA-256 still matches what was read, preventing accidental overwrites if another process modified the file in the meantime.

---

## 4. SSH Connection Pool, Streaming Tunnels & Process Supervision

- **Pooled Binary SSH RPC with Reserved Control Lanes**:
  - Maintains up to 32 persistent binary SSH stdio RPC connections (closing idle connections after 5 minutes).
  - Splits capacity into **8 regular workers (32 slots)** and **2 reserved control workers (8 slots)** so `status`, `tail`, `stop`, and `stdin` calls never get stuck behind heavy commands.
- **Low-Level Python Streaming & Tunneling Helpers**:
  - `Endpoint.for_long_stream(host, port, ...)`: Built for multi-hour log streams and `ssh -N -L` port forwards. It disables `ssh_mux` and enables SSH keepalives (`ServerAliveInterval=30`, `ServerAliveCountMax=10`) so OpenSSH `ControlMaster` cannot swallow `-N` tunnels and exit early.
  - `remote_dev.core.ssh_transport.run_stream`: Streams remote output live while enforcing optional local and remote timeouts.
  - `open_local_forward(endpoint, remote_port)`: Opens an `ssh -N -L` local port forward and exposes `.local_port`, `.wait_ready(timeout_s)`, and `.close()`.
- **Clean Process-Tree Supervision (`remote_dev.processes.control`)**:
  - Supports `prepare`, `go`, `status`, `tail`, `stop`, `stdin`, `launch`, and `exchange`.
  - On Linux, the remote worker runs as a child subreaper so `stop` reliably terminates all spawned child and grandchild processes.

---

## 5. Connection Probing & Structured Diagnostics

- **Pinpoint Slow or Broken SSH Connections**:
  ```bash
  remote-dev probe --host <host> --port <port> --diagnose-connection
  ```
  Runs a safe, read-only probe that breaks down timing across **local setup, SSH process launch, TCP/auth handshake milestones, remote execution, and stream cleanup**. If a multiplexed (`ControlMaster`) connection fails, it automatically compares a fresh non-multiplexed connection and suggests `ssh_mux=false` — without ever re-running a business command.
- **Actionable Error Details & Certainty States**:
  Every tool response includes `diagnostics` (`operation_id`, `trace_id`, start/end timestamps, phase timings, and local `record_ref`) plus `error_details` that clearly state whether a failed request was **never executed (`not_sent` / `not_executed`)**, **confirmed (`acknowledged`)**, or **interrupted mid-flight (`uncertain`)**.
- **Privacy by Default**:
  Logs default to `MINDIE_LOG_LEVEL=INFO` under `MINDIE_DIAGNOSTICS_ROOT`. Raw shell commands, file contents, environment variable values, and credentials are never written into diagnostic log fields. You can safely generate a redacted offline diagnostic bundle using `mindie-diagnostics bundle`.
