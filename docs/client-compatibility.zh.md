# 各大 AI 客户端配置与参数兼容指南

**中文** · [English](../CLIENT_COMPATIBILITY.md) · [返回文档中心](./README.md)

不同的 AI 编程助手（Claude Code、Codex、Kimi Code、Cursor、Grok Build）在调工具时各有各的“口音”——比如有的喜欢传 `path`，有的喜欢传 `file_path`；有的传 `cmd`，有的传 `command`；有的对 JSON Schema 格式挑剔得很。

`remote-dev` 在服务端内置了一层**智能参数兼容层**（`remote_dev.mcp.schemas.normalize_arguments`）。你只需要启动同一个 `remote-dev server`，就能通吃所有主流客户端，不管是走 MCP 还是走 CLI `--input-json` 都能自动识别。

---

## 一表看懂各客户端配置文件位置

在你的项目目录里按所用客户端添加一次配置即可（现成模板都在 [`examples/`](../examples/) 目录下）：

| 客户端 | 配置文件路径 | 现成模板文件 | MCP Server 名称 |
| --- | --- | --- | --- |
| **Claude Code** | `.mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Cursor IDE / Agent** | `.cursor/mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Kimi Code** | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](../examples/kimi-mcp.example.json) | `remote-dev` |
| **OpenAI Codex** | `.codex/config.toml` | [`examples/codex-config.example.toml`](../examples/codex-config.example.toml) | `remote_dev` |
| **Grok Build** | `.grok/config.toml` | [`examples/grok-config.example.toml`](../examples/grok-config.example.toml) | `remote-dev` |

### 各客户端避坑小贴士

- **Kimi Code**：读取项目下的 `.kimi-code/mcp.json`（或全局 `~/.kimi-code/mcp.json`）。注意它的配置里用 `startupTimeoutMs` / `toolTimeoutMs`，**不要写 `"type": "stdio"` 字段**。修改配置后开启新对话即可生效。
- **Cursor IDE / Cursor Agent**：在设置 **Customize > MCPs > remote-dev** 里勾选启用，然后开一个新会话。如果用命令行版 `cursor-agent`，先跑一句 `cursor-agent mcp enable remote-dev`。
- **Grok Build**：推荐直接写原生的 `.grok/config.toml`，配好后可以用 `grok inspect --json` 和 `grok mcp doctor remote-dev --json` 检查是否连通。
- **OpenAI Codex**：在 `.codex/config.toml` 中设置 `approval_policy = "on-request"` 即可正常弹出确认框。如果你用无头模式 `codex exec` 跑自动化任务，建议只在当次命令行里临时授权所需工具，不要把全局免审批写死在共享配置文件里：
  ```sh
  codex exec \
    -c 'mcp_servers.remote_dev.tools.remote_apply_patch.approval_mode="approve"' \
    -c 'mcp_servers.remote_dev.tools.remote_read.approval_mode="approve"' \
    '<明确限定了远端目录范围的任务描述>'
  ```

---

## `remote-dev` 在背后帮你做了哪些兼容处理？

1. **工具名下划线与点号都认**：
   向客户端注册工具时统一用下划线名（如 `remote_read`、`remote_apply_patch`），防止部分客户端不认带点号的名字；而在实际调用时，不管传 `remote_read` 还是 `remote.read` 都能正常执行。
2. **极简扁平的 JSON Schema**：
   所有工具的参数 Schema 都是规规矩矩的扁平对象，不搞顶层 `anyOf`、`oneOf`、`allOf` 或 `$ref` 嵌套，哪怕面对最严格的模型网关校验（如 Kimi 的 Schema 校验器）也绝不报错。
3. **自动听懂不同客户端的“习惯参数名”**：
   - 传 `path` 自动转成 `file_path`（适用于 `read`、`write`、`edit`、`multi_edit`）
   - 传 `line_offset` / `n_lines` 自动转成 `offset` / `limit`（`read` 支持负数 `offset`，比如 `-50` 就是看文件最后 50 行）
   - 传 `cmd` / `workdir` 自动转成 `command` / `cwd`（适用于 `bash`）
   - 传 `session_id` 自动转成 `job_id`（适用于 `job_stdin`、`job_status`、`job_tail`、`job_stop`）
   - `grep` 原生支持 `-i`（忽略大小写）、`-A`/`-B`/`-C`（上下文行）、`-n`、`head_limit`、`offset`、`include_ignored`，以及 `count`（匹配行数）和 `count_matches`（匹配总次数）
4. **贴心的文件安全保护**：
   - `remote_write` 支持 `append=true` 在文件末尾安全追加（与 `overwrite` 互斥）。
   - `remote_read` 遇到二进制文件（前 8192 字节里有 NUL 字节）会主动停下并提示 `binary_file`，不会把乱码塞爆模型的上下文窗口。
5. **长命令自动转后台会话**：
   - `remote_bash` 默认先等 10 秒（`yield_time_ms=10000`）。如果命令还在跑或者还有输出没读完，会自动返回一个 `session_id`。
   - 随后可以用 `remote_job_stdin` 往里输入字符（`chars`），或者传空字符串继续拉取新输出，分页时绝不会把半个 UTF-8 汉字截断。
