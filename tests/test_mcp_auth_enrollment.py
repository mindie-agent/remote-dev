"""One-time chat credentials reach shared enrollment through the public MCP API."""
import json
import io

import pytest
from jsonschema import validate

from remote_dev.auth import CredentialStore, ProfileStore, access, prepare_auth
from remote_dev.core.endpoint import Endpoint
from remote_dev.mcp import server
from remote_dev.mcp.native_spec import PROFILES
from remote_dev.mcp.schemas import harness_catalog


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("CHAT_SSH_PASSWORD", raising=False)
    monkeypatch.setattr(access, "ensure_key", lambda *args, **kwargs: (
        str(tmp_path / "key"), "ssh-ed25519 AAAAFIXTURE chat-enrollment"))
    sent = []
    monkeypatch.setattr(server, "send", lambda value, **kwargs: sent.append(value))
    return sent


@pytest.mark.parametrize("key_available", [True, False])
@pytest.mark.parametrize("profile", PROFILES)
def test_chat_password_enrolls_and_reuses_auth_without_server_environment(enrollment, monkeypatch, key_available, profile):
    monkeypatch.setenv("REMOTE_DEV_HARNESS_PROFILE", profile)
    secret = "fixture-\u5bc6\u7801-!$`quotes'\""
    methods = []

    def probe(endpoint, **kwargs):
        methods.append(kwargs["method"])
        if kwargs["method"] == "password":
            assert CredentialStore().get(kwargs["credential_ref"]) == secret
        code = 255 if len(methods) == 1 or (len(methods) == 4 and not key_available) else 0
        return access._Outcome(code, "", "Permission denied (publickey,password)." if code else "")

    monkeypatch.setattr(access, "_probe", probe)
    arguments = {"host": "auth.example", "port": 2200, "user": "alice", "password": secret}
    tool = next(tool for tool in harness_catalog(profile) if tool["name"] == "remote_auth")
    validate(arguments, tool["inputSchema"])
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "remote_auth", "arguments": arguments,
    }})
    assert "result" in enrollment[0], enrollment
    response = enrollment[0]["result"]
    assert response["isError"] is False
    method = "publickey" if key_available else "password"
    assert response["structuredContent"]["auth_method"] == method
    assert methods == ["publickey", "password", "password", "publickey"]
    endpoint = Endpoint("auth.example", 2200, user="alice")
    profile = ProfileStore().resolve(endpoint)
    assert profile["auth_method"] == method
    assert prepare_auth(endpoint, repair=False).endpoint.auth_ref == profile["auth_ref"]
    assert secret not in json.dumps(enrollment, ensure_ascii=False) + json.dumps(profile, ensure_ascii=False)


def test_conflicting_password_sources_do_not_start_ssh(enrollment, monkeypatch):
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: pytest.fail("SSH must not start"))
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "remote_auth", "arguments": {
            "host": "auth.example", "port": 22, "password": "fixture-secret",
            "password_env": "CHAT_SSH_PASSWORD",
        },
    }})
    assert enrollment[0]["error"]["data"]["category"] == "configuration"
    assert enrollment[0]["error"]["data"]["submission_state"] == "not_sent"
    assert "fixture-secret" not in json.dumps(enrollment)


@pytest.mark.parametrize("action", ["status", "forget"])
def test_password_is_not_silently_ignored_for_non_setup_actions(enrollment, action):
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "remote_auth", "arguments": {
            "host": "auth.example", "port": 22, "action": action, "password": "fixture-secret",
        },
    }})
    assert enrollment[0]["error"]["data"]["submission_state"] == "not_sent"
    assert "fixture-secret" not in json.dumps(enrollment)


def test_older_catalog_can_import_supplied_password_through_cli_stdin(enrollment, monkeypatch, capsys):
    from remote_dev.cli import run_tool_main

    secret = "fixture-stdin-!$`'\""
    monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: access._Outcome(0, "", ""))
    code = run_tool_main("auth", ["--host", "auth.example", "--port", "2200", "--user", "alice",
                                  "--password-stdin", "--no-install-key"])
    output = capsys.readouterr()
    assert code == 0
    assert json.loads(output.out)["result"]["auth_method"] == "password"
    profile = ProfileStore().resolve(Endpoint("auth.example", 2200, user="alice"))
    assert CredentialStore().get(profile["credential_ref"]) == secret
    assert secret not in output.out + output.err


@pytest.mark.parametrize("install_key", [True, False])
def test_verified_authentication_keeps_success_when_profile_recording_fails(enrollment, monkeypatch, install_key):
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: access._Outcome(0, "", ""))

    def unable_to_save(*args, **kwargs):
        raise OSError("PRIVATE_STORAGE_PATH")

    monkeypatch.setattr(ProfileStore, "save", unable_to_save)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "remote_auth", "arguments": {
            "host": "auth.example", "port": 22, "password": "fixture-secret", "install_key": install_key,
        },
    }})
    result = enrollment[0]["result"]
    assert result["isError"] is False
    details = result["structuredContent"]
    assert details["operation_completed"] is True
    assert details["receipt_unconfirmed"] is True
    assert details["auth_ref"] is None
    assert details["receipt_warning"]["error_type"] == "OSError"
    assert "PRIVATE_STORAGE_PATH" not in json.dumps(enrollment)
