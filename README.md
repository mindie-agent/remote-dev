<p align="center">
  <img src="assets/brand/remote-dev-logo.png" alt="remote-dev logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent Remote Dev (<code>remote-dev</code>)</h1>

<p align="center">
  <strong>面向 Coding Agent 的 SSH 远端开发底座——让远端 Linux 主机与容器拥有与本地工具完全对等的读写、编辑、搜索、补丁、进程与产物能力</strong>
</p>

<p align="center">
  <strong>中文</strong> · <a href="./README.en.md">English</a> · <a href="./docs/README.md">文档中心</a> · <a href="https://github.com/mindie-agent/mindie-agent">MindIE Agent 主仓库</a>
</p>

---

## 项目简介

`remote-dev` 是 [MindIE Agent](https://github.com/mindie-agent/mindie-agent) 的远端开发执行底座（Python 包名 `remote-dev`，导入名 `remote_dev`，CLI 入口 `remote-dev`）。它为 Claude Code、Codex、Kimi Code、Cursor 等支持 MCP 或 CLI 的 Coding Agent 提供与本地原生编辑器工具一一对应的远端孪生工具（Remote Twins），只需附加显式的远端端点参数（`host` / `port` / `container` / `alias`），即可在远端 Linux 主机或运行中的 Docker 容器内无缝执行代码浏览、精确修改、交互式/后台命令与产物传输。

- **19 个远端工具对称映射**：由单一 stdio MCP Server（`remote-dev server`）与 CLI 子命令一对一暴露。
- **主机与已有容器无缝切换**：在端点上附加 `container` 即可直接在已有 Docker 容器内执行文件读写、补丁、命令与后台作业，无需在容器内额外部署 SSH 服务或 Daemon。
- **二进制 SSH Stdio RPC 连接池**：跨 macOS、Linux 与原生 Windows 复用长连接池，预留独立控制通道（`status` / `tail` / `stop` / `stdin`），长耗时命令不会阻塞状态查询。
- **零隐式状态与可插拔解析器**：严格遵循显式端点契约，不猜测工作目录，支持上层消费方通过 `REMOTE_DEV_RESOLVERS` 注册自定义选择器（如 `lab`、`session_id`）。

---

## 本地工具与远端工具对照

| 本地工具 | 远端 MCP 工具 (`remote.*` / `remote_*`) | CLI 子命令 | 说明 |
| --- | --- | --- | --- |
| `Read` | `remote.read` (`remote_read`) | `remote-dev read` | 分页读取远端文件（支持负数 `offset` 尾部读取与可选乐观并发校验） |
| `Write` | `remote.write` (`remote_write`) | `remote-dev write` | 原子写入或追加（`append=true`）远端文件 |
| `Edit` | `remote.edit` (`remote_edit`) | `remote-dev edit` | 单点精确替换远端文件内容 |
| `MultiEdit` | `remote.multi_edit` (`remote_multi_edit`) | `remote-dev multi-edit` | 批量原子替换远端文件多处内容 |
| `Bash` | `remote.bash` (`remote_bash`) | `remote-dev bash` | 执行远端命令，支持 `yield_time_ms` 自动转为可持续交互会话与 `tty=true` PTY |
| `Glob` | `remote.glob` (`remote_glob`) | `remote-dev glob` | 远端文件名模式匹配 |
| `Grep` | `remote.grep` (`remote_grep`) | `remote-dev grep` | 远端内容检索（支持 `rg` 与 `grep` 回退、上下文行与计数模式） |
| `LS` | `remote.ls` (`remote_ls`) | `remote-dev ls` | 列出远端目录内容 |
| `apply_patch` | `remote.apply_patch` (`remote_apply_patch`) | `remote-dev apply-patch` | 原子应用 Codex 补丁或 Unified Diff |
| `write_stdin` | `remote.job_stdin` (`remote_job_stdin`) | `remote-dev job-stdin` | 向运行中的远端会话写入标准输入或轮询增量输出 |
| *(后台作业管理)* | `remote.job_status` / `job_tail` / `job_stop` | `remote-dev job-status` / `job-tail` / `job-stop` | 查询后台作业状态、增量日志或终止受管进程族 |
| *(产物传输)* | `remote.artifact_manifest` / `artifact_pull` / `artifact_push` | `remote-dev artifact-manifest` / `artifact-pull` / `artifact-push` | 基于 1 MiB 分块流与 SHA-256 校验的目录树/文件同步 |
| *(端点探测)* | `remote.probe` / `remote.context_snapshot` | `remote-dev probe` / `context-snapshot` | 探测远端 OS、Python、工作目录及连接阶段耗时诊断 |

---

## 快速开始

### 环境要求

- **本地客户端**：Python `>= 3.11`、本地 `ssh` 客户端（原生 Windows 需安装 Git for Windows 并使用其内置 `usr/bin/ssh.exe`）、以及随包固定的 `mindie-diagnostics`。无需本地 GPU/NPU。
- **远端目标**：标准 Linux 主机（支持 SSH 访问，具备 `bash`、`python3`，以及用于 Unified Diff 补丁的 `git`）。

### 1. 安装与启动 MCP Server

直接从 Git 引用启动（无需克隆仓库）：

```bash
uvx --from git+https://github.com/mindie-agent/remote-dev@main remote-dev server
```

从本地克隆安装：

```bash
uv pip install -e .
remote-dev server
```

### 2. 客户端 MCP 配置示例

在 Claude Code / Cursor 的 `.mcp.json` 中注册（Kimi Code 使用 `.kimi-code/mcp.json`，Codex 使用 `.codex/config.toml`，详见 [`examples/`](./examples/) 与 [客户端兼容性说明](./docs/client-compatibility.zh.md)）：

```json
{
  "mcpServers": {
    "remote-dev": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mindie-agent/remote-dev@main",
        "remote-dev",
        "server"
      ],
      "env": {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_DEFAULT_ROOT": "/",
        "REMOTE_DEV_DEFAULT_CWD": "/"
      }
    }
  }
}
```

### 3. CLI 与已有容器操作示例

```bash
# 探测远端连接与各阶段耗时诊断
remote-dev probe --host lab-host --port 22 --diagnose-connection

# 直接在远端主机已有的 Docker 容器内执行命令
remote-dev bash --host lab-host --port 22 --container repro-case --cwd /work/vllm --command "pytest -q"

# 运行本地单元测试与本地静态校验
uv pip install -e ".[test]"
python -m pytest
remote-dev validate --local-only
```

---

## 显式端点契约与配置

`remote-dev` 仅从显式字段解析目标端点，绝不从本地目录猜测远端目标：

1. 直接指定 `host` + `port`（可选附加 `container`、`user`、`root`、`cwd`、`runtime_env_file`、`identity_file`）。
2. 指定 `alias`，从 `REMOTE_DEV_ENDPOINTS_FILE` 或当前目录下的 `endpoints.json` / `endpoints.local.json` 解析。
3. 由上层消费方通过 `REMOTE_DEV_RESOLVERS` 注册的解析器插件解析自定义选择器（如 `--selector lab=gpu-1`）。

常用环境变量：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REMOTE_DEV_DEFAULT_USER` | `root` | 默认 SSH 用户名 |
| `REMOTE_DEV_DEFAULT_ROOT` | `/` | 默认路径策略根目录 |
| `REMOTE_DEV_DEFAULT_CWD` | 与 `root` 相同 | 默认远端工作目录 |
| `REMOTE_DEV_RUNTIME_ENV_FILE` | 未设置 | 命令执行前自动 `source` 的远端环境脚本 |
| `REMOTE_DEV_RESOLVERS` | 未设置 | 逗号分隔的解析器插件入口（`module:callable` 或 `/path/file.py:callable`） |
| `REMOTE_DEV_ENDPOINTS_FILE` | 未设置 | 端点别名文件路径列表（按 `os.pathsep` 分隔） |
| `REMOTE_DEV_STATE_DIR` | `<cwd>/state` | 本地作业记录、读取账本与产物清单存储目录 |

---

## 文档导航

| 文档 | 中文 | English |
| --- | --- | --- |
| 文档中心索引 | [docs/README.md](./docs/README.md) | [docs/README.en.md](./docs/README.en.md) |
| 架构与分层设计 | [docs/design.zh.md](./docs/design.zh.md) | [DESIGN.md](./DESIGN.md) |
| 客户端兼容性与参数别名 | [docs/client-compatibility.zh.md](./docs/client-compatibility.zh.md) | [CLIENT_COMPATIBILITY.md](./CLIENT_COMPATIBILITY.md) |
| 传输层、容器执行与诊断参考 | [docs/transport-and-diagnostics.md](./docs/transport-and-diagnostics.md) | [docs/transport-and-diagnostics.en.md](./docs/transport-and-diagnostics.en.md) |

---

## 仓库目录结构

```text
remote-dev/
├── remote_dev/              # 核心 Python 包（core、processes、mcp、hooks、tools、schemas）
├── examples/                # 各客户端 MCP 配置示例、别名文件模板与 Resolver 插件示例
├── docs/                    # 中英双语架构、客户端兼容性与传输诊断文档
├── tests/                   # 单元测试套件（Mock 传输，无需真实 SSH/NPU）
├── DESIGN.md                # 英文架构设计文档
└── CLIENT_COMPATIBILITY.md  # 英文客户端兼容性文档
```

---

## 许可证

本项目基于 [MIT License](./LICENSE) 开源。
