# `remote-dev` 文档中心

**中文** · [English](./README.en.md) · [返回项目主页](../README.md)

本目录收录 `remote-dev` 的架构设计、多客户端兼容性契约以及底层传输与诊断技术参考。

## 文档列表

| 文档 | 中文 | English | 内容概述 |
| --- | --- | --- | --- |
| 架构与分层设计 | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | 三层边界（工具面、共享底座、消费方）、依赖方向、容器端点与 MCP 资源模型 |
| 客户端兼容性与参数层 | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | Claude Code、Codex、Kimi Code、Cursor、Grok Build 的 MCP 配置与参数别名归一化规则 |
| 传输层、容器与诊断参考 | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | 容器执行语义、Resolver 插件接口、SSH 连接池与流原语、受管进程监督及结构化诊断 |
