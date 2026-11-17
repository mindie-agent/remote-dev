## 变更摘要 (Summary)

- 

## 设计原则 Review (Pre-PR Checklist)

> Profile: `control-plane` (`check_pr_checklist.py --profile control-plane`). Every item must be checked (`- [x]`) and backed by concrete evidence or `not-applicable: <specific reason>` (e.g., `not-applicable: docs-only`).

- [ ] **CTRL-LEASE-OR-AUTH**: Shared `remote-dev` SSH auth/credential profiles (`~/.mindie/ssh/credentials.json`), optimistic concurrency read-ledger hashes, and multi-file atomic patch rollback remain intact without silent fallbacks.
- [ ] **CTRL-HANDLE**: Standard zero-`_meta` MCP `tools/call` and concurrent resident stdio dispatch execute without `-32000` single-flight blocks (`P2.7`), and explicit `job_id` handles resolve across reconnections and subagents within the user profile.
- [ ] **CTRL-STREAM-IO**: All standalone remote worker scripts remain 100% pure Python stdlib (`python -I` safe, Windows SSH `rpc_worker` bootstrap `< 8191` chars), file/artifact hashes stream in `64 KiB` chunks with valid in-root symlink support, and process supervision uses event-driven `selectors` without spin-polling.
- [ ] **CTRL-RECEIPT**: External mutations distinguish `not_sent` / `acknowledged` / `uncertain` (`TimeoutError` precedes `OSError`, failed/uncertain background launches never report fake job IDs), and post-success local receipt failures preserve `outcome="success"` (`isError=False`) with `receipt_warning` + `receipt_unconfirmed=True`.

- **CTRL-LEASE-OR-AUTH**: 
- **CTRL-HANDLE**: 
- **CTRL-STREAM-IO**: 
- **CTRL-RECEIPT**: 

## 验证结果 (Verification)

- 
