"""[Role] Classify public remote tool results for MCP and local receipts.
[Invariants] Returned failures stay failures; uncertain writes outrank success
markers. Completed command exit codes remain native observations. Local receipt
failures cannot erase an acknowledged primary result.
[Timeouts & Locks] Pure in-memory classification; no locks or retries.
[Side-Effect Policy] Never changes the payload or submits an operation.
"""
from __future__ import annotations


def operation_outcome(name, payload):
    """Return succeeded/failed/unknown using the shared public result contract."""
    if not isinstance(payload, dict):
        return "failed"
    details = payload.get("result", payload.get("structuredContent", payload))
    if not isinstance(details, dict):
        return "failed"
    error = details.get("error_details")
    error = error if isinstance(error, dict) else {}
    if (details.get("unknown") is True or details.get("status") == "submission_uncertain"
            or error.get("submission_state") == "uncertain"):
        return "unknown"
    outcome = details.get("outcome")
    if (details.get("status") == "local_recording_failed"
            and (details.get("operation_completed") is True or error.get("operation_completed") is True)):
        outcome = details.get("execution_outcome", outcome)
    command = str(name).lower().endswith(("bash", "job_stdin", "exec_command", "write_stdin", "shell"))
    if (command and details.get("state") in {"succeeded", "failed", "timeout", "cancelled"}
            and type(details.get("exit_code")) is int):
        return "succeeded"
    if details.get("kind") == "background" and details.get("jobId") not in (None, ""):
        return "succeeded"
    if outcome in {"success", "cancelled", "cancellation-requested", "already-finished"}:
        return "succeeded"
    # Some consumer-local query tools retain their older explicit ok envelope.
    if "outcome" not in details and details.get("ok") is True:
        return "succeeded"
    return "failed"
