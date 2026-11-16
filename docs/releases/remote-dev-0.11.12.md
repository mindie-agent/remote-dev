# remote-dev 0.11.12：远程 NPU 作业存储上限与终态 GC、本地状态保留与测试临时目录修复

[English](remote-dev-0.11.12.en.md)

- **远程作业日志磁盘上限与诚实截断记账（`processes/worker.py`、`processes/worker_runtime.py`）**：PTY 与普通 pipe 输出统一经 `CappedLog` 环形落盘，`<stream>.log.prev` + `<stream>.log` 最多占用 `spec.max_log_bytes`（默认每流 `256 MiB`），替代原先无界追加；`logs.json` 与 `result.json` 记录 `logical_bytes_written` / `truncated_head_bytes`，`tail`/`exchange` 继续使用逻辑偏移，并在头部被轮转时返回 `*_truncated`、`*_skipped_bytes`、`*_logical_bytes`、`*_truncated_head_bytes`，不再把环形丢弃误报为干净 EOF。
- **远程终态作业 GC 与安全所有权（`processes/worker_runtime.py`、`core/job_ops.py`）**：在 `.remote-dev/jobs` 上按 `7 天` TTL、`128` 个终态作业、`2 GiB` 上限清理；仅处理当前远程 `os.getuid()` 拥有、`worker.lock`/`lock` 未被持有、`result.json` 状态明确终态且 `unknown` 为空、`descendants_drained is True` 或实时 `quiet` 的目录，先原子重命名为 `.trash-<job_id>-<token>` 再删除；`running`、`starting`、`stopping`、`unknown`、`retain_until_release` 永不删除。GC 带 `300s` 冷却与 `512` 条扫描上限，在作业创建与停止路径机会式触发。
- **本地端点状态保留与死会话目录清理（`core/retention.py`、`core/state_store.py`、`core/job_ops.py`）**：`state/endpoints/<id>/` 的 `reads/` 与 `logs/` 按 `7 天` / `256` 条 / `128 MiB` 清理，本地 `jobs/<job_id>.*` 仅在记录状态确认终态时按 `14 天` / `128` 条 / `256 MiB` 清理，`running`/`unknown` 记录严格保留；写入路径带 `300s` 冷却与 `2s` 有界预算，清理失败不阻塞已提交的写入。新增 `prune_stale_session_runtime_dirs()`，仅删除 `gate-v1*`/`runtime*`/`session*` 中 PID 已死、锁未被持有且超过 `24h` 的目录，并严格跳过任何包含 SQLite receipt 存储的目录（`remote-dev` 本身不管理 `gate-v1` 布局，故以显式 helper 提供给上层）。
- **测试临时目录泄漏修复（`tests/test_native_contracts.py`）**：`test_tail_text_reads_bounded_suffix_without_whole_file` 改用 `tempfile.TemporaryDirectory`，并新增回归用例断言执行后 `$TMPDIR` 中不新增 `tail-bounded-*` 目录，消除每轮 `64 MiB` 稀疏文件残留。
- **版本与发布**：`0.11.11` → `0.11.12`；`worker_runtime.py` 纳入 wheel `force-include` 与远程 worker 组合源；新增 `tests/test_retention.py` 覆盖远程终态 GC、环形日志记账、本地 `reads`/`logs`/`jobs` 保留与 receipt 保护。

验证：`pytest -q -m "not live_npu"` 通过（`1040 passed, 60 skipped, 6 deselected`），隔离 `$TMPDIR` 无 `tail-bounded-*` 残留，全部变更文件为 Unix `LF`。
