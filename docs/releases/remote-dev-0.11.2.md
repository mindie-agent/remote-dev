# remote-dev 0.11.2

[English](remote-dev-0.11.2.en.md)

新增命令层危险操作拦截与人工确认机制（`command_policy.py`）：
- 在 `remote.bash`、`start_remote_job`、`remote.job_stdin` 及客户端 `PreToolUse` Hook（`claude_remote_guard`、`codex_remote_guard`）入口拦截三类宿主机/设备级高危命令：
  1. **NPU 复位 (`npu_reset`)**：`npu-smi set -t reset ...`（放行只读 `npu-smi info`）。
  2. **宿主机关机 (`shutdown`)**：`shutdown`（不含 `-c` 取消）、`poweroff`、`halt`、`init 0`、`telinit 0`、`systemctl poweroff|halt|shutdown`、`ipmitool ... power off|soft`。
  3. **宿主机重启 (`reboot`)**：`reboot`、`shutdown -r`、`init 6`、`telinit 6`、`systemctl reboot|kexec|soft-reboot`、`ipmitool ... power reset|cycle`。
- 容器级生命周期操作（如 `docker restart/stop`、`podman restart/stop`）与普通服务重启（`systemctl restart <service>`）正常放行。
- 命中拦截时在远端派发前直接返回 `outcome="blocked"`、`status="confirmation_required"`（`submission_state="not_sent"`），提示先获取用户显式人工确认；用户确认后可通过 `confirm=true`、CLI `--confirm` 或 `REMOTE_DEV_CONFIRM_DANGEROUS=1` 放行执行。
