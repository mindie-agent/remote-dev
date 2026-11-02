# remote-dev 0.11.4

[English](remote-dev-0.11.4.en.md)

## Windows 跨平台增量日志偏移测试与 WorkBuddy 契约对齐

- 在 `tests/test_harness_profiles.py` 的 `remote_TaskOutput` 增量日志字节偏移测试中改用原始 `write_bytes` 写入 UTF-8 字节流，消除 Windows CRLF 换行转换导致的游标偏移差异，确保腾讯 WorkBuddy（`wb`）13 项原生工具镜像在 macOS、Linux 与原生 Windows 下的一致性验证。
