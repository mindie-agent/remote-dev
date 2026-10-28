"""Real shared credentials across coordinator, a plugin MCP adapter, and npu-top.

Opt-in acceptance uses the disposable SSH fixture from test_real_ssh_auth.
Sibling checkout roots can be overridden through REMOTE_DEV_AUTH_SOURCE_ROOT.
The actual Kimi nonce/MCP dispatcher runs in fresh processes; no LLM host runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

if __name__ != "__main__":
    from test_real_ssh_auth import auth_env, auth_server, pytestmark


def _consumer_worker(kind, source_root, payload):
    root = Path(source_root)
    if os.environ.get("REMOTE_DEV_AUTH_PACKAGE_ONLY"):
        import remote_dev

        assert Path(remote_dev.__file__).is_relative_to(Path(sys.prefix))
    else:
        for component in ("coordinator", "npu-top"):
            sys.path.insert(0, str(root / component))
    if kind == "coordinator":
        from mindie_coordinator.env_manager import handle_hosts

        endpoint = payload["endpoint"]
        return handle_hosts({
            "action": "bootstrap_key", "alias": "shared-auth-acceptance",
            "host": endpoint["host"], "port": endpoint["port"], "user": endpoint["user"],
            "password": payload["password"],
        }, state_dir=payload["state_dir"])
    if kind == "kimi":
        sys.path.insert(0, str(_kimi_root(root) / "scripts"))
        import identity
        import mcp_server
        from remote_dev.core.rpc_transport import close_connections

        arguments = {
            **payload["endpoint"], "auth_ref": payload["auth_ref"],
            "command": "printf plugin-shared-password", "timeout_ms": 15000,
            "request_nonce": "auth-nonce-" + payload["session"],
        }
        name = "remote.bash"
        identity.publish_nonce_bind(payload["session"], arguments["request_nonce"],
                                    name, "call-" + payload["session"], arguments)
        try:
            result = mcp_server.handle("remote", {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            return {"reply": result, "remote_state_dir": os.environ["REMOTE_DEV_STATE_DIR"]}
        finally:
            close_connections()
    if kind == "monitor":
        from npu_top.db import Database
        from npu_top.device_adapter import DeviceAdapter
        from npu_top.probe import HostProbe
        from npu_top.ssh_access import SshAccess

        state = Path(payload["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        endpoint = payload["endpoint"]
        database = Database(state / "monitor.sqlite3")
        database.initialize()
        server = database.upsert_server({
            "id": "shared-auth-acceptance",
            "name": "shared-auth-acceptance", "host": endpoint["host"],
            "port": endpoint["port"], "username": endpoint["user"],
            "auth_ref": payload["auth_ref"],
        })
        database.close()
        database = Database(state / "monitor.sqlite3")
        database.initialize()
        server = database.get_server(server["id"])
        access = SshAccess(state, state)
        checked = access.connect(server)
        probe = HostProbe(DeviceAdapter(access), timeout=20)
        command = probe._run_script(server, "printf monitor-shared-password\n", 15)
        database.close()
        return {"connection": checked, "auth_ref": server["auth_ref"],
                "returncode": command.returncode, "stdout": command.stdout}
    raise ValueError("unknown acceptance consumer")


def _run_consumer(kind, source_root, payload, environment):
    isolation = ["-I"] if os.environ.get("REMOTE_DEV_AUTH_PACKAGE_ONLY") else []
    process = subprocess.run(
        [sys.executable, *isolation, str(Path(__file__).resolve()), kind, str(source_root)],
        input=json.dumps(payload), text=True, capture_output=True, env=environment, timeout=40,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def _kimi_root(source_root):
    return Path(os.environ.get("REMOTE_DEV_AUTH_KIMI_ROOT", source_root / "mindie-agent-kimi"))


def test_password_only_profile_is_shared_by_real_consumer_processes(auth_server, auth_env):
    source_root = Path(os.environ.get("REMOTE_DEV_AUTH_SOURCE_ROOT", Path(__file__).resolve().parents[2]))
    for expected in ("coordinator/mindie_coordinator", "npu-top/npu_top"):
        if not (source_root / expected).exists():
            pytest.skip(f"consumer checkout unavailable: {expected}")
    if not (_kimi_root(source_root) / "scripts" / "mcp_server.py").is_file():
        pytest.skip("Kimi consumer checkout unavailable")
    endpoint = auth_server.endpoint(password_only=True)
    endpoint_fields = {
        "host": endpoint.host, "port": endpoint.port, "user": endpoint.user,
        "root": endpoint.root, "cwd": endpoint.cwd, "runtime_env": False,
    }
    environment = dict(
        os.environ, MINDIE_TOP_URL="http://127.0.0.1:0",
        REMOTE_DEV_STATE_DIR=str(auth_env / "coordinator-calls"),
    )
    if os.environ.get("REMOTE_DEV_AUTH_PACKAGE_ONLY"):
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(source_root / "remote-dev"), environment.get("PYTHONPATH"),
        )))
    configured = _run_consumer("coordinator", source_root, {
        "endpoint": endpoint_fields, "password": auth_server.password,
        "state_dir": str(auth_env / "coordinator"),
    }, environment)
    assert configured["state"] == "ready", configured
    authentication = configured["authentication"]
    assert authentication["auth_method"] == "password", authentication
    reference = authentication["auth_ref"]
    assert auth_server.password not in json.dumps(configured)

    adapter_state = auth_env / "kimi-adapter"
    config = auth_env / "kimi.json"
    config.write_text(json.dumps({"state_dir": str(adapter_state)}), encoding="utf-8")
    environment["MINDIE_KIMI_CONFIG"] = str(config)
    environment.pop("MINDIE_SSH_PASSWORD", None)
    environment.pop("TEST_SSH_PASSWORD", None)
    for session in ("first-chat", "second-chat"):
        called = _run_consumer("kimi", source_root, {
            "endpoint": endpoint_fields, "auth_ref": reference, "session": session,
        }, environment)
        assert called["reply"]["result"]["isError"] is False, called
        assert "plugin-shared-password" in json.dumps(called), called
        assert called["remote_state_dir"] == str(adapter_state / "remote" / session)

    monitored = _run_consumer("monitor", source_root, {
        "endpoint": endpoint_fields, "auth_ref": reference,
        "state_dir": str(auth_env / "npu-top"),
    }, environment)
    assert monitored["connection"]["ok"], monitored
    assert monitored["auth_ref"] == reference
    assert monitored["returncode"] == 0
    assert monitored["stdout"] == "monitor-shared-password"
    profiles = list((auth_env / "shared-auth" / "profiles").glob("*.json"))
    assert len(profiles) == 1
    assert (auth_env / "shared-auth" / "credentials.json").is_file()
    assert not (auth_env / "npu-top" / "keys").exists()
    assert auth_server.password not in (auth_env / "coordinator" / "machines.json").read_text()


def test_monitor_uses_saved_password_after_key_revocation_without_repair(auth_server, auth_env):
    from remote_dev.auth import setup_access

    source_root = Path(os.environ.get("REMOTE_DEV_AUTH_SOURCE_ROOT", Path(__file__).resolve().parents[2]))
    if not (source_root / "npu-top" / "npu_top").exists():
        pytest.skip("npu-top consumer checkout unavailable")
    endpoint = auth_server.endpoint()
    configured = setup_access(endpoint, password=auth_server.password)
    assert configured["auth_method"] == "publickey", configured
    shared_root = auth_env / "shared-auth"
    before = {str(path.relative_to(shared_root)): path.read_bytes()
              for directory in ("profiles", "keys")
              for path in (shared_root / directory).rglob("*") if path.is_file()}
    auth_server.container("rm /root/.ssh/authorized_keys")
    environment = dict(os.environ)
    if os.environ.get("REMOTE_DEV_AUTH_PACKAGE_ONLY"):
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = str(source_root / "remote-dev")
    monitored = _run_consumer("monitor", source_root, {
        "endpoint": {"host": endpoint.host, "port": endpoint.port, "user": endpoint.user},
        "auth_ref": configured["auth_ref"], "state_dir": str(auth_env / "npu-top"),
    }, environment)

    assert monitored["connection"]["ok"], monitored
    assert monitored["returncode"] == 0, monitored
    assert monitored["stdout"] == "monitor-shared-password", monitored
    assert auth_server.container("test ! -e /root/.ssh/authorized_keys && printf absent") == "absent"
    after = {str(path.relative_to(shared_root)): path.read_bytes()
             for directory in ("profiles", "keys")
             for path in (shared_root / directory).rglob("*") if path.is_file()}
    assert after == before
    assert not (auth_env / "npu-top" / "keys").exists()


def test_key_install_preserves_existing_authorized_key_without_final_newline(auth_server, auth_env):
    from remote_dev.auth import setup_access

    original = auth_server.container(
        "mkdir -p /root/.ssh; "
        "ssh-keygen -q -t ed25519 -N '' -f /tmp/existing-no-newline-key; "
        'printf "%s" "$(cat /tmp/existing-no-newline-key.pub)" > /root/.ssh/authorized_keys; '
        "cat /root/.ssh/authorized_keys"
    )
    endpoint = auth_server.endpoint()
    configured = setup_access(endpoint, password=auth_server.password)
    assert configured["auth_method"] == "publickey", configured
    installed = Path(configured["identity_file"] + ".pub").read_text().strip()
    actual = auth_server.container("cat /root/.ssh/authorized_keys")
    assert actual.splitlines() == [original, installed]
    setup_access(endpoint)
    assert auth_server.container("cat /root/.ssh/authorized_keys") == actual


if __name__ == "__main__":
    print(json.dumps(_consumer_worker(sys.argv[1], sys.argv[2], json.load(sys.stdin))))
