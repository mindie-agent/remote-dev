# `remote-dev` 远端开发底座架构设计

**中文** · [English](../DESIGN.md) · [返回文档中心](./README.md)

`remote-dev` 让 Codex、Claude Code、Cursor、Kimi Code、Grok Build 等支持 MCP 的 Coding Agent 在远端 Linux 主机或容器上的开发体验与本地开发保持一致。本地工作继续使用客户端原生的 `Read` / `Edit` / `Write` / `Bash` / `Glob` / `Grep` / `apply_patch` 工具；远端工作使用对应的 `remote.*` 工具，仅增加显式端点字段。

---

## 三层架构边界

### Layer A：远端原生开发工具面

- `remote.read` (`RemoteRead`)
- `remote.write` (`RemoteWrite`)
- `remote.edit` (`RemoteEdit`)
- `remote.multi_edit` (`RemoteMultiEdit`)
- `remote.bash` (`RemoteBash`)
- `remote.glob` (`RemoteGlob`)
- `remote.grep` (`RemoteGrep`)
- `remote.ls` (`RemoteLS`)
- `remote.apply_patch` (`RemoteApplyPatch`)

同时包含后台作业管理（`remote.job_status` / `job_tail` / `job_stop` / `job_stdin`）、产物同步（`remote.artifact_manifest` / `artifact_pull` / `artifact_push`）以及端点探测（`remote.probe`、`remote.context_snapshot`）。

### Layer B：共享底座

- **端点标识与显式解析**（`remote_dev.core.endpoint`）。
- **已有 Docker 容器坐标**（`remote_dev.core.container_endpoint`）：通过宿主机既有 RPC 将容器名称或短 ID 解析为运行中的完整容器 ID，并在写入作业记录或并发账本前冻结该完整 ID；直接传入完整 ID 时无需额外探测。
- **消费方自定义选择器 Resolver 插件接口**（`remote_dev.core.endpoint`）。
- **跨平台二进制 SSH Stdio RPC 连接池**（`remote_dev.core.rpc_transport`）：同时支持 Windows 与 POSIX，具备请求 ID、有界代码缓存、作用域取消、并发请求调度以及“结果未知绝不自动重放”保证。
- **底层 SSH 选项、附加流、端口转发与首次交互引导**（`remote_dev.core.ssh_transport`）：保留显式 `ControlMaster` 策略控制。
- **全权限默认根与可选路径围栏策略**（`remote_dev.core.path_policy`、`remote_dev.core.permissions`）。
- **可选读取账本乐观并发校验**（`remote_dev.core.read_ledger`、`remote_dev.core.state_store`）。
- **紧凑预览与完整本地引用**（`remote_dev.core.preview`、`remote_dev.result`）。
- **通用远端进程监督器**（`remote_dev.processes.control`）与基于持久化字节游标的执行/让出/轮询会话（`remote_dev.core.job_ops`），支持管道输入、真实 PTY 与同步库等待。
- **产物清单与有界二进制分块流**（`remote_dev.core.artifact_ops`、`remote_dev.core.artifact_transport`），在 SHA-256 校验通过后执行原子替换。
- **本地记录锁与远端共享变更锁**：跨文件工具、补丁与产物提交互斥，并发只读操作互不阻塞。
- **Hook 守卫接口**（`remote_dev.hooks`）。
- **并发 MCP Server 与资源服务**（`remote_dev.mcp`）：配备 8 个常规 Worker（32 槽位）与 2 个预留控制 Worker（8 槽位），远端 RPC 采用相同配额隔离。

`ssh_command` 是统一的完整命令构造入口。容器内操作通过 `docker exec -i <full-id>` 驱动既有的 Python RPC Worker、二进制产物 Worker、Bash 脚本与数据流，无需在容器内部署 SSH 服务、独立 Daemon 或依赖安装器。容器 ID 会隔离本地状态、变更锁与连接池；已持久化的作业记录始终绑定原容器完整 ID，不会重新解析可变容器名。

### Layer C：上层消费方

领域工作流 Skill、会话管理器与多角色协调器（Coordinator）位于各自的消费方仓库中，仅通过工具调用、环境变量与注册的 Resolver 插件与 `remote-dev` 交互。`remote-dev` 绝不反向导入 Layer C。

---

## 依赖方向

`remote-dev` 底座仅依赖 Python 标准库、`mindie-diagnostics` 与本地 `ssh` 客户端可执行文件；所有上层消费方单向依赖本底座：

- **显式端点解析**：原生支持 `host` / `port` / `user` / `root` / `cwd` / `container` 与 `alias`。其他业务选择器一律通过 `remote_dev.core.endpoint.register_resolver` 或 `REMOTE_DEV_RESOLVERS` 插件解析。
- **通用进程控制**：`remote_dev.processes.control` 同时服务普通后台作业与 Coordinator 受管执行，无需在远端主机额外部署 Coordinator。
- **环境变量注入配置**：通过 `REMOTE_DEV_RUNTIME_ENV_FILE`、`REMOTE_DEV_SSH_MUX_DIR`、`REMOTE_DEV_STATE_DIR` 与 `REMOTE_DEV_ENDPOINTS_FILE` 解耦消费方特定路径。

---

## MCP 传输与资源

MCP Server 支持标准 stdio `Content-Length` JSON-RPC 帧协议（同时保留按行分隔的 JSON-RPC 作为本地轻量测试回退）。

暴露的 MCP 资源包括：

- `remote://endpoints`
- `remote://endpoint/<endpoint-id>/context/latest`
- `remote://endpoint/<endpoint-id>/jobs`
- `remote://endpoint/<endpoint-id>/job/<job-id>/status`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stdout`
- `remote://endpoint/<endpoint-id>/job/<job-id>/stderr`
- `remote://endpoint/<endpoint-id>/artifacts`
- `remote://endpoint/<endpoint-id>/artifacts/<artifact-id>/manifest`

---

## 本地与联机验证

```bash
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

针对可达 SSH 主机的联机验证：

```bash
remote-dev validate --host <host> --port <port> --root /srv/app --cwd /srv/app
```

更多传输层、容器与可观测性细节请参阅 [传输层、容器与诊断参考](./transport-and-diagnostics.md)。
