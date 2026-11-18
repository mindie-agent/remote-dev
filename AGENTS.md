# Remote Dev (`remote-dev`) — Control-Plane Architecture & Invariants

Shared SSH workspace, durable background job supervision, and 7-Harness MCP substrate (`control-plane` repository class) for MindIE Agent.

## 1. Domain Boundary & Five Iron Rules

1. **Shared SSH Auth, Concurrency Ledgers & External Contracts**:
   - `remote_dev/auth/` is the sole owner of user-scoped SSH profiles (`~/.mindie/ssh/credentials.json`) consumed by `coordinator` and `npu-top`.
   - Preserve all external import paths (`ssh_forward.py` re-exports in `ssh_transport.py`, `tools/validate_remote_dev_scaffold.py`, `wb_native_compat.py`), optimistic read-ledger SHA-256 hashes, and atomic multi-file patch rollback.
2. **Zero Session-Binding Gates & Concurrent MCP Dispatch (`P2.7`, `AD-02`, `AD-03`)**:
   - Standard zero-`_meta` MCP `tools/call` works without prior session hooks or required `request_nonce`.
   - Explicit `job_id` handles resolve across MCP reconnections and subagents within the user profile (`_assert_visible` never raises cross-session `PermissionError`; `session_scope` is correlation metadata only).
   - `catalog_refresh.serve_selected` dispatches concurrent `tools/call` requests across warm resident helper pools (work + control lanes) without `-32000` single-flight blocks or cold per-call `python -I --once` startups.
3. **Standalone Pure-Stdlib Workers, Streaming I/O & Event-Driven Supervision (`Iron Rule 4`, `Principle 18`)**:
   - All 12 remote worker modules (`remote_file_worker.py`, `remote_file_text.py`, `search_worker.py`, `patch_worker.py`, `worker.py`, `worker_runtime.py`, `worker_gc.py`, `artifact_worker.py`, `python_inventory.py`, `mutation.py`, `probe.py`, `rpc_worker.py`) use 100% Python stdlib (`python3 -I` safe), and the compressed Windows SSH `rpc_worker.py` + `mutation.py` bootstrap stays strictly `< 8191` chars.
   - File/artifact SHA-256 digests stream in `64 KiB` chunks with valid in-root symlink and `.git` linked-worktree support (`AD-07`) while blocking out-of-root symlink escapes and destination symlink overwrites.
   - Local `_posix_owner.py` and remote `worker.py` / `worker_runtime.py` use `selectors.DefaultSelector` + wakeup pipes/signals instead of `20 Hz` / `100 Hz` sleep spin-loops.
4. **External-Effect & Receipt Honesty (`Principles 9 & 14`, Iron Rule 2)**:
   - Never replay an `uncertain` mutation; always place `except TimeoutError: raise` before `except OSError:`.
   - `dsh_bash` inspects failed/uncertain launch outcomes before returning background job handles.
   - When a primary remote operation completes (`operation_completed=True`), `recording_failure()` preserves `outcome="success"` (`isError=False`) and attaches `receipt_warning` + `receipt_unconfirmed=True`.
5. **Bounded Local & Remote Retention (`Principle 18`)**:
   - `prune_endpoint_state()` bounds `reads/`, `logs/`, terminal `jobs/`, `context/` (protecting `latest.json` and its `full_probe`), `artifacts/` (protecting `< 60s` transfers), and `patches/`.
   - `prune_stale_session_runtime_dirs()` verifies dead PIDs without `os.kill(pid, 0)` on Windows and checks both POSIX `flock` and Windows `CreateFileW(dwShareMode=0)` + `msvcrt.locking(LK_NBLCK)`, never deleting SQLite receipt stores.

## 2. Module Navigation

- `remote_dev/mcp/server.py`, `tools.py`, `schemas.py`, `catalog_refresh.py`, `_resident_helper.py`: Concurrent stdio MCP server, strict 7-Harness `tools/list`/`tools/call` boundary, and warm generation-aware resident gateway.
- `remote_dev/mcp/profiles_*.py`, `harness_profiles.py`, `native_spec.py`, `native_handlers_*.py`, `native_common.py`, `wb_native_compat.py`: Harness-native adapters (`codex`, `claude`, `kimi`, `dsh`, `wb`, `agy`, `oc`) with per-profile support-tool declarations.
- `remote_dev/auth/`: Shared SSH key enrollment, credential store, non-secret endpoint alias persistence (`endpoint_store.py`), bounded onboarding discovery (`discover.py`), and pre-dispatch verification.
- `remote_dev/core/ssh_transport.py`, `ssh_stream.py`, `ssh_forward.py`, `rpc_transport.py`, `local_process.py`, `_posix_owner.py`: SSH argv/ControlMaster policy, binary RPC pool, port forwards, and owned process-group / Windows Job supervision.
- `remote_dev/core/file_ops.py`, `patch_ops.py`, `artifact_ops.py`, `search_ops.py`, `job_ops.py`, `shell_ops.py`: Remote workspace operations and durable job control.
- `remote_dev/core/state_store.py`, `retention.py`, `session_retention.py`, `windows_probe.py`: Atomic local state and cross-platform bounded GC.

## 3. Verification & Pre-PR Gate

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) ([English](CONTRIBUTING.en.md)) before submitting changes:

```bash
# Local isolated Tier 0/1 suite
pytest -q -m "not live_npu"

# Standing A2 (173.125.1.2) & A3 (192.168.13.165) non-destructive Tier 2 suite (never use .153)
REMOTE_DEV_LIVE_NPU=1 pytest tests/test_live_npu_endpoints.py -q

# Unix LF line-ending & control-plane PR checklist gates
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile control-plane --body-file <pr_body.md>
```
