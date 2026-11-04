# remote-dev 0.11.5

[中文](remote-dev-0.11.5.md)

## Windows Zero Console Popup Hardening for Local Media Probes

- Explicitly passes `subprocess.CREATE_NO_WINDOW` to local `ffprobe` subprocess calls in `remote_dev/core/media_core.py` and SSH test fixtures in `tests/conftest.py`, matching `local_process.py` so Windows GUI hosts never flash console windows.
