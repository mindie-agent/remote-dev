# remote-dev 0.11.11：Windows PowerShell 5.1 / CP936 适配、WSL2 DrvFS/PE 遮蔽防护与跨平台真实 OpenSSH 验收

[English](remote-dev-0.11.11.en.md)

- **WSL2 DrvFS / PE `$PATH` 遮蔽防护与 Windows 长路径前缀剥离（`core/_host_exec.py`、`core/ssh_transport.py`、`auth/access.py`、`core/media_core.py`）**：在 POSIX / WSL2 下定位 `ssh`、`ssh-keygen` 及本地媒体工具时严格识别并跳过 `/mnt/c` 等 DrvFS 挂载及 `MZ..PE\0\0` Windows 可执行文件（若 `$PATH` 仅剩 Windows 垫片则显式快速失败），且通过 `realpath(..., strict=True)` 与 `os.stat` 全程透传 `TimeoutError`；在原生 Windows 下同时兼容用户级 `%LOCALAPPDATA%\Programs\Git` 与 `libexec\git-core` 布局并自动剥离 `< 260` 字符路径上的 `\\?\` 扩展前缀，避免 MSYS2/Git for Windows `ssh.exe`、`ssh-keygen.exe` 与 `git -C` 解析失败。
- **CP936 / UTF-8 编码一致性与 `TimeoutError` 契约加固（`processes/probe.py`、`processes/worker.py`、`core/media_notebook.py`、`core/media_video.py`、`mcp/native_handlers_claude.py`）**：消除隐式本地默认编码读写与 `except OSError` 吞没 `TimeoutError` 的隐患；同步升级 `mindie-diagnostics` 至正式版 `v0.5.12`。
- **真实跨平台 OpenSSH 验收矩阵升级（`.github/workflows/ssh-auth-platforms.yml`）**：补齐 `push: branches: [main]` 触发器与最新正式版组件基线，新增 **Windows PowerShell 5.1 (`powershell.exe`)** 四路真实 SSH 认证验收及 WSL2 `/mnt/c` DrvFS/PE 遮蔽场景端到端回归。
