# remote-dev 0.11.3

[中文](remote-dev-0.11.3.md)

Add native Tencent WorkBuddy (`wb` / `workbuddy`) remote development tool profile (`profiles_wb.py`, `native_handlers_wb.py`):
- Register `"wb"` (with `"workbuddy"` alias) in `NATIVE_PROFILES`, exposing 13 native WorkBuddy tool mirrors alongside the 20 canonical `remote_*` tools: `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_BashOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_KillShell`, and `remote_ViewImage`.
- `remote_TaskOutput` and `remote_BashOutput` track raw log byte offsets for incremental output reads vs `full=true` snapshots, support case-insensitive regex line filtering via `filter`, and surface `local_recording_failed` via `recording_failure` if local cursor persistence fails.
- `remote_ViewImage` supports `image_path` and `query` metadata; `remote_MultiEdit` enforces `minItems: 1` array validation.
