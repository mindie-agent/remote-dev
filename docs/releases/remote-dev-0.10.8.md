# remote-dev 0.10.8

本版本修复远端参数要求不易发现、缺参错误无法指导 Agent 纠正的问题。直接指定 SSH 主机时，`host` 与 `port` 必须同时提供；这一要求现在同时出现在实际工具说明和参数说明中，覆盖 canonical 与各原生 Harness 工具目录。

`port: 22` 是常见示例，不是新增默认值。配置的别名和 resolver 仍可提供连接信息；使用已配置的默认 endpoint 时应省略 endpoint selectors。任务控制继续使用各宿主自己的 `job_id`、`session_id` 或 `task_id`，不额外强制传入主机端口。

已知的本地缺参错误返回 `category=caller`、`submission_state=not_sent`、`retryable=false`，并携带有界的 `input_repair`。例如只传 `host` 时，`input_repair.missing` 为 `["port"]`，`action` 说明如何补齐连接参数。缺少命令等操作参数沿用相同合同。仅在确认尚未提交的调用上提供纠正建议；连接中断、远端结果不确定和已成功操作的回执语义保持不变，不自动重放调用。

该问题在 remote-dev 0.10.6 和 0.10.7 中均能复现；它不是本版本新增的端口要求。旧 Codex 适配器只展示通用 `caller/not_sent`，需要配套的新版宿主适配器把共享纠正信息展示给模型。DSH 直接使用共享 MCP；Codex、Claude Code、Kimi 的配套插件分别验证错误投影。

验收覆盖真实 endpoint 解析、canonical/native 必填参数、MCP 错误数据、别名和默认 endpoint，以及不确定结果不附带重试建议。macOS 上的实际 Codex CLI `0.164.0-alpha.11.1` 与模型已通过候选代码验收：首次漏传端口被拒绝，Agent 根据错误自行补上 `port: 22`，真实 A3 SSH 只读命令返回退出码 0 和预期输出。此项为候选 MCP 代码验收，不代表所有宿主的原生插件安装或 NPU 计算验收。

运行时继续固定已正式发布的 mindie-diagnostics 0.5.4（`5ae643e19223f2181ecdbaa3edaf464137c4805e`）。发布流水线校验精确 Tag 源码、完整本地合同测试和微基准，并随包交付报告及校验和。
