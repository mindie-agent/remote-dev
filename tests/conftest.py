"""Never send intentional test failures to the developer's real diagnostic root."""
import pytest


@pytest.fixture(autouse=True)
def isolated_diagnostics(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(root / "isolated-diagnostics"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(root / "reporting.json"))
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(root / "remote-dev-state"))
