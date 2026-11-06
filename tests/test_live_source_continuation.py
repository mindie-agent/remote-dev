"""Read-only A2/A3 checks: discovery failure leaves native scripts usable."""
import os
from pathlib import Path
import shlex

import pytest

from remote_dev.core.container_endpoint import pin_container_endpoint
from remote_dev.core.context_snapshot import remote_probe
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.python_discovery import compact_shell_script
from remote_dev.core.ssh_transport import run_script

_REAL_HOME = Path.home()
pytestmark = [pytest.mark.live_npu,
              pytest.mark.skipif(os.environ.get("REMOTE_DEV_LIVE_NPU") != "1", reason="explicit live endpoint acceptance")]


@pytest.mark.parametrize("host,container", [
    ("173.125.1.2", "mindie-bench-a2-core-20261017"),
    ("192.168.13.165", "vaws-offload-pd165-20261016"),
])
def test_failed_probe_and_custom_script_on_existing_container(host, container, tmp_path, monkeypatch):
    # Reuse an existing key without importing user credentials or changing
    # remote authorized_keys. All test-owned state stays in the fixture root.
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "ssh-access"))
    endpoint = pin_container_endpoint(Endpoint(
        host=host, port=22, container=container, root="/", cwd="/", runtime_env=False,
        identity_file=(_REAL_HOME / ".ssh/id_ed25519").as_posix(),
        known_hosts_file=(_REAL_HOME / ".ssh/known_hosts").as_posix(), connect_timeout_ms=5000,
    ), timeout_ms=5000)
    failed = remote_probe(endpoint, python_executable="/nonexistent/mindie-probe-python", timeout_ms=5000)["result"]
    assert failed["outcome"] == "failed"
    assert failed["next"]["endpoint_arguments"]["container"] == endpoint.container

    custom = 'set -- ' + shlex.join(["selected source directory", "line1\nline2"]) + "\n"
    custom += compact_shell_script('printf "%s\\n" "$1" "$2"\n' + "# task script\n" * 1500)
    escaped = run_script(endpoint, custom, timeout_ms=5000)
    assert escaped.returncode == 0, escaped.stderr
    assert escaped.stdout == "selected source directory\nline1\nline2\n"

    observed = remote_probe(endpoint, distributions=["torch", "torch-npu"], timeout_ms=5000)["result"]
    assert observed["outcome"] == "success", observed
    summary = observed["probe"]["summary"]
    assert summary["python_prefix"].startswith("/")
    assert summary["isolated_python"] is True
    assert set(summary["distributions"]) == {"torch", "torch-npu"}
