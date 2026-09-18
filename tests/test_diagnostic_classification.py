import json
from pathlib import Path
from mindie_diagnostics import configure
from remote_dev.observability import observed_tool
from remote_dev.core.errors import error_details


def test_expected_input_wait_warns_without_becoming_failure(tmp_path):
    recorder = configure("remote-dev", root=tmp_path)
    @observed_tool("remote.read")
    def wait():
        return {"outcome": "needs_input", "status": "missing_input"}
    reply = wait()
    rows = [json.loads(line) for line in Path(recorder.record_ref).read_text().splitlines()]
    assert reply["outcome"] == "needs_input"
    assert any(row["event"] == "tool.needs_input" and row["severity"] == "WARNING" for row in rows)
    assert not any(row["event"] == "operation.failure" for row in rows)


def test_returned_failure_keeps_exception_type_and_code(tmp_path):
    recorder = configure("remote-dev", root=tmp_path)
    @observed_tool("remote.bash")
    def fail():
        return {"outcome": "failed", "error_details": {"type": "RemoteExecutionError", "error_code": "reply_lost",
            "category": "rpc_timeout", "submission_state": "uncertain", "retryable": False}}
    fail()
    rows = [json.loads(line) for line in Path(recorder.record_ref).read_text().splitlines()]
    failure = next(row["attributes"] for row in rows if row["event"] == "operation.failure")
    assert failure["error_type"] == "RemoteExecutionError" and failure["error_code"] == "reply_lost"
    assert failure["submission_state"] == "uncertain"


def test_arbitrary_validation_and_permission_are_not_caller_classification():
    assert error_details(ValueError("broken internal JSON"))["category"] == "validation"
    assert error_details(PermissionError("state not writable"))["category"] == "permission"
