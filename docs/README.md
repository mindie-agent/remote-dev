# `remote-dev` 文档中心

**中文** · [English](./README.en.md) · [返回项目主页](../README.md)

这里汇总了 `remote-dev` 的架构原理、各大 AI 客户端的接入指南，以及容器直通、SSH 连接池和故障诊断的详细说明。

## 文档导航

| 文档主题 | 中文 | English | 你能在这里查到什么 |
| --- | --- | --- | --- |
| 整体架构与分层设计 | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | 为什么分成工具层、底座层和上层业务？容器直通和并发通道是怎么工作的？ |
| 各大客户端接入与参数兼容 | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | Claude Code、Codex、Kimi Code、Cursor、Grok Build 的配置方法，以及不同客户端的参数名自动转换规则 |
| 容器直通、SSH 连接池与诊断参考 | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | 怎么免装 SSH 直接操作远端 Docker 容器、怎么写自定义查机器插件、长连接隧道怎么防断连、遇到网络慢怎么测速排障 |
