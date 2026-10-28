"""Real OpenSSH authentication acceptance in one disposable, resource-limited container.

Opt in with REMOTE_DEV_AUTH_TEST_HOST and REMOTE_DEV_AUTH_TEST_IMAGE. The host
needs existing key access and Docker; no host accounts, SSH settings, keys,
devices or workload directories are changed. Only loopback ports are published.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pytest

from remote_dev.auth import setup_access, auth_status
from remote_dev.core.endpoint import Endpoint
from remote_dev.core import artifact_ops, job_ops, rpc_transport, ssh_transport

pytestmark = [
    pytest.mark.live_ssh,
    pytest.mark.skipif(not os.environ.get("REMOTE_DEV_AUTH_TEST_HOST"),
                       reason="explicit disposable OpenSSH authentication acceptance"),
]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class SshFixture:
    host: str
    name: str
    ssh: str
    password: str = field(repr=False)
    key_port: int = 0
    password_port: int = 0
    bindir: Path | None = None

    def admin(self, command, *, input=None):
        result = subprocess.run(
            [self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "ControlMaster=no", "-o", "ControlPath=none", self.host, command],
            input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=25,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def container(self, command, *, input=None):
        return self.admin(shlex.join(["docker", "exec", "-i", self.name, "sh", "-c", command]),
                          input=input)

    def endpoint(self, *, password_only=False):
        return Endpoint("127.0.0.1", self.password_port if password_only else self.key_port,
                        user="root", root="/tmp/auth-work", cwd="/tmp/auth-work",
                        runtime_env=False, ssh_mux=False)


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory):
    image = os.environ["REMOTE_DEV_AUTH_TEST_IMAGE"]
    root = tmp_path_factory.mktemp("real-auth-server")
    fixture = SshFixture(os.environ["REMOTE_DEV_AUTH_TEST_HOST"],
                         "mindie-auth-test-" + uuid.uuid4().hex[:12],
                         shutil.which("ssh"), secrets.token_urlsafe(24))
    tunnel = None
    created = False
    try:
        fixture.admin(shlex.join([
            "docker", "run", "--detach", "--name", fixture.name,
            "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
            "--publish", "127.0.0.1::2222", "--publish", "127.0.0.1::2223",
            "--entrypoint", "/bin/sh", image, "-c", "exec sleep 7200",
        ]))
        created = True
        fixture.container(
            "chpasswd; mkdir -p /run/sshd /tmp/auth-work; ssh-keygen -A",
            input="root:" + fixture.password + "\n",
        )
        for port, keys in ((2222, "yes"), (2223, "no")):
            config = (
                f"Port {port}\nListenAddress 0.0.0.0\n"
                "HostKey /etc/ssh/ssh_host_ed25519_key\n"
                f"PidFile /tmp/sshd-{port}.pid\n"
                "AuthorizedKeysFile .ssh/authorized_keys\n"
                f"PubkeyAuthentication {keys}\nPasswordAuthentication yes\n"
                "KbdInteractiveAuthentication no\nUsePAM no\n"
                "PermitRootLogin yes\nStrictModes yes\n"
                "AllowTcpForwarding yes\nLogLevel ERROR\n"
            )
            fixture.container(f"cat > /tmp/sshd-{port}.conf; /usr/sbin/sshd -f /tmp/sshd-{port}.conf",
                              input=config)
        ports = [int(fixture.admin(f"docker port {fixture.name} {p}/tcp").rsplit(":", 1)[1])
                 for p in (2222, 2223)]
        fixture.key_port, fixture.password_port = _free_port(), _free_port()
        tunnel = subprocess.Popen([
            fixture.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ExitOnForwardFailure=yes", "-N",
            "-L", f"127.0.0.1:{fixture.key_port}:127.0.0.1:{ports[0]}",
            "-L", f"127.0.0.1:{fixture.password_port}:127.0.0.1:{ports[1]}",
            fixture.host,
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 8
        while True:
            assert tunnel.poll() is None, tunnel.stderr.read().decode()
            try:
                with socket.create_connection(("127.0.0.1", fixture.key_port), timeout=.2):
                    break
            except OSError:
                assert time.monotonic() < deadline, "test SSH tunnel did not become ready"
                time.sleep(.05)
        fixture.bindir = root / "bin"
        fixture.bindir.mkdir()
        wrapper = fixture.bindir / "ssh"
        wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.quote(fixture.ssh) +
            " -F /dev/null -o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=" +
            shlex.quote(str(root / "known_hosts")) + ' "$@"\n', encoding="utf-8", newline="\n")
        wrapper.chmod(0o700)
        yield fixture
    finally:
        rpc_transport.close_connections()
        if tunnel is not None:
            tunnel.terminate()
            tunnel.communicate(timeout=5)
        if created:
            fixture.admin(f"docker rm -f {fixture.name}")


@pytest.fixture
def auth_env(tmp_path, monkeypatch, auth_server):
    monkeypatch.setenv("PATH", str(auth_server.bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "shared-auth"))
    # Exercise the normal user-level provider under the isolated auth root.
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "session-one"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(tmp_path / "diagnostics"))
    monkeypatch.setenv("REMOTE_DEV_SSH_MUX", "0")
    auth_server.container("rm -f /root/.ssh/authorized_keys")
    yield tmp_path
    rpc_transport.close_connections()


def _fresh_call(endpoint, tmp_path, profile="canonical", command="printf fresh-auth"):
    env = dict(os.environ, REMOTE_DEV_STATE_DIR=str(tmp_path / ("new-chat-" + uuid.uuid4().hex)),
               REMOTE_DEV_SESSION_ID=uuid.uuid4().hex)
    env.pop("TEST_SSH_PASSWORD", None)
    source = """
import json,sys
from remote_dev.mcp.tools import call_tool
from remote_dev.core.rpc_transport import close_connections
args=json.loads(sys.stdin.read())
try:
    print(json.dumps(call_tool("remote.bash",args,profile=sys.argv[1])))
finally:
    close_connections()
"""
    payload = {"host": endpoint.host, "port": endpoint.port, "user": endpoint.user,
               "root": endpoint.root, "cwd": endpoint.cwd, "runtime_env": False,
               "command": command, "timeout_ms": 15000}
    process = subprocess.run([sys.executable, "-c", source, profile], input=json.dumps(payload),
                             text=True, capture_output=True, env=env, timeout=22)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["result"]["outcome"] == "success", result
    return result


def test_password_installs_key_and_new_process_reuses_it(auth_server, auth_env):
    endpoint = auth_server.endpoint()
    result = setup_access(endpoint, password=auth_server.password)
    assert result["auth_method"] == "publickey", result
    assert result["bootstrap"]["key_verified"] is True, result
    assert "fresh-auth" in json.dumps(_fresh_call(endpoint, auth_env))
    profiles = list((auth_env / "shared-auth" / "profiles").glob("*.json"))
    assert profiles and all(auth_server.password not in p.read_text() for p in profiles)


def test_password_only_survives_process_chat_and_harness_changes(auth_server, auth_env):
    endpoint = auth_server.endpoint(password_only=True)
    result = setup_access(endpoint, password=auth_server.password)
    assert result["auth_method"] == "password", result
    assert result["bootstrap"]["key_verified"] is False, result
    for profile in ("codex", "claude", "kimi", "dsh"):
        assert "fresh-auth" in json.dumps(_fresh_call(endpoint, auth_env, profile=profile))


def test_revoked_public_key_recovers_before_single_business_write(auth_server, auth_env):
    endpoint = auth_server.endpoint()
    result = setup_access(endpoint, password=auth_server.password)
    auth_server.container("rm /root/.ssh/authorized_keys; rm -f /tmp/auth-work/recovery-count")
    command = "printf 'once\\n' >> recovery-count; cat recovery-count"
    result = _fresh_call(endpoint, auth_env, command=command)
    assert auth_server.container("cat /tmp/auth-work/recovery-count") == "once", result
    assert auth_status(endpoint)["auth_method"] == "publickey"
    assert auth_server.container("wc -l < /root/.ssh/authorized_keys") == "1"


def test_password_profile_supports_all_transport_channels(auth_server, auth_env):
    endpoint = auth_server.endpoint(password_only=True)
    setup_access(endpoint, password=auth_server.password, install_key=False)
    captured = ssh_transport.run_bytes(endpoint, "cat", stdin=b"\x00binary\xff", timeout_ms=15000)
    assert captured.returncode == 0 and captured.stdout == b"\x00binary\xff", captured
    streamed = ssh_transport.run_stream(endpoint, "printf streamed", merge_stderr=False, timeout_ms=15000)
    assert streamed.returncode == 0 and streamed.stdout == "streamed", streamed
    from remote_dev.diagnostics import diagnose_ssh
    diagnosed = diagnose_ssh(endpoint, timeout_ms=15000)
    assert diagnosed["status"] != "unavailable", diagnosed
    with ssh_transport.open_local_forward(endpoint, remote_port=2223, ready_timeout_s=15) as forward:
        with socket.create_connection(("127.0.0.1", forward.local_port), timeout=3) as connection:
            assert connection.recv(256).startswith(b"SSH-2.0-")
    source = auth_env / "artifact.bin"
    source.write_bytes(bytes(range(256)) * 1024)
    pushed = artifact_ops.remote_artifact_push(endpoint, local_path=str(source), remote_path="artifact.bin", timeout_ms=15000)
    assert pushed["result"]["outcome"] == "success", pushed
    pulled = artifact_ops.remote_artifact_pull(endpoint, remote_path="artifact.bin", local_dir=str(auth_env / "pulled"), timeout_ms=15000)
    assert pulled["result"]["outcome"] == "success", pulled
    assert any(p.read_bytes() == source.read_bytes() for p in (auth_env / "pulled").rglob("*") if p.is_file())
    started = job_ops.start_remote_job(endpoint, command="sleep 1; printf durable",
                                     job_id="job-auth-" + uuid.uuid4().hex[:12], yield_time_ms=1)
    assert started["result"]["outcome"] in {"success", "running"}, started
    rpc_transport.close_connections()
    record_id = started["result"].get("job_id")
    assert record_id, started
    observed = job_ops.remote_job_status(endpoint, job_id=record_id)
    assert observed["result"]["outcome"] in {"success", "running"}, observed
