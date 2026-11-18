# remote-dev 0.11.14：7-Harness 1:1 原生工具面与零摩擦 SSH 机器接入

[English](remote-dev-0.11.14.en.md)

`remote-dev v0.11.14` 完成 Harness 工具面的干净收敛，并补齐 Antigravity / OpenCode 两个官方宿主：

- **严格 1:1 每宿主工具面（`tools/list` 与 `tools/call` 对齐）**：绑定 `REMOTE_DEV_HARNESS_PROFILE` 后只暴露该宿主的原生镜像工具与最小的 `remote.auth` / `remote.probe` / `remote.artifact_pull` / `remote.artifact_push` 远程控制工具；不再混入跨宿主 canonical 别名，`remote_BashOutput` / `remote_KillShell` 等重复历史别名从 Claude Code 与 WorkBuddy 面退役（`TaskOutput` / `TaskStop` 为唯一作业入口）。未绑定 Profile 时仍提供 20 个可移植 `remote_*` canonical 工具。
- **新增 `agy`（Google Antigravity）与 `oc`（OpenCode）原生档案**：分别镜像 `read_file` / `write_file` / `edit_file` / `run_command` / `manage_task` / `find_by_name` / `grep_search` / `list_dir` 与 `read` / `write` / `edit` / `bash` / `glob` / `grep` / `list` 的字段、默认值与命名习惯；`antigravity`、`opencode`、`cc`、`workbuddy` 继续作为 Profile 别名。
- **零摩擦 SSH 机器接入**：`remote_auth` 与 `remote-dev auth setup|status|forget` 在直接提供 `host` 时默认 SSH 端口 `22`（其他工具仍要求显式 `host+port` 或已配置 alias）；新增 `--password <pw>` 便于终端末尾粘贴密码，新增 `--save-default` / `--save-alias` 将**仅非敏感字段**（host/port/user/container/cwd/root/auth_ref）原子合并进 `endpoints.json`（`0o600` 文件、`0o700` 目录、`record_lock` 串行化），后续调用可零 `host`/`port` 解析。
- **回执诚实性与任务连续性**：SSH 已验证后若本地 endpoint 保存失败，保持 `outcome="success"` 与 `isError=False`，仅附加 `endpoint_save` / `receipt_warning` / `receipt_unconfirmed`；新增 `discover=True` 用一条 `<= 10s` 只读 SSH 命令收集 OS、Ascend NPU 与运行中 Docker 容器，超时或部分失败以 `discovery.status=unavailable|partial` 呈现而绝不回滚认证成功。传输错误提示明确允许在组件不可用时改用原生 `ssh` 或本地脚本继续任务。
