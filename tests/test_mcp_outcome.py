"""Public result captures preserve failure, execution certainty and native exits."""
import copy

import pytest

from remote_dev.mcp.outcome import operation_outcome


@pytest.mark.parametrize("name,details,expected", [
    ("remote_write", {"outcome": "blocked", "status": "path_outside_root"}, "failed"),
    ("remote_edit", {"outcome": "conflict", "status": "content_changed"}, "failed"),
    ("remote_bash", {"outcome": "failed", "status": "submission_uncertain",
                     "error_details": {"submission_state": "uncertain"}}, "unknown"),
    ("remote_bash", {"outcome": "failed", "state": "failed", "exit_code": 2}, "succeeded"),
    ("remote_bash", {"outcome": "success", "state": "succeeded", "exit_code": 0,
                     "error_details": {"submission_state": "uncertain"}}, "unknown"),
    ("remote_write", {"outcome": "failed", "status": "local_recording_failed",
                      "operation_completed": True, "execution_outcome": "success"}, "succeeded"),
    ("remote_bash", {"kind": "background", "jobId": "fixture-owned-job"}, "succeeded"),
    ("remote_job_status", {"outcome": "cancellation-requested"}, "succeeded"),
])
def test_result_capture_classification_does_not_rewrite_evidence(name, details, expected):
    payload = {"text": "fixture", "result": details}
    before = copy.deepcopy(payload)
    assert operation_outcome(name, payload) == expected
    assert payload == before
