# remote-dev 0.11.6

[English](remote-dev-0.11.6.en.md)

本补丁修复用户已提供 SSH 密码、公共 MCP 接口却无法接收密码而阻断首次连接的问题。

- `remote_auth(action="setup")` 接收仅用于当次认证的 `password`，复用共享 SSH 密钥生成、幂等公钥安装和新进程密钥验证。密码不写入配置、日志、回执或持久环境。
- 已验证的 SSH 操作不会被后续本地配置或回执写入失败反杀；返回明确的 `receipt_warning` / `receipt_unconfirmed`，保留已经验证的直接密钥连接信息。
- MCP 结果统一区分成功、失败和外部写入结果未知；未知写入需先对账，不引导盲目重试。
- Windows PowerShell、Windows PowerShell 5 与 WSL 的真实 SSH 验收覆盖公共 MCP 密码入口、错误密码、幂等安装、明文凭据不落盘及本地记账失败后的新进程连接；不包含用户业务服务器或模型部署验收。

源代码、跨平台合同测试和精确标签的发布基准分别由 CI 与发布流水线验证。
