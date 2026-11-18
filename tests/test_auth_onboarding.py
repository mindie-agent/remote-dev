"""Frictionless SSH onboarding: default port, endpoint receipts and discovery.

[Role]
Verify `remote_auth`/`remote-dev auth setup` onboarding semantics without SSH.
[Invariants]
Passwords never reach endpoints.json, receipts stay successful when a verified
setup's local save or discovery fails, and a saved default resolves with no
host/port selectors.
[Timeouts & Locks]
Local temporary files only; discovery is mocked or parsed as text.
[Side-Effect Policy]
Writes temporary endpoint files and isolated remote-dev state.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import remote_dev.mcp.tools as tools
from remote_dev.auth import discover
from remote_dev.auth.endpoint_store import (
    endpoints_target_path,
    save_endpoint_record,
)
from remote_dev.core.endpoint import EndpointError, resolve_endpoint
from remote_dev.mcp.outcome import operation_outcome

FACTS = {
    "auth_ref": "authref-fixture",
    "auth_method": "publickey",
    "identity_file": "/tmp/fixture-key",
    "bootstrap": {"status": "already_configured", "key_verified": True},
    "status": "completed",
}


@pytest.fixture
def endpoint_file(tmp_path, monkeypatch):
    path = tmp_path / "endpoints.json"
    monkeypatch.setenv("REMOTE_DEV_ENDPOINTS_FILE", str(path))
    return path


def _setup(fake=None):
    calls: list[object] = []

    def setup_access(endpoint, **kwargs):
        calls.append(endpoint)
        return dict(fake or FACTS)

    return calls, mock.patch("remote_dev.auth.setup_access", setup_access)


def test_auth_host_defaults_to_port_22_but_other_tools_do_not():
    calls, patcher = _setup()
    with patcher:
        result = tools.call_tool("remote_auth", {"host": "192.0.2.10", "password": "secret"})
    assert calls[0].host == "192.0.2.10"
    assert calls[0].port == 22
    assert result["result"]["outcome"] == "success"
    assert "secret" not in json.dumps(result)

    with pytest.raises(EndpointError):
        tools.call_tool("remote_read", {"host": "192.0.2.10", "file_path": "/tmp/x"})


def test_auth_alias_without_host_uses_alias_port(endpoint_file):
    endpoint_file.write_text(json.dumps({"lab": {"host": "203.0.113.7", "port": 2200, "user": "ops"}}))
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {"alias": "lab", "password": "fixture"})
    assert calls[0].host == "203.0.113.7"
    assert calls[0].port == 2200


def test_save_default_records_non_secret_endpoint_and_resolves_without_selectors(endpoint_file):
    calls, patcher = _setup()
    with patcher:
        result = tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "user": "alice", "password": "never-store-me",
            "save_default": True,
        })
    details = result["result"]
    assert details["outcome"] == "success"
    assert details["endpoint_save"] == {
        "saved": True, "alias": "default", "default": True, "path": str(endpoint_file),
    }
    payload = json.loads(endpoint_file.read_text())
    assert payload["default_alias"] == "default"
    assert payload["endpoints"]["default"] == {
        "host": "192.0.2.10", "port": 22, "user": "alice", "auth_ref": "authref-fixture",
    }
    assert "never-store-me" not in endpoint_file.read_text()
    if os.name != "nt":
        assert stat.S_IMODE(endpoint_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(endpoint_file.parent.stat().st_mode) == 0o700

    resolved = resolve_endpoint({})
    assert (resolved.host, resolved.port, resolved.user) == ("192.0.2.10", 22, "alice")
    assert resolved.auth_ref == "authref-fixture"
    assert resolved.alias == "default"


def test_save_alias_keeps_default_map_clean_and_defaults_to_it(endpoint_file):
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {
            "host": "198.51.100.9", "port": 2222, "user": "ops",
            "save_default": True, "save_alias": " lab ",
        })
    payload = json.loads(endpoint_file.read_text())
    assert payload["default_alias"] == "lab"
    assert set(payload["endpoints"]) == {"lab"}
    assert "default" not in payload["endpoints"]
    resolved = resolve_endpoint({})
    assert (resolved.host, resolved.port, resolved.alias) == ("198.51.100.9", 2222, "lab")


def test_save_alias_merges_existing_flat_file_without_losing_entries(endpoint_file):
    endpoint_file.write_text(json.dumps({"old": {"host": "203.0.113.1", "port": 22}}))
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {
            "host": "203.0.113.2", "save_alias": "new", "action": "setup",
        })
    payload = json.loads(endpoint_file.read_text())
    assert set(payload) == {"old", "new"}
    assert payload["old"]["host"] == "203.0.113.1"
    assert payload["new"]["host"] == "203.0.113.2"


def test_save_failure_keeps_verified_setup_successful(endpoint_file):
    calls, patcher = _setup()
    with patcher, mock.patch(
        "remote_dev.auth.onboarding.save_endpoint_record",
        side_effect=OSError("PRIVATE_STORAGE_PATH"),
    ):
        result = tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "password": "secret", "save_default": True,
        })
    details = result["result"]
    assert details["outcome"] == "success"
    assert details["endpoint_save"]["saved"] is False
    assert details["receipt_unconfirmed"] is True
    assert details["receipt_warning"]["stage"] == "endpoint_save"
    assert operation_outcome("remote_auth", result) == "succeeded"
    assert "secret" not in json.dumps(result)


def test_discovery_failure_keeps_verified_setup_successful(endpoint_file):
    calls, patcher = _setup()
    with patcher, mock.patch(
        "remote_dev.auth.onboarding.discover_endpoint",
        return_value={"status": "unavailable", "reason": "timeout"},
    ):
        result = tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "password": "secret", "discover": True,
        })
    details = result["result"]
    assert details["outcome"] == "success"
    assert details["discovery"] == {"status": "unavailable", "reason": "timeout"}
    assert operation_outcome("remote_auth", result) == "succeeded"


def test_discovery_parser_reports_ok_partial_and_permission_states():
    ok = discover.parse_discovery(
        "__RD_OS__\nLinux 5.10 aarch64\n"
        "__RD_NPU__\nTotal Count : 2\nChip Name : Ascend 910B\n"
        "__RD_DOCKER__\nabc\tmindie-vllm\timage/vllm-ascend:latest\tUp 2 hours\n"
    )
    assert ok["status"] == "ok"
    assert ok["os"] == "Linux 5.10 aarch64"
    assert ok["npu_status"] == "ok"
    assert ok["npu_count"] == 2
    assert ok["npu_chips"] == ["Ascend 910B"]
    assert ok["docker_status"] == "ok"
    assert ok["containers"][0]["is_ascend_candidate"] is True

    partial = discover.parse_discovery("__RD_OS__\nLinux\n__RD_NPU__\n__RD_MISSING__\n")
    assert partial["status"] == "partial"
    assert partial["npu_status"] == "not_present"
    assert partial["docker_status"] == "not_installed"

    denied = discover.parse_discovery(
        "__RD_OS__\nLinux\n__RD_NPU__\n__RD_MISSING__\n"
        "__RD_DOCKER__\npermission denied while trying to connect to the Docker daemon\n"
    )
    assert denied["docker_status"] == "permission_denied"


def test_discovery_timeout_is_an_unavailable_receipt_without_raising(monkeypatch):
    class TimedOut:
        timed_out = True
        cancelled = False
        returncode = None
        stdout = b""
        stderr = b""

    monkeypatch.setattr(discover, "_launch", lambda endpoint, timeout_ms: (object(), timeout_ms))
    monkeypatch.setattr(discover, "capture_launch", lambda launch, timeout_ms: TimedOut())
    receipt = discover.discover_endpoint(object(), timeout_ms=5000)
    assert receipt == {"status": "unavailable", "reason": "timeout"}


def test_cli_password_at_end_of_command_defaults_port_and_never_echoes(endpoint_file, capsys):
    from remote_dev.cli import run_tool_main

    calls, patcher = _setup()
    with patcher:
        code = run_tool_main("auth", [
            "--host", "192.0.2.10", "--user", "alice", "--password", "cli-secret",
        ])
    output = capsys.readouterr()
    assert code == 0
    assert calls[0].port == 22
    assert calls[0].user == "alice"
    assert "cli-secret" not in output.out + output.err


def test_cli_save_default_and_discover_flags_reach_the_tool(endpoint_file, capsys, monkeypatch):
    from remote_dev.cli import run_tool_main

    calls, patcher = _setup()
    with patcher, mock.patch(
        "remote_dev.auth.onboarding.discover_endpoint",
        return_value={"status": "ok", "os": "Linux", "npu_status": "not_present"},
    ) as discovery:
        code = run_tool_main("auth", [
            "--host", "192.0.2.10", "--password", "cli-secret",
            "--save-default", "--discover",
        ])
    output = capsys.readouterr()
    assert code == 0
    discovery.assert_called_once()
    payload = json.loads(endpoint_file.read_text())
    assert payload["default_alias"] == "default"
    result = json.loads(output.out)["result"]
    assert result["discovery"]["status"] == "ok"
    assert result["endpoint_save"]["saved"] is True


def test_transport_hints_allow_native_fallback_without_blocking_language():
    from remote_dev.core.errors import AUTH_ERROR_HINTS, CALLER_ERROR_HINTS

    hints = " ".join([*AUTH_ERROR_HINTS.values(), *CALLER_ERROR_HINTS.values()]).casefold()
    for forbidden in ("halt and wait", "wait for the user", "fix remote-dev", "waiting on remote-dev"):
        assert forbidden not in hints
    assert "native ssh" in AUTH_ERROR_HINTS["connection_unavailable"]
    assert "native ssh" in AUTH_ERROR_HINTS["authentication_timeout"]


def test_cli_auth_status_defaults_port_and_skips_setup(endpoint_file, capsys, monkeypatch):
    from remote_dev.cli import run_tool_main

    seen = []

    def status(endpoint, **kwargs):
        seen.append(endpoint)
        return {"configured": False, "target": {"host": endpoint.host, "port": endpoint.port}}

    monkeypatch.setattr("remote_dev.auth.auth_status", status)
    code = run_tool_main("auth", ["--action", "status", "--host", "192.0.2.10", "--user", "alice"])
    assert code == 0
    assert seen and (seen[0].host, seen[0].port) == ("192.0.2.10", 22)
    assert "192.0.2.10" in capsys.readouterr().out


def test_save_alias_preserves_an_existing_flat_default(endpoint_file):
    endpoint_file.write_text(json.dumps({
        "old": {"host": "203.0.113.1", "port": 22},
        "default_alias": "old",
    }))
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {"host": "203.0.113.2", "save_alias": "new"})
    payload = json.loads(endpoint_file.read_text())
    assert payload["default_alias"] == "old"
    assert set(payload) == {"old", "new", "default_alias"}


def test_endpoint_store_writes_the_last_configured_path(tmp_path, monkeypatch):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    monkeypatch.setenv("REMOTE_DEV_ENDPOINTS_FILE", os.pathsep.join((str(first), str(second))))
    assert endpoints_target_path() == second

    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {"host": "192.0.2.10", "save_default": True})
    assert not first.exists()
    assert second.exists()
    resolved = resolve_endpoint({})
    assert (resolved.host, resolved.alias) == ("192.0.2.10", "default")


def test_endpoint_store_preserves_existing_parent_directory_mode(tmp_path, monkeypatch):
    parent = tmp_path / "shared"
    parent.mkdir()
    os.chmod(parent, 0o755)
    endpoint_file = parent / "endpoints.json"
    monkeypatch.setenv("REMOTE_DEV_ENDPOINTS_FILE", str(endpoint_file))
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {"host": "192.0.2.10", "save_default": True})
    assert endpoint_file.exists()
    if os.name != "nt":
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755


@pytest.mark.parametrize("alias", ["endpoints", "default_alias"])
def test_endpoint_store_rejects_reserved_alias_names(alias):
    endpoint = SimpleNamespace(host="192.0.2.10", port=22, user="alice")
    with pytest.raises(ValueError, match=alias):
        save_endpoint_record(endpoint, {}, save_alias=alias)


def test_forget_clears_alias_auth_ref_and_allows_resetup(endpoint_file):
    calls, patcher = _setup()
    with patcher:
        tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "save_default": True, "save_alias": "lab",
        })
    assert json.loads(endpoint_file.read_text())["endpoints"]["lab"]["auth_ref"] == "authref-fixture"

    with mock.patch("remote_dev.auth.forget_access",
                    lambda endpoint, **kwargs: {"removed": True, "auth_ref": "authref-fixture"}):
        result = tools.call_tool("remote_auth", {"action": "forget", "alias": "lab"})
    assert result["result"]["outcome"] == "success"
    assert "auth_ref" not in json.loads(endpoint_file.read_text())["endpoints"]["lab"]

    resetup, resetup_patcher = _setup()
    with resetup_patcher:
        tools.call_tool("remote_auth", {"alias": "lab", "password": "new-secret"})
    assert resetup[0].host == "192.0.2.10"
    assert getattr(resetup[0], "auth_ref", None) is None


def test_endpoint_and_profile_recording_warnings_are_both_preserved(endpoint_file):
    failed_profile = {
        **FACTS,
        "auth_ref": None,
        "status": "local_recording_failed",
        "operation_completed": True,
        "execution_outcome": "success",
        "profile_saved": False,
        "receipt_unconfirmed": True,
        "receipt_warning": {"stage": "profile_save", "error_type": "OSError"},
    }
    calls, patcher = _setup(failed_profile)
    with patcher, mock.patch(
        "remote_dev.auth.onboarding.save_endpoint_record",
        side_effect=OSError("PRIVATE_STORAGE_PATH"),
    ):
        result = tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "password": "secret", "save_default": True,
        })
    details = result["result"]
    assert details["profile_saved"] is False
    assert details["endpoint_save"]["saved"] is False
    assert {warning["stage"] for warning in details["recording_warnings"]} == {
        "profile_save", "endpoint_save",
    }


def test_discovery_is_skipped_when_the_single_deadline_is_exhausted(endpoint_file):
    calls, patcher = _setup()
    with patcher, mock.patch("remote_dev.auth.onboarding.discover_endpoint") as discovery:
        result = tools.call_tool("remote_auth", {
            "host": "192.0.2.10", "password": "secret", "discover": True, "timeout_ms": 1,
        })
    assert result["result"]["discovery"] == {"status": "skipped_budget_exhausted"}
    discovery.assert_not_called()


def test_discovery_reports_failing_docker_and_npu_exit_statuses():
    failed = discover.parse_discovery(
        "__RD_OS__\nLinux 5.10 aarch64\n"
        "__RD_NPU__\nNPU driver error\n__NPU_RC:1\n"
        "__RD_DOCKER__\nCannot connect to the Docker daemon\n__DOCKER_RC:1\n"
    )
    assert failed["npu_status"] == "error"
    assert failed["docker_status"] == "unavailable"

    missing = discover.parse_discovery(
        "__RD_OS__\nLinux\n__RD_NPU__\n__NPU_MISSING__\n"
        "__RD_DOCKER__\n__DOCKER_MISSING__\n"
    )
    assert missing["npu_status"] == "not_present"
    assert missing["docker_status"] == "not_installed"

    healthy = discover.parse_discovery(
        "__RD_OS__\nLinux\n__RD_NPU__\nTotal Count : 1\nChip Name : Ascend 910B\n"
        "__NPU_RC:0\n__RD_DOCKER__\n"
        "mindie-vllm\tsubmitted/vllm-ascend:latest\n__DOCKER_RC:0\n"
    )
    assert healthy["npu_status"] == "ok"
    assert healthy["docker_status"] == "ok"
    assert healthy["containers"][0]["name"] == "mindie-vllm"
    assert healthy["containers"][0]["is_ascend_candidate"] is True
