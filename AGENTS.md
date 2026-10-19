# Remote Dev (`remote-dev`) — Agent-Native Architecture & Invariants

`remote-dev` provides the shared remote workspace substrate (18 canonical `remote.*` tools + 40 native 6-Harness aliases over SSH JSON-RPC, durable background job supervision, and hash-verified binary artifact streaming) for MindIE Agent.

## 1. Module Navigation & Responsibilities

| Module | Role | Key Entry Points |
| :--- | :--- | :--- |
| `remote_dev/mcp/tools.py` & `schemas.py` | MCP tool dispatcher, `remote://` resource reader, and 6-Harness alias normalizer (18 canonical tools + 40 native tool aliases across Claude Code, Codex, Kimi, Cursor, Antigravity, DSH) | `call_tool()`, `list_tools()`, `normalize_arguments()` |
| `remote_dev/mcp/server.py` | Stdio JSON-RPC 2.0 MCP server (`Content-Length` framed & newline-delimited) with dedicated short-control thread pool | `main()`, `Dispatcher`, `handle()` |
| `remote_dev/cli.py` | Unified CLI entry point (`remote-dev <tool>`) sharing the MCP schema and dispatcher | `main()`, `run_tool()` |
| `remote_dev/core/file_ops.py` & `remote_file_worker.py` | Remote `read`/`ls`/`write`/`edit`/`multi_edit` with read-ledger SHA-256 optimistic concurrency; `remote_file_worker.py` is a standalone `.py` file loaded via `lru_cache` | `remote_read()`, `remote_write()`, `remote_edit()`, `remote_multi_edit()`, `remote_ls()` |
| `remote_dev/core/rpc_transport.py` & `processes/rpc_worker.py` | Pooled binary-pipe SSH JSON-RPC transport (`_POOL_LIMIT=32`) and remote `python3 -I` worker | `request()`, `RpcConnection`, `close_connections()` |
| `remote_dev/core/job_ops.py` & `processes/worker.py` | Durable remote background job supervisor (`prepare`/`go`/`launch`/`exchange`/`status`/`tail`/`stop`) and interactive `stdin` session cursor management | `start_remote_job()`, `remote_job_status()`, `remote_job_stdin()`, `remote_job_stop()`, `remote_job_tail()` |
| `remote_dev/core/artifact_ops.py`, `artifact_transport.py` & `processes/artifact_worker.py` | SHA-256 manifest generation and chunked binary `pull`/`push` over an independent SSH channel (`ssh_mux=False, keepalive=True`) | `remote_artifact_manifest()`, `remote_artifact_pull()`, `remote_artifact_push()`, `ArtifactStream` |
| `remote_dev/core/patch_ops.py` & `search_ops.py` | Atomic multi-file Codex `apply_patch` / unified-diff transactions with rollback (`patch_ops.py`), and `rg` / `grep -E` workspace search (`search_ops.py`) | `remote_apply_patch()`, `remote_glob()`, `remote_grep()` |
| `remote_dev/core/ssh_transport.py` & `local_process.py` | SSH argv construction (all options before `--`), ControlMaster policy, attached streams (`run_stream`), `-N -L` port forwards (`open_local_forward`), and `OwnedProcess` group/Job-Object lifecycle | `ssh_command()`, `run_script()`, `run_stream()`, `open_local_forward()`, `OwnedProcess` |
| `remote_dev/diagnostics.py` | Read-only two-stage SSH connection diagnostic (`diagnose_ssh`: Stage 1 mux `60%` vs Stage 2 independent `>= 40%`) | `diagnose_ssh()`, `http_connection()` |
| `remote_dev/core/state_store.py`, `locking.py` & `atomic.py` | Endpoint/context-scoped local records, cross-thread/process `record_lock` (`RLock` + `flock`/`msvcrt`), and `os.replace()` atomic file publication | `write_read_ledger()`, `load_write_ledger_guard()`, `record_lock()`, `serialize_mutation()` |

## 2. Core State, Concurrency & Timeout Invariants (`v0.0.2` Iron Rules)

1. **Timeout Arithmetic & Multi-Stage Isolation**:
   - **Job Stop Drain**: `job_ops.py` and `processes/worker.py` enforce `STOP_DRAIN_SECONDS >= STOP_GRACE_SECONDS + 3.0` (`max(5.0, grace + 3.0)` locally; `15.0 >= 10.0 + 3.0` on the worker) so `SIGTERM` group draining completes before `SIGKILL` escalation and outer poll cutoff.
   - **RPC Handshake Deadline**: `rpc_transport.py` computes `acquire_deadline` at `request()` entry and enforces it across both `_acquire()` pool waiting and `RpcConnection.request()` worker readiness (`self.ready.wait`), preventing hangs on half-open SSH connections.
   - **Two-Stage SSH Diagnostic**: `diagnostics.diagnose_ssh()` splits `timeout_ms` into Stage 1 (`60%`, multiplexed) and Stage 2 (`max(2000ms, 40%)`, independent `ssh_mux=False`) so a hung `ControlMaster` socket never starves the fallback probe.
2. **Remote `python3 -I` Isolation & Standalone Worker Modules (Iron Rule 4)**:
   - All remote Python workers (`rpc_worker.py`, `worker.py`, `artifact_worker.py`, `CONNECTION_PROBE_SCRIPT`) execute under `python3 -I` so files in the remote login directory or workspace never shadow Python stdlib modules.
   - `remote_file_worker.py` is maintained as a standalone `.py` module loaded via `functools.lru_cache(maxsize=1)` in `file_ops.py`.
3. **External-Effect Honesty (`not_sent` / `acknowledged` / `uncertain`) (Iron Rule 2 & Principle 14)**:
   - Never automatically retry a submitted remote mutation or job launch.
   - Pre-dispatch failures set `submission_state="not_sent"` (`retryable=True` when transient); post-write disconnects/timeouts on mutating calls set `submission_state="uncertain"`.
   - If a remote operation succeeds (`acknowledged`) but local read-ledger, cursor, or snapshot recording fails, `recording_failure()` preserves `operation_completed=True` and `execution_outcome="success"` while surfacing `status="local_recording_failed"`.
4. **Lightweight Atomic Persistence & Locking (Iron Rule 3)**:
   - Local and remote file replacements use sibling temp files + `fsync` + `os.replace()` (`atomic.replace_file`), without redundant `lstat`/`fstat` ownership ceremonies or shadow state machines.
   - Long job output polls (`yield_time_ms > 250` in `remote_job_stdin`) snapshot the cursor and wait outside `record_lock(path)` so concurrent `stdin` writes or `job_stop` calls never block behind a waiting reader.

## 3. Verification Commands

Run the local unit + property test suite:

```bash
pytest -q
```

Run the live non-destructive integration suite against standing A2 (`173.125.1.2`) and A3 (`192.168.13.165`) Ascend NPU clusters:

```bash
REMOTE_DEV_LIVE_NPU=1 pytest tests/test_live_npu_endpoints.py -q
```
