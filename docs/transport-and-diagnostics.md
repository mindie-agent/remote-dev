# 传输层、容器执行与诊断参考

**中文** · [English](./transport-and-diagnostics.en.md) · [返回文档中心](./README.md)

本文档整理 `remote-dev` 在已有 Docker 容器执行、Resolver 插件扩展、底层 SSH 传输与流原语、受管进程监督以及结构化诊断方面的完整技术规范。

---

## 1. 在已有 Docker 容器内工作

在普通 SSH 主机端点上附加 `container` 字段（或 CLI `--container <name-or-id>`），即可让文件读写、编辑、搜索、补丁、命令、后台作业与产物传输直接在该运行中的容器内执行：

```json
{"host": "lab-host", "port": 22, "container": "repro-case", "cwd": "/work/vllm", "command": "bash /work/start-case.sh"}
```

- **零侵入先决条件**：容器需处于运行状态并具备 `python3` 与 `bash`（Unified Diff 补丁需 `git`）。`user` 仍为宿主机 SSH 用户，`docker exec` 保持容器自身的配置用户与环境。`remote-dev` 不会安装软件包、不会启停容器，也不隐式回退到宿主机。
- **容器 ID 冻结机制**：每次基于容器名称或短 ID 的新操作会通过单次只读 `docker inspect` 解析出完整容器 ID（写入 `target.container`，原选择器保留在 `target.container_selector`）。同一操作、作业回执、读取账本与连接池复用均锁定该完整 ID，绝不使用易变的名称缓存或在提交不确定后自动重放。
- **容器 PTY 与限制**：容器内需要终端时使用 `remote.bash` 并指定 `tty=true`。底层交互式密码引导（`run_interactive`）与 SSH 本地端口转发（`open_local_forward`）显式拒绝容器端点；请通过显式宿主机端点转发已发布的端口。

---

## 2. Resolver 插件接口

除 `host` / `port` / `alias` 以外的所有业务选择器（如实验室机器清单、会话注册表、Coordinator 租约）均由消费方通过 Resolver 插件提供：

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

通过环境变量加载到 MCP Server 或 CLI 进程：

```bash
export REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
remote-dev bash --selector lab=gpu-1 --command "nproc"
```

---

## 3. 路径权限与读取账本（Read Ledger）

- **默认路径策略**：直连端点默认 `root=/` 且 `cwd=/`，但依然强制执行路径包含校验与符号链接逃逸检查。需要目录隔离时可显式传入 `--root /srv/app --cwd /srv/app`。
- **乐观并发读取账本**：按 `client_context_id`（依次回退至 `CLAUDE_SESSION_ID`、`CODEX_SESSION_ID`、`CODEX_RUN_ID`、`REMOTE_DEV_SESSION_ID`）隔离在 `id-<sha256>` 目录下（Schema `remote-dev.read_ledger.v2`）。未读过的文件可直接写入；一旦在当前上下文读取过某文件，后续写入或编辑会自动校验记录的 SHA-256，防止覆盖并发修改。

---

## 4. SSH 传输层、长连接流与进程监督

- **开发者工具连接池**：MCP 与 CLI 工具内部使用独立的二进制 SSH Stdio RPC 连接池（上限 32 个连接，空闲 5 分钟自动回收，预留 2 个控制 Worker / 8 个槽位处理 `status` / `stop` / `tail` / `stdin`）。
- **底层 Python 传输原语**：
  - `Endpoint.for_long_stream(host, port, ...)`：强制设置 `ssh_mux=False` 与 `keepalive=True`（`ServerAliveInterval=30`、`ServerAliveCountMax=10`），专用于小时级长流与隧道，防止 `ControlMaster` 吞掉 `ssh -N -L` 导致静默退出。
  - `remote_dev.core.ssh_transport.run_stream`：保持附加并实时转发输出，脚本通过二进制 stdin 传输，支持跨平台超时控制。
  - `open_local_forward(endpoint, remote_port)`：基于 `for_long_stream` 建立 `ssh -N -L` 本地端口转发并暴露 `local_port`、`wait_ready(timeout_s)` 与 `close()`。
  - `run_interactive(endpoint, remote_command)`：用于首次接触的 TTY 交互式引导（`BatchMode=no`），严禁与多路复用混用。
- **受管后台进程监督（`remote_dev.processes.control`）**：支持 `prepare`、`go`、`status`、`tail`、`stop`、`stdin`、`launch`、`exchange`。Linux Worker 作为子进程 Subreaper 运行，确保 `stop` 精确终止所属进程族。

---

## 5. 连接探测与结构化诊断

- **连接阶段探测**：`remote-dev probe --host <host> --port <port> --diagnose-connection` 发送单次只读探测，汇报本地准备、SSH 进程耗时、TCP/认证里程碑、远端执行与流排空耗时。当多路复用连接失败时，会对比独立连接并提示 `ssh_mux=false`，但绝不自动重放业务命令。
- **结构化诊断信封**：每次工具响应均包含 `diagnostics`（`operation_id`、`trace_id`、UTC 起止时间、单调时钟耗时、有界阶段摘要与本地 `record_ref`）以及 `error_details`（精确区分 `not_sent` / `not_executed`、`uncertain` 与 `acknowledged`）。
- **隐私与日志脱敏**：默认 `MINDIE_LOG_LEVEL=INFO`，日志目录可通过 `MINDIE_DIAGNOSTICS_ROOT` 指定。命令原文、文件内容、环境变量值与凭据绝不写入诊断日志参数；可使用 `mindie-diagnostics bundle` 生成脱敏的离线诊断包。
