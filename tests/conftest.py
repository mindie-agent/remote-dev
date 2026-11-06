"""Never send intentional test failures to the developer's real diagnostic root."""
from pathlib import Path
import shutil
import pytest

_REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def isolated_diagnostics(tmp_path, monkeypatch, request):
    from remote_dev.core import ssh_transport
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    real_ssh = _REAL_HOME / ".ssh"
    if request.node.get_closest_marker("live_npu") is not None and real_ssh.is_dir() and not (home / ".ssh").exists():
        try:
            (home / ".ssh").symlink_to(real_ssh, target_is_directory=True)
        except TimeoutError:
            raise
        except OSError:
            shutil.copytree(real_ssh, home / ".ssh")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg-data"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(root / "mindie-config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(root / "mindie-data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(root / "isolated-diagnostics"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(root / "reporting.json"))
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(root / "remote-dev-state"))
    monkeypatch.setattr(ssh_transport, "_MUX_DIR", home / ".ssh" / "remote-dev-mux")
