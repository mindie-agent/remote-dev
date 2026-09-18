"""Public logging contracts use real files and injected operations, never SSH."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from mindie_diagnostics import configure, get_recorder
from remote_dev.observability import observed_tool
from remote_dev.result import make_result
from remote_dev.mcp.tools import call_tool
from remote_dev.mcp.schemas import TOOL_SCHEMAS
from remote_dev.core.errors import RemoteExecutionError, error_details


def events(recorder):
    return [json.loads(line) for line in Path(recorder.record_ref).read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("name", list(TOOL_SCHEMAS))
def test_every_public_tool_records_pre_dispatch_failure(tmp_path, name):
    recorder = configure("remote-dev", root=tmp_path)
    with pytest.raises(ValueError, match="unsupported"):
        call_tool(name, {"not_an_argument": True})
    rows = events(recorder)
    starts = [row for row in rows if row["event"] == "operation.start"]
    ends = [row for row in rows if row["event"] == "operation.end"]
    assert len(starts) == len(ends) == 1
    assert starts[0]["operation"] == name
    assert starts[0]["operation_id"] == ends[0]["operation_id"]
    assert ends[0]["status"] == "error"


def test_tool_identity_exists_during_operation_and_ends_once(tmp_path):
    recorder = configure("remote-dev", root=tmp_path)
    @observed_tool("remote.read")
    def inner():
        return {"result": make_result(tool="remote.read", target={}, outcome="success", status="ok", summary="ok")}
    @observed_tool("remote.read")
    def outer():
        return inner()
    before = datetime.now(timezone.utc)
    result = outer()["result"]
    after = datetime.now(timezone.utc)
    rows = events(recorder)
    assert len([row for row in rows if row["event"] == "operation.start"]) == 1
    assert result["invocation_id"] == rows[0]["operation_id"] == result["diagnostics"]["operation_id"]
    assert before <= datetime.fromisoformat(result["started_at"]) <= after
    assert result["diagnostics"]["finished_at"] and result["diagnostics"]["status"] == "success"


def test_log_disk_failure_does_not_change_success_or_release(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("occupied")
    configure("remote-dev", root=blocked)
    @observed_tool("remote.bash")
    def success():
        return {"state": "succeeded", "quiet": True, "resources_released": True}
    result = success()
    assert result["state"] == "succeeded" and result["quiet"] and result["resources_released"]
    assert result["diagnostics"]["logging_failed"] is True


def test_error_certainty_survives_wrapped_exception():
    original = RemoteExecutionError("pipe lost", category="rpc_send", submission_state="uncertain")
    try:
        raise original
    except RemoteExecutionError as exc:
        try:
            raise RuntimeError("host failed") from exc
        except RuntimeError as wrapped:
            details = error_details(wrapped)
    assert details["submission_state"] == "uncertain" and details["retryable"] is False
    assert details["category"] == "rpc_send"


def test_lost_launch_reply_keeps_original_job_reference(tmp_path, monkeypatch):
    from remote_dev.core import job_ops
    from remote_dev.core.endpoint import resolve_endpoint
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(job_ops, "control", Mock(side_effect=RemoteExecutionError(
        "reply lost", category="rpc_timeout", submission_state="uncertain")))
    endpoint = resolve_endpoint({"host": "example.invalid", "port": 22, "root": "/tmp", "cwd": "/tmp"})
    result = job_ops.start_remote_job(endpoint, command="echo opaque")
    value = result["result"]
    assert value["status"] == "submission_uncertain"
    assert value["session_id"] == value["job_id"]
    assert value["error_details"]["submission_state"] == "uncertain"
    assert Path(value["refs"]["job_record"]).is_file()
    assert job_ops.control.call_count == 1
