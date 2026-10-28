"""Real OpenSSH acceptance through installed packages on PowerShell and WSL.

The CI provision step creates the disposable server and independent plugin venvs.
Tests never install packages, alter client SSH configuration, or use mocked SSH.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import uuid

import pytest

pytestmark = [
    pytest.mark.live_ssh,
    pytest.mark.skipif(not os.environ.get("REMOTE_DEV_CROSS_SSH_MANIFEST"),
                       reason="explicit cross-platform disposable OpenSSH acceptance"),
]


class Acceptance:
    def __init__(self, root):
        self.root = root
        self.manifest = json.loads(Path(os.environ["REMOTE_DEV_CROSS_SSH_MANIFEST"]).read_text())
        self.admin = json.loads(os.environ["REMOTE_DEV_CROSS_SSH_ADMIN"])
        self.pythons = json.loads(os.environ["REMOTE_DEV_CROSS_SSH_PYTHONS"])
        assert len(self.pythons) == 2 and len(set(self.pythons)) == 2
        assert all(" " in item and Path(item).is_file() for item in self.pythons)
        self.auth_root = root / "shared auth with spaces"
        self.environment = dict(os.environ, REMOTE_DEV_AUTH_DIR=str(self.auth_root),
                                REMOTE_DEV_SSH_MUX="0", MINDIE_TOP_URL="http://127.0.0.1:0",
                                PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        for name in ("REMOTE_DEV_CREDENTIAL_FILE", "PYTHONPATH", "TEST_SSH_PASSWORD",
                     "MINDIE_SSH_PASSWORD", "SSH_AUTH_SOCK"):
            self.environment.pop(name, None)
        self.inspect("reset")
        self.runtimes = []
        self.server_host = os.environ.get("REMOTE_DEV_CROSS_SSH_HOST", "127.0.0.1")
        with socket.create_connection((self.server_host, self.manifest["key_port"]), timeout=5) as sock:
            assert sock.recv(256).startswith(b"SSH-2.0-OpenSSH")

    def inspect(self, action="inspect"):
        completed = subprocess.run([*self.admin, action, "--state", self.manifest["root"]],
                                   capture_output=True, timeout=25)
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        return json.loads(completed.stdout)

    def endpoint(self, password_only=False):
        return {
            "host": self.server_host,
            "port": self.manifest["password_port" if password_only else "key_port"],
            "user": self.manifest["user"], "root": self.manifest["work"],
            "cwd": self.manifest["work"], "runtime_env": False,
            "known_hosts_file": str(self.root / "known hosts"),
        }

    def invoke(self, action, endpoint, *, runtime=0, password=False, **extra):
        state = self.root / ("chat " + uuid.uuid4().hex)
        environment = dict(self.environment, REMOTE_DEV_STATE_DIR=str(state / "remote"),
                           MINDIE_DIAGNOSTICS_ROOT=str(state / "diagnostics"),
                           REMOTE_DEV_SESSION_ID=uuid.uuid4().hex)
        payload = {
            "action": action, "endpoint": endpoint, "state_dir": str(state),
            "session": uuid.uuid4().hex, "kimi_root": os.environ["REMOTE_DEV_CROSS_KIMI_ROOT"],
            **extra,
        }
        if password:
            payload["password"] = self.manifest["password"]
        worker = Path(__file__).with_name("cross_platform_auth_client.py")
        completed = subprocess.run(
            [self.pythons[runtime], "-I", str(worker)], input=json.dumps(payload).encode("utf-8"),
            capture_output=True, env=environment, timeout=65,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        result = json.loads(completed.stdout)
        assert self.manifest["password"] not in json.dumps(result, ensure_ascii=False)
        runtime_info = result.pop("_runtime")
        assert Path(runtime_info["prefix"]) == Path(self.pythons[runtime]).parent.parent
        assert Path(runtime_info["remote_dev_file"]).is_relative_to(Path(runtime_info["prefix"]))
        self.runtimes.append(runtime_info)
        return result

    def assert_success(self, result, expected):
        assert result["result"]["outcome"] == "success", result
        assert expected in json.dumps(result), result


@pytest.fixture(scope="module", autouse=True)
def platform_evidence():
    expected = os.environ["REMOTE_DEV_CROSS_PLATFORM"]
    if expected == "windows-powershell":
        assert sys.platform == "win32" and os.name == "nt"
        assert os.environ.get("REMOTE_DEV_CROSS_SHELL", "").startswith("PowerShell ")
    elif expected == "wsl":
        assert sys.platform == "linux"
        assert "microsoft" in platform.release().lower(), "ordinary Linux is not WSL evidence"
        assert os.environ.get("WSL_DISTRO_NAME"), "the test must run inside a real WSL distribution"
    elif expected == "macos":
        assert sys.platform == "darwin"
    else:
        raise AssertionError("unknown acceptance platform")
    evidence = {"platform": expected, "system": platform.system(),
                "release": platform.release(), "python": sys.version,
                "shell": os.environ.get("REMOTE_DEV_CROSS_SHELL"),
                "wsl_distribution": os.environ.get("WSL_DISTRO_NAME"),
                "runtime_coverage": "installed remote-dev/coordinator/npu-top; actual Kimi adapter",
                "harness_profiles": ["codex", "claude", "kimi", "dsh"]}
    wheelhouse = Path(os.environ.get("REMOTE_DEV_CROSS_WHEELHOUSE", "wheelhouse"))
    evidence["component_commits"] = {
        path.stem: path.read_text().strip() for path in sorted(wheelhouse.glob("*.commit"))
    }
    evidence["consumer_interpreters"] = json.loads(os.environ["REMOTE_DEV_CROSS_SSH_PYTHONS"])
    kimi_commit = subprocess.run(
        ["git", "-C", os.environ["REMOTE_DEV_CROSS_KIMI_ROOT"], "rev-parse", "HEAD"],
        capture_output=True, timeout=5,
    )
    assert kimi_commit.returncode == 0
    evidence["kimi_commit"] = kimi_commit.stdout.decode().strip()
    from remote_dev.core.ssh_transport import _ssh_executable
    ssh = _ssh_executable()
    version = subprocess.run([ssh, "-V"], capture_output=True, timeout=5)
    assert version.returncode == 0
    evidence["ssh"] = ssh
    evidence["ssh_version"] = version.stderr.decode("utf-8", "replace").strip()
    print(json.dumps(evidence))
    target = os.environ.get("REMOTE_DEV_CROSS_EVIDENCE")
    if target:
        Path(target).write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.fixture
def acceptance(tmp_path):
    return Acceptance(tmp_path)


def test_auto_install_key_then_new_venv_without_password(acceptance):
    endpoint = acceptance.endpoint()
    configured = acceptance.invoke("setup", endpoint, password=True)
    assert configured["auth_method"] == "publickey", configured
    assert configured["bootstrap"]["key_verified"], configured
    installed = Path(configured["identity_file"] + ".pub").read_text().strip()
    assert acceptance.inspect()["authorized_keys"] == [installed]
    called = acceptance.invoke("call", endpoint, runtime=1, command="printf key-reused")
    acceptance.assert_success(called, "key-reused")
    assert (acceptance.auth_root / "credentials.json").is_file()
    profiles = list((acceptance.auth_root / "profiles").glob("*.json"))
    assert len(profiles) == 1
    assert acceptance.manifest["password"] not in profiles[0].read_text()


def test_password_only_server_across_processes_and_profiles(acceptance):
    endpoint = acceptance.endpoint(password_only=True)
    configured = acceptance.invoke("setup", endpoint, password=True)
    assert configured["auth_method"] == "password", configured
    assert configured["bootstrap"]["key_verified"] is False, configured
    before = acceptance.inspect()["authorized_keys"]
    for index, profile in enumerate(("codex", "claude", "kimi", "dsh")):
        called = acceptance.invoke("call", endpoint, runtime=index % 2,
                                   profile=profile, command="printf password-reused")
        acceptance.assert_success(called, "password-reused")
    assert acceptance.inspect()["authorized_keys"] == before


def test_revoked_key_is_repaired_before_exactly_one_business_write(acceptance):
    endpoint = acceptance.endpoint()
    configured = acceptance.invoke("setup", endpoint, password=True)
    installed = Path(configured["identity_file"] + ".pub").read_text().strip()
    acceptance.inspect("revoke")
    called = acceptance.invoke(
        "call", endpoint, runtime=1,
        command="printf 'once\\n' >> recovery-count; cat recovery-count",
    )
    acceptance.assert_success(called, "once")
    observed = acceptance.inspect()
    assert observed["counter"] == "once\n"
    assert observed["authorized_keys"] == [installed]
    assert called["result"]["authentication"]["bootstrap"]["key_verified"], called


def test_coordinator_kimi_two_chats_and_monitor_share_default_store(acceptance):
    endpoint = acceptance.endpoint(password_only=True)
    configured = acceptance.invoke("setup", endpoint, password=True)
    reference = configured["auth_ref"]
    coordinated = acceptance.invoke("coordinator", endpoint, runtime=1, auth_ref=reference)
    assert coordinated["state"] == "ready", coordinated
    assert coordinated["authentication"]["auth_ref"] == reference
    assert coordinated["authentication"]["auth_method"] == "password"
    for index in (0, 1):
        called = acceptance.invoke("kimi", endpoint, runtime=index, auth_ref=reference,
                                   command="printf plugin-shared-password")
        assert called["reply"]["result"]["isError"] is False, json.dumps(called)
        assert "plugin-shared-password" in json.dumps(called), called
    monitored = acceptance.invoke("monitor", endpoint, runtime=1, auth_ref=reference)
    assert monitored["connection"]["ok"], monitored
    assert monitored["returncode"] == 0, monitored
    assert monitored["stdout"] == "monitor-shared-password", monitored
    assert len(list((acceptance.auth_root / "profiles").glob("*.json"))) == 1
    assert len({item["prefix"] for item in acceptance.runtimes}) == 2
