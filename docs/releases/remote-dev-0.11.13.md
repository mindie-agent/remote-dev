# remote-dev 0.11.13：跨会话后台作业句柄、长驻并发连接池、`64 KiB` 流式传输与安全防护收敛

[English](remote-dev-0.11.13.en.md)

`remote-dev v0.11.13` 完成控制面洁净架构收敛并升级锁定 `mindie-diagnostics v0.5.16`：

- **跨连接句柄寻址与零前置会话绑定门禁（`P2.7`）**：后台长作业统一通过稳定 `job_id` 跨 MCP 重连与跨会话直接查询、尾读与终止，无需任何前置会话 Hook 或 `_meta` 绑定；移除模块导入期隐式全局环境变量副作用。
- **长驻并发 MCP 分发、`64 KiB` 流式传输与符号链接/超时安全**：只读探测与状态查询走并发执行通道而不被慢速远端命令阻塞；大文件同步按 `64 KiB` 分块流式读写（控制面内存峰值 `<= 512 MiB`）；严格拦截越界符号链接并在所有 `OSError` 边界前显式重抛 `TimeoutError`（`Windows` 默认根路径不再误解析为盘符根）。
- **底座升级与 `control-plane` 门禁对齐**：升级 `mindie-diagnostics` 正式版引脚至 `v0.5.16`，对齐 `--profile control-plane` PR 设计原则门禁。
