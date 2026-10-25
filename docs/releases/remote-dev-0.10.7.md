# remote-dev 0.10.7

本版本固定 mindie-diagnostics 0.5.4（`5ae643e`）：recorder 在 reader-registry/游标协调之前以 `O_EXCL` 分配首个/空闲日志槽，修复旧实现下约 101ms setup 即触发的 CI 间歇失败；完整窗口读取与压力语义不变。100ms 回收扫描预算与 3s SQLite 限制仍在，本版本不声称异步优化。

本版本保留大日志的模型可见头尾摘要：native 与 canonical 的普通命令/任务输出给出有界 UTF-8 头尾、明确省略说明，以及可读取的完整远端原始日志与真实本地缓存状态引用。后台/增量页仍按预算返回完整页，游标不越过未返回字节。

终端一次性命令只读取有界真实远端头尾；预览读取受一个小的共享 deadline 约束。预览失败不会把已完成的命令变成 MCP 传输错误或重放，结果保留真实 exit，并显式标记 `preview_degraded`。重定向日志很大时仍应通过 `remote.read` 读取原始日志。

已验证 canonical 在 canonical/Codex/Claude/Kimi profile 以及四类 native 前台/后台路径；CI 覆盖 Ubuntu 3.11/3.12、macOS、Windows。正式 tag/release 由发布流程另行生成。
