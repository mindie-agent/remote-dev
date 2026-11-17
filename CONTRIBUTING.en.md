# Contributing Guide (`remote-dev` — `control-plane`)

[中文](./CONTRIBUTING.md) · **English**

`remote-dev` belongs to the MindIE Agent **`control-plane`** repository class, providing the shared SSH JSON-RPC workspace substrate, durable background job supervision, streaming binary artifact transfer, and unified SSH credential management.

## 1. Core Architectural Guardrails

1. **Shared Credentials & Optimistic Concurrency Ledgers (`CTRL-LEASE-OR-AUTH`)**:
   - `remote_dev/auth/` owns shared SSH authentication profiles (`~/.mindie/ssh/credentials.json`); preserve all 27+ external import paths (`ssh_forward.py`, `validate_remote_dev_scaffold.py`, `wb_native_compat.py`).
   - Preserve read-ledger SHA-256 optimistic concurrency guards and `patch_ops.py` atomic multi-file rollback without silent fallbacks.
2. **Zero Session-Equality Gates & Concurrent MCP Dispatch (`CTRL-HANDLE`)**:
   - Standard zero-`_meta` MCP `tools/call` works without prior hooks or `request_nonce` (`P2.7`).
   - Explicit `job_id` handles must remain inspectable and stoppable across MCP reconnections and subagents within the same user profile (`_assert_visible` never blocks cross-session access).
   - `catalog_refresh.serve_selected` must execute concurrent foreground and status/read requests across warm resident worker pools without `-32000` single-flight rejections or per-call `python -I --once` cold starts.
3. **Pure-Stdlib Remote Workers, Streaming Hashes & Event-Driven Waits (`CTRL-STREAM-IO`)**:
   - Keep all 12 remote worker scripts 100% pure Python stdlib (`python3 -I` safe) and keep the compressed Windows SSH `rpc_worker.py` + `mutation.py` command line strictly `< 8191` chars.
   - Stream file/artifact SHA-256 digests in `64 KiB` chunks (never whole-file `read_bytes()`); permit valid symlinks and `.git` worktree files inside `endpoint.root` while rejecting escaping symlinks and destination symlink overwrites.
   - Use `selectors.DefaultSelector` + wakeup pipes/signals in `_posix_owner.py` and `worker.py` rather than high-frequency sleep loops.
4. **External-Effect & Receipt Honesty (`CTRL-RECEIPT`)**:
   - Place `except TimeoutError: raise` before `except OSError:`; `dsh_bash` must surface failed/uncertain background launches instead of returning fake background job IDs.
   - When a primary remote operation completes (`operation_completed=True` with primary success) and local receipt persistence fails, `recording_failure()` must preserve `outcome="success"` (`isError=False`) with `receipt_warning` + `receipt_unconfirmed=True`.

## 2. Isolated Testing & Out-of-Tree Wheel Smoke Check

Isolate host paths (`HOME`, `USERPROFILE`, `XDG_*`, `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`, `REMOTE_DEV_AUTH_DIR`, `REMOTE_DEV_STATE_DIR`, `UV_CACHE_DIR`, `TMPDIR`) in every test run:

```bash
# 1. Local isolated test suite
python -m pip install -e ".[test]"
pytest -q -m "not live_npu"

# 2. Out-of-tree wheel install smoke check
uv build
smoke_dir="$(mktemp -d)"
(cd "$smoke_dir" && python3 -m venv .venv && .venv/bin/pip install /path/to/dist/*.whl && .venv/bin/remote-dev --help)
rm -rf "$smoke_dir"

# 3. Non-destructive Tier 2 live NPU verification on standing A2 (173.125.1.2) / A3 (192.168.13.165) — never use .153
REMOTE_DEV_LIVE_NPU=1 pytest tests/test_live_npu_endpoints.py -q
```

## 3. Pre-PR Gate (`control-plane` Profile)

1. Verify Unix `LF` (`\n`) line endings across tracked files:
   ```bash
   python3 .github/scripts/check_lf_eol.py
   ```
2. Complete all four checklist IDs (`CTRL-LEASE-OR-AUTH`, `CTRL-HANDLE`, `CTRL-STREAM-IO`, `CTRL-RECEIPT`) in [`.github/pull_request_template.md`](.github/pull_request_template.md) with concrete verification evidence (or `not-applicable: docs-only` for documentation-only PRs) and validate locally:
   ```bash
   python3 .github/scripts/check_pr_checklist.py --profile control-plane --body-file <pr_body.md>
   ```
