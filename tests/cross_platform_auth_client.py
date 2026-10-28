"""Installed-runtime worker for cross-platform SSH authentication acceptance.

[Role] Exercises actual coordinator, Kimi MCP, and monitor consumer boundaries.
[Invariants] Runtime packages come from this interpreter's installed environment.
Kimi is a native plugin directory, not a Python distribution; only its explicit
scripts directory is added when exercising its published nonce/MCP adapter.
[Timeouts & Locks] Authentication and calls have 30s budgets; the driver bounds
each worker process and every exit closes the remote RPC connection registry.
[Side-Effect Policy] Setup alone receives a password on stdin. Other actions
reuse its shared profile reference; monitor operations never repair remote keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def _endpoint_arguments(payload):
    arguments = dict(payload["endpoint"])
    if payload.get("auth_ref"):
        arguments["auth_ref"] = payload["auth_ref"]
    return arguments


def _setup(payload):
    from remote_dev.auth import setup_access
    from remote_dev.core.endpoint import Endpoint

    endpoint = Endpoint(**_endpoint_arguments(payload))
    password = payload.get("password")
    if password is None:
        return setup_access(endpoint, timeout_ms=30000)
    os.environ["TEST_SSH_PASSWORD"] = password
    try:
        return setup_access(endpoint, password_env="TEST_SSH_PASSWORD", timeout_ms=30000)
    finally:
        os.environ.pop("TEST_SSH_PASSWORD", None)


def _call(payload):
    from remote_dev.mcp.tools import call_tool

    return call_tool("remote.bash", {
        **_endpoint_arguments(payload),
        "command": payload.get("command", "printf shared-auth-call"),
        "timeout_ms": 30000,
    }, profile=payload.get("profile", "canonical"))


def _coordinator(payload):
    from mindie_coordinator.env_manager import handle_hosts

    arguments = {
        **_endpoint_arguments(payload),
        "action": "bootstrap_key",
        "alias": "cross-platform-shared-auth",
        "timeout_ms": 30000,
    }
    password = payload.get("password")
    if password is not None:
        os.environ["TEST_SSH_PASSWORD"] = password
        arguments["password_env"] = "TEST_SSH_PASSWORD"
    try:
        return handle_hosts(arguments, state_dir=payload["state_dir"])
    finally:
        os.environ.pop("TEST_SSH_PASSWORD", None)


def _kimi(payload):
    scripts = Path(payload["kimi_root"]).resolve() / "scripts"
    sys.path.insert(0, str(scripts))
    import identity
    import mcp_server

    state = Path(payload["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    config = state / "kimi-config.json"
    config.write_text(json.dumps({"state_dir": str(state / "adapter")}), encoding="utf-8")
    os.environ["MINDIE_KIMI_CONFIG"] = str(config)
    os.environ["REMOTE_DEV_HARNESS_PROFILE"] = "kimi"
    session = payload["session"]
    arguments = {
        **_endpoint_arguments(payload),
        "command": payload.get("command", "printf plugin-shared-password"),
        "timeout_ms": 30000,
        "request_nonce": "auth-nonce-" + session,
    }
    name = "remote.bash"
    identity.publish_nonce_bind(session, arguments["request_nonce"],
                                name, "call-" + session, arguments)
    reply = mcp_server.handle("remote", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return {"reply": reply, "remote_state_dir": os.environ["REMOTE_DEV_STATE_DIR"]}


def _monitor(payload):
    from npu_top.db import Database
    from npu_top.device_adapter import DeviceAdapter
    from npu_top.probe import HostProbe
    from npu_top.ssh_access import SshAccess

    state = Path(payload["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    endpoint = payload["endpoint"]
    database = Database(state / "monitor.sqlite3")
    database.initialize()
    try:
        server = database.upsert_server({
            "id": "cross-platform-shared-auth",
            "name": "cross-platform-shared-auth",
            "host": endpoint["host"], "port": endpoint["port"],
            "username": endpoint["user"], "auth_ref": payload["auth_ref"],
        })
    finally:
        database.close()
    database = Database(state / "monitor.sqlite3")
    database.initialize()
    try:
        server = database.get_server(server["id"])
        access = SshAccess(state, state)
        checked = access.connect(server)
        probe = HostProbe(DeviceAdapter(access), timeout=30)
        command = probe._run_script(
            server, payload.get("command", "printf monitor-shared-password") + "\n", 30,
        )
        return {
            "connection": checked, "auth_ref": server["auth_ref"],
            "returncode": command.returncode, "stdout": command.stdout,
        }
    finally:
        database.close()


def main():
    import remote_dev
    from remote_dev.core.rpc_transport import close_connections

    payload = json.load(sys.stdin)
    actions = {
        "setup": _setup, "call": _call, "coordinator": _coordinator,
        "kimi": _kimi, "monitor": _monitor,
    }
    try:
        result = actions[payload["action"]](payload)
        result["_runtime"] = {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "remote_dev_file": remote_dev.__file__,
        }
        print(json.dumps(result))
    finally:
        close_connections()


if __name__ == "__main__":
    main()
