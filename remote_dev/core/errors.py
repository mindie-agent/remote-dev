"""
[Role] Structured exception hierarchy and transport submission-state extractor (`errors.py`).
[Invariants]
- Distinguishes `submission_state` (`not_sent`, `acknowledged`, `uncertain`) and `category` (`caller`, `validation`, `permission`, `remote_execution`, `cleanup`) without fragile prose regexes.
- `record_cleanup_failure` attaches secondary cleanup errors to the primary exception without masking the primary cause.
[Timeouts & Locks]
- Pure in-memory exception chain traversal (`error_details`).
[Side-Effect Policy]
- Side-effect-free exception metadata annotation.
"""
from __future__ import annotations


CALLER_ERROR_HINTS = {
    "invalid_argument": "Correct the named argument to the type and range declared in the tool schema, then retry; nothing was sent.",
    "endpoint_incomplete": "Provide host together with port (no implicit port 22), or select a configured endpoint with alias.",
    "endpoint_alias_unknown": "Use an alias from the endpoint alias files that defines host and port, or pass host and port explicitly.",
    "unknown_job": "Use the job_id or session_id returned by a command started in this session. Inspect legacy string IDs with remote_job_status or remote_job_tail.",
    "path_policy": "Use an absolute path on the remote endpoint inside its configured root; local filesystem paths are not valid here.",
    "unsupported_shell_for_runtime_env": "runtime_env needs a POSIX-family shell (bash, sh, dash, zsh, ksh); choose one of these or disable runtime_env.",
}


class RemoteDevError(RuntimeError):
    """Base class for deterministic remote-dev failures."""


class EndpointError(RemoteDevError):
    """Raised when an endpoint cannot be resolved."""


class PathPolicyError(RemoteDevError):
    """Raised when a remote path violates root/cwd policy (a pre-dispatch caller error)."""

    category = "caller"
    error_code = "path_policy"
    field = "path"


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
    previous = getattr(error, "cleanup_error", None)
    error.cleanup_error = (previous + "; " + detail)[:4000] if previous else detail
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
        for key in ("exit_code", "stderr_tail"):
            if getattr(current, key, None) is not None:
                result[key] = getattr(current, key)
        if getattr(current, "category", None) == "caller":
            result.update(category="caller", submission_state="not_sent", retryable=False)
            code = getattr(current, "error_code", None)
            if code in CALLER_ERROR_HINTS:
                result["error_code"] = code
                field = getattr(current, "field", None)
                if isinstance(field, str) and field.isidentifier():
                    result["field"] = field
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


def caller_error(message, exception_type=ValueError, *, error_code="invalid_argument", field=None):
    """Only known pre-dispatch input checks may use this classification."""
    error = exception_type(message)
    error.category = "caller"
    error.error_code = error_code if error_code in CALLER_ERROR_HINTS else "invalid_argument"
    error.field = field
    return error
