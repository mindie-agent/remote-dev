# remote-dev 0.11.7

[English](remote-dev-0.11.7.en.md)

本版本让从任意目录发起的源码开发任务能够找到实际业务 Python 与 editable 源码，并在自动探测不适用时继续完成任务。

- `remote_probe` 支持显式 `python_executable`、补充 `python_candidates` 和 `required_distributions`。有限发现依赖齐备的活动环境或唯一候选；多个合格环境返回歧义，显式指定失败不换环境。
- 无框架导入的元数据探测返回分发版本、安装与 editable 源码位置、Python prefix 和 ABI，保留不同 venv 的身份。元数据存在不代表原生加载或 NPU 可运行性已经验证。
- 共享 MCP 工具说明明确源码、构建输出与解释器分别选择；尊重用户指定的新目录，未指定时沿用现有检出，不依赖用户进入 MindIE Agent 仓库。
- 失败结果保留端点、候选和原始错误，通过 `next` 提供显式解释器、CLI、原生 SSH 或任务脚本的继续入口；未知外部写仍需先对账。
- 探针与 readiness 传输复用维护在独立文件中的 worker，并压缩传输以满足 Windows SSH 命令长度边界。

本版本通过组件正式 Release 独立发现，无需更新产品 channel。完整跨平台合同、真实隔离 OpenSSH 和 wheel 安装由 CI 验证；精确 Tag 的测试与 benchmark 由发布工作流执行。A2/A3 SSH 不可达，实际原生重编译、动态库加载与 NPU Canary 尚未验收。
