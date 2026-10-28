# remote-dev 0.11.1

[English](remote-dev-0.11.1.en.md)

修复原生 Windows 进程继承 Git Bash 的 PATH 时找不到 Git 自带 OpenSSH 的问题。现在同时识别 `Git/ucrt64/bin/git.exe`、`Git/usr/bin/git.exe` 和此前支持的目录，仍然使用同一安装内的 `usr/bin/ssh.exe`。npu-top 的 Windows CI 实际发现 `C:\Program Files\Git\ucrt64\bin\git.exe`，因此此次修复针对真实环境失败。

本版本保留 0.11.0 的统一认证行为：remote-dev 管理密码与公钥，coordinator 和 npu-top 复用；自动装公钥、password-only、失效公钥修复都不调用系统凭据库，不要求系统密码。业务提交前完成认证修复，已提交业务不重放。只读监控可使用已存密码，不修改远端公钥。

Git 目录选择合同覆盖新增布局。跨平台真实 SSH 工作流记录实际 Git 与 SSH 路径，并在 PowerShell 中分别验证普通 PATH 与 Git Bash 布局 PATH 下的四条认证路径；WSL2 验证原生 Linux OpenSSH。完整验收结果与实测提交记录在配套产品 0.1.2 的验收附件中，正式发布工作流从该标签的干净源码生成 benchmark 报告。
