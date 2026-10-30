# remote-dev 0.11.2

[中文](remote-dev-0.11.2.md)

Add command-level safety interception and explicit human confirmation for destructive host and NPU operations (`command_policy.py`):
- Intercept three categories of destructive host/device commands across `remote.bash`, `start_remote_job`, `remote.job_stdin`, and client `PreToolUse` hooks (`claude_remote_guard`, `codex_remote_guard`):
  1. **NPU reset (`npu_reset`)**: `npu-smi set -t reset ...` (read-only `npu-smi info` remains unblocked).
  2. **Host shutdown (`shutdown`)**: `shutdown` (excluding `-c`), `poweroff`, `halt`, `init 0`, `telinit 0`, `systemctl poweroff|halt|shutdown`, and `ipmitool ... power off|soft`.
  3. **Host reboot (`reboot`)**: `reboot`, `shutdown -r`, `init 6`, `telinit 6`, `systemctl reboot|kexec|soft-reboot`, and `ipmitool ... power reset|cycle`.
- Container lifecycle commands (`docker restart/stop`, `podman restart/stop`) and ordinary service restarts (`systemctl restart <service>`) are not intercepted.
- Unconfirmed dangerous commands are blocked before dispatch (`outcome="blocked"`, `status="confirmation_required"`, `submission_state="not_sent"`) and require explicit human approval before retrying with `confirm=true`, CLI `--confirm`, or `REMOTE_DEV_CONFIRM_DANGEROUS=1`.
