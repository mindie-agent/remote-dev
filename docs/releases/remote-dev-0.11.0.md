# remote-dev 0.11.0

[English](remote-dev-0.11.0.en.md)

本版本让 SSH 认证可跨 Agent 会话复用。首次提供密码后，remote-dev 验证登录并尝试安装、独立验证公钥；服务器不支持公钥或拒绝安装时，已验证的密码仍可用于后续连接。公钥被撤销后，普通开发操作会在提交业务命令前使用已保存密码修复认证，不重放已提交的业务操作。只读监控和诊断允许密码回退，但不修改远端公钥。

remote-dev 是唯一认证所有者。coordinator 和 npu-top 只保存 `auth_ref` 并调用同一实现；密码默认以明文保存在当前用户的 `~/.mindie/ssh/credentials.json`，POSIX 目录和文件分别使用 `0700`、`0600`。可通过 `REMOTE_DEV_AUTH_DIR` 和 `REMOTE_DEV_CREDENTIAL_FILE` 显式选择共享位置。同一操作系统、同一用户下的不同插件虚拟环境使用同一份配置，不调用系统钥匙串，不要求系统登录密码、解锁密码或额外主密码。Windows 原生与 WSL 的用户目录独立，不自动混用路径或凭据。

新增 `remote-dev auth setup/status/forget` 入口和共享认证 API。受控的 askpass 子进程按引用读取保存的密码，不把凭据文件中的密码复制到 SSH 参数或环境变量。原生 Windows 安装包提供 `remote-dev-askpass.exe`，继续使用 Git for Windows 内置 SSH；macOS 和 WSL 使用其 OpenSSH 客户端。认证准备、命令执行和子进程回收共用有界预算；主机指纹冲突、凭据不可用及结果不确定均明确返回。

升级保留已有 endpoint 和 OpenSSH 身份配置。需要可恢复的密码登录时，显式配置一次凭据即可；删除共享认证只删除本地配置，不撤销远端已安装公钥。运行时继续锁定 mindie-diagnostics 0.5.4（`5ae643e19223f2181ecdbaa3edaf464137c4805e`）。

验收状态：已完成 macOS 本地认证、子进程生命周期及消费者合同测试，并通过三组件全部固定 benchmark 工作负载的本地兼容性检查。在隔离的 `REMOTE_DEV_AUTH_DIR` 下，默认 `credentials.json` 已验证可由独立 Python 3.11 和 3.13 进程复用；未读取真实用户凭据。原生 Windows PowerShell 与真实 WSL 验收仍待完成；此候选说明不代表三平台均已通过。发布前须补齐对应平台的最终源码 SHA 与实测结果；正式 benchmark 报告由 Tag 流水线在干净源码上生成并随包交付。
