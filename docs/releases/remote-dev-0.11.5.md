# remote-dev 0.11.5

[English](remote-dev-0.11.5.en.md)

## Windows 零控制台弹窗加固与本地媒体探针窗口隐藏

- 在 `remote_dev/core/media_core.py` 的本地 `ffprobe` 子进程调用及 `tests/conftest.py` 的 SSH 测试服务夹具中统一显式附加 `subprocess.CREATE_NO_WINDOW`，与 `local_process.py` 保持一致，消除 Windows GUI 宿主下的控制台窗口闪现。
