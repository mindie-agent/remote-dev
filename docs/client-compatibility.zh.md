# `remote-dev` 客户端兼容性与参数归一化

**中文** · [English](../CLIENT_COMPATIBILITY.md) · [返回文档中心](./README.md)

`remote-dev` 使用单一 MCP Server 与统一的 Schema 集合服务所有支持 MCP 的 Coding Agent。不同客户端的参数命名习惯由共享的参数归一化层（`remote_dev.mcp.schemas.normalize_arguments`）统一处理，同时作用于 MCP 调度器与 CLI `--input-json` 入口。

---

## 客户端配置矩阵

在消费方项目中按客户端注册一次 MCP Server 即可。配置模板位于 [`examples/`](../examples/)：

| 客户端 | 项目级配置文件路径 | 示例模板 | Server 标识 |
| --- | --- | --- | --- |
| Kimi Code | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](../examples/kimi-mcp.example.json) | `remote-dev` |
| Claude Code | `.mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| Cursor IDE / Cursor Agent | `.cursor/mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| OpenAI Codex | `.codex/config.toml` | [`examples/codex-config.example.toml`](../examples/codex-config.example.toml) | `remote_dev` |
| Grok Build | `.grok/config.toml` | [`examples/grok-config.example.toml`](../examples/grok-config.example.toml) | `remote-dev` |

### 各客户端配置要点

- **Kimi Code**：从 `.kimi-code/mcp.json`（用户级为 `~/.kimi-code/mcp.json`）读取 MCP Server，配置项使用 `startupTimeoutMs` / `toolTimeoutMs` 且不包含 `type` 字段。信任项目目录后生效；新增或修改配置后需开启新会话。
- **Cursor IDE / Cursor Agent**：在 **Customize > MCPs > remote-dev** 中启用仓库 MCP 源并开启新会话。使用 CLI `cursor-agent` 时需先执行 `cursor-agent mcp enable remote-dev`。
- **Grok Build**：建议使用原生 `.grok/config.toml`，并通过 `grok inspect --json` 与 `grok mcp doctor remote-dev --json` 校验注册状态。
- **OpenAI Codex**：使用 `.codex/config.toml` 并配置 `approval_policy = "on-request"` 以支持交互式审批。对于经明确授权的无头 `codex exec` 任务，仅在单次命令行传入工具级审批，切勿在共享配置中写入全局免批规则：
  ```sh
  codex exec \
    -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
    -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
    '<显式授权且限定远端 root 的任务描述>'
  ```

---

## 共享兼容性契约

- **跨客户端可移植工具命名**：工具发现阶段统一通告下划线名称（如 `remote_read`、`remote_apply_patch`），确保各客户端均能完整注册全部 19 个工具。`tools/call` 同时接受下划线名称（`remote_read`）与标准点号名称（`remote.read`），返回信封保持标准点号名称。
- **扁平 JSON Schema**：所有工具输入 Schema 均为标准扁平对象，不使用顶层 `anyOf`、`oneOf`、`allOf` 或 `$ref`，避免被严格的模型网关（如 Kimi Schema 校验器）提前拒绝。
- **服务端端点与别名校验**：调用时需提供 `host` + `port`、`alias` 或已注册 Resolver 声明的选择器字段。支持客户端别名的必填字段（如 `file_path`、`command`）在服务端归一化后校验，不放入传输层 `required` 列表，使仅传别名的调用也能顺利通过客户端前置校验。
- **客户端原生参数别名映射**：
  - `path` → `file_path`（`read`、`write`、`edit`、`multi_edit`）
  - `line_offset` / `n_lines` → `offset` / `limit`（`read`；负数 `offset` 表示从文件末尾倒数读取）
  - `cmd` / `workdir` → `command` / `cwd`（`bash`）
  - `session_id` → `job_id`（`job_stdin`、`job_status`、`job_tail`、`job_stop`）
  - `-i`、`-A`、`-B`、`-C`、`-n`、`head_limit`、`offset`、`include_ignored`（`grep`），同时支持 `count`（每个文件的匹配行数）与 `count_matches`（每个文件的匹配总次数）
- **原子追加与二进制文件保护**：
  - `remote_write` 支持 `append=true`（与 `overwrite` 互斥）。
  - `remote_read` 自动检测二进制文件（前 8192 字节包含 NUL 字节）并返回可操作的 `binary_file` 状态。
- **交互式会话与增量输出游标**：
  - `remote_bash` 默认等待 `yield_time_ms`（默认 `10000` 毫秒）；若进程仍在运行或仍有未读输出，则返回 `session_id`。
  - `remote_job_stdin` 支持写入标准输入（`chars`），或当 `chars` 为空时轮询增量输出；部分写入会精确返回 `written_chars` 并推迟 `eof=true` 直至全部字符写入完毕。
  - 输出字节游标由跨进程锁保护，分页边界绝不截断多字节 UTF-8 字符。
