# 贡献指南 (`remote-dev` — `control-plane`)

**中文** · [English](./CONTRIBUTING.en.md)

`remote-dev` 属于 MindIE Agent **远端与集群控制面（`control-plane`）** 仓库类别，提供跨宿主共享的 SSH JSON-RPC 远端工作区、可恢复后台任务监管、流式二进制产物传输与统一 SSH 凭据底座。

## 1. 核心架构红线

1. **共享凭据与并发读写账本（`CTRL-LEASE-OR-AUTH`）**：
   - `remote_dev/auth/` 是 `~/.mindie/ssh/credentials.json` 共享 SSH 认证档案的唯一权威；保持全部 27+ 跨仓导出路径（含 `ssh_forward.py`、`validate_remote_dev_scaffold.py`、`wb_native_compat.py`）100% 向后兼容。
   - 保持 `reads/` 乐观并发 SHA-256 账本与 `patch_ops.py` 多文件原子回滚语义完整，严禁静默降级。
2. **零会话隔离门禁与并发 MCP 调度（`CTRL-HANDLE`）**：
   - 标准零 `_meta` MCP `tools/call` 免前置 Hook、免 `request_nonce` 直接可用（`P2.7`）。
   - 同一用户配置下，凭显式 `job_id` 必须允许子代理或重连连接直接查询状态、拉取日志与停止任务（严禁在 `_assert_visible` 抛出跨会话 `PermissionError`）。
   - `catalog_refresh.serve_selected` 必须通过常驻 Worker 池并发执行慢速前台任务与快速状态/读取请求，严禁返回 `-32000 "component call already running"` 或每次请求冷启动 `python -I --once`。
3. **纯标准库远端 Worker、流式哈希与事件驱动监管（`CTRL-STREAM-IO`）**：
   - 全部 12 个远端 Worker 脚本必须保持 100% Python 标准库（`python3 -I` 安全），且 Windows OpenSSH 压缩后的 `rpc_worker.py` + `mutation.py` 启动命令严格 `< 8191` 字符。
   - 文件与产物 SHA-256 计算统一采用 `64 KiB` 流式分块，禁止整文件 `read_bytes()`；允许 `endpoint.root` 内合法软链（如 HF Hub 快照与 `.so` 版本链）及 `.git` 文件工作树，同时拦截越界软链与写操作软链覆盖。
   - 进程监管（`_posix_owner.py`、`worker.py`）统一使用 `selectors` 与唤醒管道/信号，严禁高频 `sleep` 空转轮询。
4. **外部副作用与回执诚实性（`CTRL-RECEIPT`）**：
   - `except OSError:` 前必须显式 `except TimeoutError: raise`；`dsh_bash` 后台启动失败或 `uncertain` 时必须原样上报错误，严禁伪造 `"background job None"`。
   - 当远端主操作已完成（`operation_completed=True` 且主结果为成功）而本地回执落盘失败时，`recording_failure()` 必须保持 `outcome="success"`（`isError=False`）并附加 `receipt_warning` 与 `receipt_unconfirmed=True`。

## 2. 本地隔离测试与打包冒烟

所有测试必须隔离宿主机真实目录（`HOME`、`USERPROFILE`、`XDG_*`、`MINDIE_CONFIG_HOME`、`MINDIE_DATA_HOME`、`MINDIE_DIAGNOSTICS_ROOT`、`REMOTE_DEV_AUTH_DIR`、`REMOTE_DEV_STATE_DIR`、`UV_CACHE_DIR`、`TMPDIR`）：

```bash
# 1. 本地隔离单元测试套件
python -m pip install -e ".[test]"
pytest -q -m "not live_npu"

# 2. 源码目录外真实 Wheel 安装冒烟验证
uv build
smoke_dir="$(mktemp -d)"
(cd "$smoke_dir" && python3 -m venv .venv && .venv/bin/pip install /path/to/dist/*.whl && .venv/bin/remote-dev --help)
rm -rf "$smoke_dir"

# 3. 常驻 A2 (173.125.1.2) / A3 (192.168.13.165) 非破坏性实机验证（严禁连接 .153）
REMOTE_DEV_LIVE_NPU=1 pytest tests/test_live_npu_endpoints.py -q
```

## 3. 提交 PR 前自检（`control-plane` Profile）

1. 验证全部文本文件使用 Unix `LF`（`\n`）换行：
   ```bash
   python3 .github/scripts/check_lf_eol.py
   ```
2. 按 [`.github/pull_request_template.md`](.github/pull_request_template.md) 勾选全部 4 项稳定核查 ID（`CTRL-LEASE-OR-AUTH`、`CTRL-HANDLE`、`CTRL-STREAM-IO`、`CTRL-RECEIPT`），填写实测证据（纯文档改动可写 `not-applicable: docs-only`），并运行本地校验：
   ```bash
   python3 .github/scripts/check_pr_checklist.py --profile control-plane --body-file <pr_body.md>
   ```
