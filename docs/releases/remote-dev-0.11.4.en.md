# remote-dev 0.11.4

[中文](remote-dev-0.11.4.md)

## Windows Cross-Platform Incremental Log Offset Test & WorkBuddy Profile Alignment

- Use raw `write_bytes` in `tests/test_harness_profiles.py` when testing `remote_TaskOutput` incremental byte-offset cursors, preventing Windows CRLF newline translation from skewing byte offsets and ensuring clean cross-platform verification of the 13-tool Tencent WorkBuddy (`wb`) profile across macOS, Linux, and native Windows.
