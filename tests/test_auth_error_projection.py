"""Safe MCP authentication failures retain delivery and key-maintenance facts."""
import json

import pytest

from remote_dev.auth import access
from remote_dev.core.errors import (
    AUTH_ERROR_HINTS, RemoteExecutionError, error_details, safe_auth_error_details,
)
from remote_dev.mcp import server


def test_real_auth_call_reports_missing_credential_without_private_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("PRIVATE_MISSING_SSH_PASSWORD", raising=False)
    sent = []
    monkeypatch.setattr(server, "send", lambda value, **kwargs: sent.append(value))
    server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "remote_auth", "arguments": {
            "host": "192.0.2.10", "port": 22, "user": "fixture",
            "password_env": "PRIVATE_MISSING_SSH_PASSWORD",
        }},
    })
    failure = sent[0]["error"]
    assert failure["data"]["category"] == "credential_unavailable"
    assert failure["data"]["submission_state"] == "not_sent"
    assert failure["message"] == AUTH_ERROR_HINTS["credential_unavailable"]
    assert "PRIVATE_MISSING_SSH_PASSWORD" not in json.dumps(sent)


@pytest.mark.parametrize("state", ["not_sent", "uncertain"])
def test_auth_projection_keeps_maintenance_uncertainty_separate_from_submission(state):
    error = RemoteExecutionError("PRIVATE_SSH_STDERR", category="connection_unavailable",
                                 submission_state=state)
    error.stderr_tail = "PRIVATE_SSH_STDERR"
    error.bootstrap = {
        "status": "unknown", "installation": "unknown", "key_verified": False,
        "error": "PRIVATE_SSH_STDERR", "cleanup_error": "PRIVATE_PATH",
    }
    public = safe_auth_error_details(error_details(error))
    assert public["submission_state"] == state
    assert public["bootstrap"] == {
        "status": "unknown", "installation": "unknown", "key_verified": False,
    }
    assert "PRIVATE" not in json.dumps(public)


def test_host_key_rejection_has_actionable_safe_hint():
    with pytest.raises(RemoteExecutionError) as caught:
        access._require_success(
            access._Outcome(255, "", "Host key verification failed. PRIVATE_HOST_PATH"),
            "SSH verification",
        )
    public = safe_auth_error_details(error_details(caught.value))
    assert public["category"] == "host_key"
    assert public["submission_state"] == "not_sent"
    assert public["hint"] == AUTH_ERROR_HINTS["host_key"]
    assert "PRIVATE_HOST_PATH" not in json.dumps(public)


def test_business_failure_does_not_gain_authentication_repair_guidance():
    from remote_dev.auth.store import auth_error

    error = RemoteExecutionError("receipt failed", category="local_recording_failed",
                                 submission_state="acknowledged")
    error.operation_completed = True
    assert safe_auth_error_details(error_details(error)) is None
    failure = auth_error("cannot save SSH profile", "local_recording_failed")
    assert safe_auth_error_details(error_details(failure))["submission_state"] == "not_sent"
