# remote-dev 0.11.3

[English](remote-dev-0.11.3.en.md)

新增腾讯 WorkBuddy（`wb` / `workbuddy`）原生远端开发工具镜像支持（`profiles_wb.py`、`native_handlers_wb.py`）：
- 在 `NATIVE_PROFILES` 中新增 `"wb"`（支持 `"workbuddy"` 别名），除 20 项标准 `remote_*` 工具外额外暴露 13 项与 WorkBuddy 内置引擎（`@genie/agent-cli`）参数契约严格对齐的原生工具镜像：`remote_Read`、`remote_Write`、`remote_Edit`、`remote_MultiEdit`、`remote_Bash`、`remote_Glob`、`remote_Grep`、`remote_TaskOutput`、`remote_BashOutput`、`remote_NotebookEdit`、`remote_TaskStop`、`remote_KillShell`、`remote_ViewImage`。
- `remote_TaskOutput` 与 `remote_BashOutput` 基于原始日志文件字节偏移实现增量读取与 `full=true` 全量快照切换，支持 `filter` 不区分大小写正则行过滤，并在游标持久化异常时通过 `recording_failure` 诚实暴露 `local_recording_failed`。
- `remote_ViewImage` 支持 `image_path` 远端图片读取与 `query` 问题上下文附加；`remote_MultiEdit` 支持 `minItems: 1` 数组契约校验。
