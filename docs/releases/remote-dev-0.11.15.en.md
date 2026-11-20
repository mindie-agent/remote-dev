# remote-dev 0.11.15: Lock-Free Per-Request `session_scope` Isolation, MCP Import Purity, and Windows Long-Path Hardening

[中文](remote-dev-0.11.15.md) · **English**

When multi-session hosts such as `mindie-harness` dispatch concurrent `call_tool` requests inside a single Python process, mutating process-global `os.environ` causes session IDs, local state directories, and SSH credential stores to bleed across threads. `remote-dev v0.11.15` introduces lock-free `ContextVar`-backed `session_scope` isolation, removes import-time environment mutation from `remote_dev.mcp.server`, and hardens Windows `> 260`-character paths and stream cleanup edge cases.

## Lock-Free Per-Request Session Isolation (`ContextVar` `session_scope`)

`remote_dev.core.state_store` adds the `session_scope(session_id, *, state_dir=None, auth_dir=None)` context manager and `scoped_auth_dir()` accessor so in-process callers can bind per-request isolation without touching `os.environ`:

```python
from remote_dev.core.state_store import session_scope
from remote_dev.mcp.tools import call_tool

with session_scope("sess-worker-1", state_dir="/tmp/state-1", auth_dir="/tmp/ssh-1"):
    result = call_tool("remote_read", {"host": "192.0.2.10", "port": 22, "path": "/workspace/model.py"})
```

- **End-to-End State, Read-Ledger, and Auth Isolation**: `state_root()`, `_effective_ledger_context_id()`, and `remote_dev.auth.store.auth_root()` resolve active `ContextVar` bindings before falling back to `REMOTE_DEV_STATE_DIR`, `REMOTE_DEV_SESSION_ID` (and host session env vars), or `REMOTE_DEV_AUTH_DIR` (`~/.mindie/ssh` by default). Explicit `client_context_id` arguments retain top precedence, and nested `session_scope` blocks restore outer tokens cleanly on both normal exit and exception unwind.
- **Auth-Root Isolation in the SSH RPC Pool**: `remote_dev/core/rpc_transport.py` includes `str(auth_root())` in the pooled connection key so concurrent sessions bound to distinct `auth_dir` roots never share or cross-reuse authenticated SSH channels.
- **Automatic Thread-Pool Context Propagation**: `remote_dev/mcp/catalog_refresh.py` (`CatalogRefresh.start` plus the `mindie-mcp-work` and `mindie-mcp-ctrl` executors in `serve_selected`) and `remote_dev/tools/validate_remote_dev_scaffold.py` dispatch worker tasks through `contextvars.copy_context().run`, giving concurrent in-process requests 100% isolated read-before-write ledgers (`reads/`), job state directories, and SSH credential stores.

## Import-Time Purity in MCP Server & Lean Surface Cleanup

- **Zero Environment Side Effects on Module Import**: Removed the top-level `os.environ.setdefault("REMOTE_DEV_SESSION_ID", ...)` call from `remote_dev/mcp/server.py` so importing the module never mutates `os.environ`. Running the standalone stdio server (`_serve()` / `remote-dev server`) still initializes a process-level `mcp-<pid>-<rand>` default when unset.
- **Superseded Shim Removal**: Deleted unreferenced `remote_dev/core/text_ops.py` and legacy `remote_dev/mcp/adapters/*` shims so every production module remains reachable and single-sourced.

## Windows Long-Path Support & Stream Cleanup Reliability

- **Windows Extended-Length Paths Past `260` Characters (`MAX_PATH`)**: Unified `\\?\` and `\\?\UNC\` extended-length path handling across `remote_dev/core/state_store.py` (`_fs_path`), `job_records.py`, `retention.py`, and `locking.py`. `locking._lock_key` normalizes extended and standard paths to the same in-process `RLock` key, while `maybe_prune_endpoint_state_dir` preserves clean caller-facing `base` path strings after pruning deep state trees.
- **Staged Tempfile Cleanup Before Negative ACK**: `remote_dev/processes/artifact_worker.py` now unlinks the staged `.part` temporary file before sending `{"status": "hash_mismatch", ...}` and closing the transfer stream, preventing orphan partial files on remote targets.
- **Buffered Output Drain Across Cleanup Errors & Timeout Propagation**: `remote_dev/core/ssh_transport.py` records `owner.stop()` exceptions into `cleanup` while still draining buffered `stdout`/`stderr` via bounded `proc.communicate(timeout=1)` so diagnostic output survives teardown errors; `remote_dev/core/artifact_ops.py` explicitly re-raises `TimeoutError` before `except OSError:` during directory traversal.

## Verification

- Full isolated local test suite (`pytest -q -m "not live_npu"`): **`948 passed, 0 failed`**.
- Added `tests/test_session_scope.py` covering 8-thread `threading.Barrier`-synchronized `session_scope` ledger and credential isolation, nested scope precedence and exception unwind restoration, `serve_selected` work/control lane `ContextVar` propagation, RPC connection-pool `auth_root` key isolation, and `> 260`-character Windows path state/job/retention operations.
