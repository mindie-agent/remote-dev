# 容器直通、SSH 连接池与故障诊断参考

**中文** · [English](./transport-and-diagnostics.en.md) · [返回文档中心](./README.md)

这篇文档用大白话讲清楚 `remote-dev` 的五个进阶能力：**怎么直接钻进远端 Docker 容器干活、怎么写自定义查机器插件、怎么防并发改坏文件、底层 SSH 连接池怎么工作，以及遇到网络问题怎么排障。**

---

## 1. 直接在远端已有的 Docker 容器里干活

很多时候，算力服务器上已经起好了配好环境的 Docker 容器，但容器里根本没装 `sshd`，或者没映射额外的 SSH 端口。

在 `remote-dev` 里，你只需要在普通宿主机连接参数上多加一个 `container` 字段（或者 CLI 加 `--container <容器名或短ID>`），所有文件读写、搜索、打补丁、跑命令和传文件就都会直接进到那个容器里执行：

```json
{"host": "lab-host", "port": 22, "container": "repro-case", "cwd": "/work/vllm", "command": "bash /work/start-case.sh"}
```

- **容器里需要装什么？** 只要容器正在运行，并且里面有 `python3` 和 `bash` 就行（如果要用 Unified Diff 补丁，还需要 `git`）。`remote-dev` 绝不会往你的容器里乱装包，也不会擅自启停容器。
- **不怕同名容器重建串台**：每次你用容器名（比如 `repro-case`）发起操作时，`remote-dev` 会先通过宿主机查出它当前的 **64 位完整容器 ID** 并锁定下来。后续的后台任务查询、日志拉取和文件锁全部认准这个完整 ID，哪怕中途有人把容器删了重建一个同名的，也绝不会把命令发错地方。
- **哪些操作不走容器？** 如果需要在容器里用交互式终端，调 `remote.bash` 时传 `tty=true` 即可。而底层的首次密码引导（`run_interactive`）和 SSH 本地端口转发（`open_local_forward`）是宿主机级别的操作，不支持传 `container`（端口转发直接对宿主机暴露的端口做就行）。

---

## 2. 编写自定义查机器插件（Resolver）

如果你的团队有自己的机器清单、实验室设备表或者调度系统，不想每次手填 IP，可以写一个简单的 Python 函数注册成 Resolver 插件。比如按 `--selector lab=gpu-1` 自动查出机器 IP 和环境脚本：

```python
from remote_dev.core.endpoint import EndpointError, register_resolver, resolver_setup

def by_lab(payload):
    lab = payload.get("lab")
    if not lab:
        return None
    record = my_inventory.load(lab)
    if record is None:
        raise EndpointError(f"找不到实验室机器: {lab!r}")
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

然后通过环境变量 `REMOTE_DEV_RESOLVERS` 挂载给 MCP Server 或 CLI：

```bash
export REMOTE_DEV_RESOLVERS="/abs/path/consumer/remote_dev_plugin.py:setup"
remote-dev bash --selector lab=gpu-1 --command "nproc"
```

---

## 3. 目录范围限制与“先读后改”防覆盖账本

- **目录范围限制（Path Policy）**：默认情况下 `root=/` 且 `cwd=/`，方便你查看服务器上的任何地方（同时依然会拦截恶意符号链接逃逸）。如果你想把 Agent 严格关在某个工作目录里，只需传入 `--root /srv/app --cwd /srv/app`，超出 `/srv/app` 的读写就会被直接拦下。
- **先读后改防覆盖（Read Ledger）**：
  - 账本按客户端会话 ID（自动识别 `client_context_id`、`CLAUDE_SESSION_ID`、`CODEX_SESSION_ID` 等）分开存放在本地 `state/` 目录下。
  - 还没读过的新文件可以直接写；但只要在当前会话里用 `remote_read` 读过某个文件，后续再用 `remote_edit` 或 `remote_write` 修改它时，底座会自动比对远端文件当前的 SHA-256 是否跟刚才读的时候一致。如果发现中途被别人改过了，会立刻报错拦截，避免把别人的修改覆盖掉。

---

## 4. SSH 连接池、长流隧道与进程管理原语

- **自带控制通道隔离的 SSH 连接池**：
  - 最多保持 32 条 SSH 二进制 RPC 长连接，空闲 5 分钟自动回收。
  - 内部拆分为 **8 个常规 Worker（32 并发槽位）** 和 **2 个专用控制 Worker（8 并发槽位）**。查状态（`status`）、看日志（`tail`）、停任务（`stop`）和发输入（`stdin`）走专用控制通道，永远不会被慢命令堵死。
- **供上层 Python 调用的底层流与隧道函数**：
  - `Endpoint.for_long_stream(host, port, ...)`：专门给跑几个小时的长日志流或 `ssh -N -L` 端口转发准备的端点配置。它会自动关闭 `ssh_mux`（多路复用）并开启心跳保活（每 30 秒一次，最多容忍 10 次），防止 OpenSSH 的 `ControlMaster` 把 `-N` 隧道吞掉导致假退出。
  - `remote_dev.core.ssh_transport.run_stream`：建立长连接实时流式读取远端输出，支持本地与远端双重超时控制。
  - `open_local_forward(endpoint, remote_port)`：建立 `ssh -N -L` 本地端口转发，提供 `.local_port`、`.wait_ready(timeout_s)` 和 `.close()`。
- **干净利落的远端进程监管（`remote_dev.processes.control`）**：
  - 支持 `prepare`、`go`、`status`、`tail`、`stop`、`stdin`、`launch`、`exchange`。
  - 在 Linux 上，远端监管进程会自动开启 Subreaper 模式，不管你的脚本派生了多少层子进程，`stop` 时都能一网打尽。

---

## 5. 连不上或响应慢？用内置诊断工具看个明白

- **一键测出连接卡在哪一步**：
  ```bash
  remote-dev probe --host <服务器IP> --port <SSH端口> --diagnose-connection
  ```
  这条只读命令会把一次连接拆解成：**本地准备耗时、SSH 进程耗时、TCP/认证握手里程碑、远端执行耗时、输出流回收耗时**。如果发现是 SSH 多路复用（Mux）卡死，它会自动用独立连接做一次对比测试并提示你设置 `ssh_mux=false`（全程只读，绝不会重跑你的业务命令）。
- **每次调用都附带结构化诊断信息**：
  每个工具的返回结果里都带有 `diagnostics`（包含 `operation_id`、`trace_id`、起止时间、各阶段耗时和本地记录路径）以及 `error_details`。出错时会明确告诉你命令到底是**根本没发出去（`not_sent` / `not_executed`）**、**已经执行（`acknowledged`）**，还是**中途断网结果未知（`uncertain`）**。
- **严格的隐私脱敏**：
  诊断日志默认级别为 `MINDIE_LOG_LEVEL=INFO`（目录可通过 `MINDIE_DIAGNOSTICS_ROOT` 修改）。你的命令原文、文件内容、环境变量值和密码密钥**永远不会写进诊断日志**，可以放心用 `mindie-diagnostics bundle` 导出离线诊断包用来提 Issue。
