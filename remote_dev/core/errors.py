from __future__ import annotations


class RemoteDevError(RuntimeError):
    """Base class for deterministic remote-dev failures."""


class EndpointError(RemoteDevError):
    """Raised when an endpoint cannot be resolved."""


class PathPolicyError(RemoteDevError):
    """Raised when a remote path violates root/cwd policy."""


class RemoteExecutionError(RemoteDevError):
    """Raised when a remote command cannot be launched cleanly."""

    def __init__(self, message, *, category="remote_execution", submission_state=None, retryable=False):
        super().__init__(message)
        self.category = category
        self.submission_state = submission_state
        self.retryable = bool(retryable)


def record_cleanup_failure(error, cleanup_error):
    """Keep the primary failure and expose cleanup separately to callers."""
    detail = f"{type(cleanup_error).__name__}: {cleanup_error}"[:1000]
    error.cleanup_error = detail
    error.add_note("Cleanup also failed: " + detail)


def error_details(exc):
    """Keep transport certainty through exception wrappers without parsing prose."""
    result = {"type": type(exc).__name__, "category": "internal", "retryable": False}
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "cleanup_error", None) is not None:
            result["cleanup_error"] = current.cleanup_error
        if getattr(current, "operation_completed", None) is not None:
            result["operation_completed"] = current.operation_completed
        if getattr(current, "category", None) == "caller":
            result.update(category="caller", submission_state="not_sent", retryable=False)
            break
        if isinstance(current, RemoteExecutionError) or hasattr(current, "submission_state"):
            result.update(category=getattr(current, "category", "remote_execution"), retryable=getattr(current, "retryable", False))
            if getattr(current, "submission_state", None) is not None:
                result["submission_state"] = current.submission_state
            break
        if isinstance(current, (ValueError, TypeError)):
            result.update(category="validation")
        elif isinstance(current, PermissionError):
            result.update(category="permission")
        current = current.__cause__ or current.__context__
    return result


def caller_error(message, exception_type=ValueError):
    """Only known pre-dispatch input checks may use this classification."""
    error = exception_type(message)
    error.category = "caller"
    return error
