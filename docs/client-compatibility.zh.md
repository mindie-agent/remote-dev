# 各大 AI 客户端配置与参数兼容指南

**中文** · [English](../CLIENT_COMPATIBILITY.md) · [返回文档中心](./README.md)

不同的 AI 编程 Harness（**OpenAI Codex**、**Claude Code**、**Cursor**、**Kimi Code**、**DeepSeek Harness / DSH**、**Google Antigravity** 以及 **Grok Build**）在调用本地工具时各有各的原生工具名、参数命名和行为习惯——比如有的传 `path` 或 `AbsolutePath` / `TargetFile`，有的用 `StartLine` / `EndLine` 闭区间切片，有的支持按行号范围 `[StartLine, EndLine]` 局部替换，有的支持按 AST 符号大纲（`view_file_outline` / `view_code_item`）查看代码。

`remote-dev` 在服务端内置了统一的**六大 Harness 原生工具与参数兼容层**（`remote_dev.mcp.schemas.normalize_arguments` + `HARNESS_TOOL_ALIASES`）。只需要启动同一个 `remote-dev server`，就能让所有 Harness 的调用方式和返回语义与本地工具完全一致。

---

## 六大 Harness 原生工具与参数全量对照表

| 能力维度 | `remote-dev` 标准工具 | 兼容的 Harness 工具别名 | 六大 Harness 原生参数与行为全兼容支持 |
| --- | --- | --- | --- |
| **文件读取 / 行区间切片 / 符号大纲 / 代码项提取** | `remote.read` (`remote_read`) | `remote_view_file`、`remote_read_file`、`remote_view_file_outline`、`remote_view_code_item` | `file_path` / `path` / `AbsolutePath` / `TargetFile` / `target_file` / `File`；`offset` + `limit`（CC）、`line_offset` + `n_lines` 含负数尾部倒读（Kimi）、`StartLine` + `EndLine` 闭区间切片（Antigravity / Cursor / DSH）、`ContentOffset` 字节/字符偏移续读（Antigravity）、`should_read_entire_file`（Cursor）、`outline=true` + `ItemOffset` 类与函数大纲提取（Antigravity `view_file_outline`）、`NodePaths` / `symbol` 按符号提取定义（Antigravity `view_code_item`）。 |
| **文件写入 / 覆盖 / 末尾追加** | `remote.write` (`remote_write`) | `remote_write_to_file`、`remote_write_file` | `file_path` / `path` / `TargetFile` / `target_file` / `AbsolutePath`；`content` / `CodeContent` / `contents`；`overwrite` / `Overwrite`、`append` / `Append`、`mode="overwrite"\|"write"\|"append"`（Kimi），传 `TargetFile` 时自动开启 `create_dirs=true` 创建父目录（Antigravity），支持 `Description` / `explanation`。 |
| **单处精确替换（支持行区间限定）** | `remote.edit` (`remote_edit`) | `remote_replace_file_content`、`remote_str_replace_file` | `file_path` / `path` / `TargetFile` / `target_file`；`old_string` / `TargetContent` / `old_str`、`new_string` / `ReplacementContent` / `new_str`、`replace_all` / `AllowMultiple`；支持 `StartLine`（`start_line`）与 `EndLine`（`end_line`）将查找与替换限定在指定行号闭区间内，即使目标文本在文件其他位置重复出现也不会报错（Antigravity）。 |
| **多处原子替换（Multi-Chunk）** | `remote.multi_edit` (`remote_multi_edit`) | `remote_multi_replace_file_content` | `file_path` / `path` / `TargetFile`；`edits` / `ReplacementChunks`（每个分块均支持 `old_string`/`TargetContent`/`old_str`、`new_string`/`ReplacementContent`/`new_str`、`replace_all`/`AllowMultiple` 以及自动感知行号偏移量的 `StartLine`/`EndLine`）。 |
| **补丁原子应用** | `remote.apply_patch` (`remote_apply_patch`) | `remote.apply_patch` | `patch` 或 `command`，同时兼容 Codex `*** Begin Patch` 语法与标准 Git Unified Diff。 |
| **内容搜索（正则与字面量）** | `remote.grep` (`remote_grep`) | `remote_grep_search` | `pattern` / `Query` / `query`；`path` / `SearchPath` / `target_directory`；`fixed_strings` / `-F` / `literal` / `IsRegex=false`（字面量精确匹配，`rg -F` 与 `grep -F` 回退均支持）；`MatchPerLine`（`true` → `content`，`false` → `files_with_matches`）；`Includes` / `include_pattern` 与 `Excludes` / `exclude_pattern`；`-i` / `CaseInsensitive` / `case_sensitive`；`-A`、`-B`、`-C`、`-n`、`head_limit`、`offset`、`count`、`count_matches`。 |
| **文件名与路径查找** | `remote.glob` (`remote_glob`) | `remote_find_by_name`、`remote_file_search` | `pattern` / `Pattern` / `query`（传过滤条件时可省略，默认 `*`）；`path` / `SearchDirectory` / `target_directory`；`Extensions`（按扩展名过滤）、`Excludes` / `exclude_pattern`、`Type`（`file` / `directory` / `any`）、`MaxDepth` / `depth`、`FullPath` / `full_path`、`respect_gitignore`。 |
| **目录结构列出** | `remote.ls` (`remote_ls`) | `remote_list_dir` | `path` / `DirectoryPath` / `dir_path` / `target_directory`；支持 `MaxDepth` / `depth` / `recursive` 多层目录展开与 `Excludes` / `ignore_globs` 过滤。 |
| **命令执行与后台守护** | `remote.bash` (`remote_bash`) | `remote_exec_command`、`remote_run_command`、`remote_run_terminal_cmd`、`remote_shell` | `command` / `cmd` / `CommandLine`；`cwd` / `workdir` / `working_dir` / `Cwd`；`yield_time_ms` / `WaitMsBeforeAsync`；`run_in_background` / `is_background` / `IsDaemon`（立即以受管后台任务启动并返回 `job_id` / `session_id`）；`tty`、`max_output_tokens`、`env`。 |
| **交互式输入与后台任务管理** | `remote.job_status` / `job_stdin` / `job_tail` / `job_stop` | `remote_manage_task`、`remote_write_stdin`、`remote_bash_output`、`remote_task_output`、`remote_kill_shell`、`remote_task_stop` | `job_id` / `session_id` / `TaskId` / `task_id` / `bash_id` / `shell_id`；`chars` / `Input` / `input` / `stdin`；支持 `Action="list"\|"status"\|"kill"\|"stop"\|"send_input"\|"tail"`（完整对齐 Antigravity `manage_task`）。 |

---

## 一表看懂各客户端配置文件位置

在你的项目目录里按所用客户端添加一次配置即可（现成模板都在 [`examples/`](../examples/) 目录下）：

| 客户端 | 配置文件路径 | 现成模板文件 | MCP Server 名称 |
| --- | --- | --- | --- |
| **Claude Code** | `.mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Cursor IDE / Agent** | `.cursor/mcp.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Kimi Code** | `.kimi-code/mcp.json` | [`examples/kimi-mcp.example.json`](../examples/kimi-mcp.example.json) | `remote-dev` |
| **OpenAI Codex** | `.codex/config.toml` | [`examples/codex-config.example.toml`](../examples/codex-config.example.toml) | `remote_dev` |
| **DeepSeek Harness (DSH)** | `.mcp.json` / DSH MCP 配置 | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Google Antigravity** | `mcp_config.json` | [`examples/mcp.json`](../examples/mcp.json) | `remote-dev` |
| **Grok Build** | `.grok/config.toml` | [`examples/grok-config.example.toml`](../examples/grok-config.example.toml) | `remote-dev` |

### 各客户端避坑小贴士

- **Kimi Code**：读取项目下的 `.kimi-code/mcp.json`（或全局 `~/.kimi-code/mcp.json`）。注意它的配置里用 `startupTimeoutMs` / `toolTimeoutMs`，**不要写 `"type": "stdio"` 字段**。修改配置后开启新对话即可生效。
- **Cursor IDE / Cursor Agent**：在设置 **Customize > MCPs > remote-dev** 里勾选启用，然后开一个新会话。如果用命令行版 `cursor-agent`，先跑一句 `cursor-agent mcp enable remote-dev`。
- **Google Antigravity**：原生支持 `StartLine`/`EndLine`、`ContentOffset`、`outline=true`（`view_file_outline`）、`NodePaths`（`view_code_item`）、行区间限定替换（`replace_file_content` / `multi_replace_file_content`）、`IsRegex=false` 字面量搜索、`find_by_name` 组合过滤以及 `manage_task(Action=...)`。
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

1. **工具名下划线、点号与各 Harness 原生别名全兼容**：
   向客户端注册工具时统一用下划线名（如 `remote_read`、`remote_apply_patch`），防止部分客户端不认带点号的名字；而在实际调用时，不管传 `remote_read`、`remote.read`，还是传 `remote_view_file`、`remote_replace_file_content`、`remote_exec_command`、`remote_manage_task` 等别名都能正常执行。
2. **极简扁平的 JSON Schema**：
   所有工具的参数 Schema 都是规规矩矩的扁平对象，不搞顶层 `anyOf`、`oneOf`、`allOf` 或 `$ref` 嵌套，哪怕面对最严格的模型网关校验（如 Kimi 的 Schema 校验器）也绝不报错。
3. **与 `coordinator` 零配置联动解析目标机器**：
   配合 `mindie-coordinator`（设置 `REMOTE_DEV_RESOLVERS="mindie_coordinator.endpoint_bridge:setup_resolvers"`）使用时，`remote-dev` 会直接从 `coordinator` 的 `machines.json` / `endpoints.json` 自动解析 `<alias>`、`<alias>-host`、`<alias>-docker` 与默认机器 `default_alias`。
4. **贴心的文件安全保护与行区间局部替换**：
   - `remote_write` 支持 `append=true` / `mode="append"` 在文件末尾安全追加（与 `overwrite` 互斥）。
   - `remote_edit` 与 `remote_multi_edit` 支持 `start_line` / `end_line`（`StartLine` / `EndLine`），在指定行范围内消歧并完成替换。
   - `remote_read` 遇到二进制文件（前 8192 字节里有 NUL 字节）会主动停下并提示 `binary_file`，不会把乱码塞爆模型的上下文窗口。
5. **长命令自动转后台会话**：
   - `remote_bash` 默认先等 10 秒（`yield_time_ms=10000`；若传 `run_in_background=true` / `IsDaemon=true` 则 200ms 快速转后台）。如果命令还在跑或者还有输出没读完，会自动返回一个 `session_id`。
   - 随后可以用 `remote_job_stdin` 往里输入字符（`chars` / `Input`），或者传空字符串继续拉取新输出，分页时绝不会把半个 UTF-8 汉字截断。
