# `remote-dev` 文档中心

**中文** · [English](./README.en.md) · [返回项目主页](../README.md)

这里汇总了 `remote-dev` 的架构原理、各大 AI 客户端的接入指南、容器直通与 SSH 连接池诊断参考，以及各版本发布说明。

## 文档导航

| 文档主题 | 中文 | English | 你能在这里查到什么 |
| --- | --- | --- | --- |
| 整体架构与分层设计 | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | 为什么分成工具层、底座层和上层业务？容器直通和并发通道是怎么工作的？ |
| 各大客户端接入与参数兼容 | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | Claude Code、Codex、Kimi Code、Cursor、Grok Build 的配置方法，以及不同客户端的参数名自动转换规则 |
| 容器直通、SSH 连接池与诊断参考 | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | 怎么免装 SSH 直接操作远端 Docker 容器、怎么写自定义查机器插件、长连接隧道怎么防断连、遇到网络慢怎么测速排障 |

## 版本发布记录

| 版本 | 中文 | English | 核心更新 |
| --- | --- | --- | --- |
| **`v0.11.15`（当前版本）** | [remote-dev-0.11.15.md](./releases/remote-dev-0.11.15.md) | [remote-dev-0.11.15.en.md](./releases/remote-dev-0.11.15.en.md) | 基于 `ContextVar` 的无锁请求级 `session_scope` 会话/状态/凭据隔离、`remote_dev.mcp.server` 导入期零环境变量污染、Windows `> 260` 字符超长路径兼容与流式传输清理加固 |
| `v0.11.14` | [remote-dev-0.11.14.md](./releases/remote-dev-0.11.14.md) | [remote-dev-0.11.14.en.md](./releases/remote-dev-0.11.14.en.md) | 7-Harness 严格 1:1 原生工具面收敛（新增 `agy` 与 `oc` 档案）与单步零摩擦 SSH 机器接入 |
| `v0.11.13` | [remote-dev-0.11.13.md](./releases/remote-dev-0.11.13.md) | [remote-dev-0.11.13.en.md](./releases/remote-dev-0.11.13.en.md) | 跨会话后台作业句柄、常驻并发连接池、`64 KiB` 流式传输与软链/超时安全收敛 |
| `v0.11.12` | [remote-dev-0.11.12.md](./releases/remote-dev-0.11.12.md) | [remote-dev-0.11.12.en.md](./releases/remote-dev-0.11.12.en.md) | 远端 NPU 作业环形日志上限与终态 GC、本地端点状态有界保留与测试临时目录修复 |
