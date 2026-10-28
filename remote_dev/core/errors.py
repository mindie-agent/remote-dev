"""
[Role] Structured exception hierarchy and transport submission-state extractor (`errors.py`).
[Invariants]
- Distinguishes `submission_state` (`not_sent`, `acknowledged`, `uncertain`) and `category` (`caller`, `validation`, `permission`, `remote_execution`, `cleanup`) without fragile prose regexes.
- `record_cleanup_failure` attaches secondary cleanup errors to the primary exception without masking the primary cause.
- Known local caller failures may attach bounded `input_repair` metadata (`missing`, `action`); arbitrary exception text is never copied.
[Timeouts & Locks]
- Pure in-memory exception chain traversal (`error_details`).
[Side-Effect Policy]
- Side-effect-free exception metadata annotation.
"""
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
    previous = getattr(error, "cleanup_error", None)
    error.cleanup_error = (previous + "; " + detail)[:4000] if previous else detail
    error.add_note("Cleanup also failed: " + detail)


def _input_repair(value):
    """Bound trusted repair metadata to the small public contract."""
    if not isinstance(value, dict):
        return None
    missing = [item for item in value.get("missing", ())
               if isinstance(item, str) and 0 < len(item) <= 64][:16]
    action = value.get("action")
    if not isinstance(action, str) or not action:
        action = None
    elif len(action) > 500:
        action = action[:500]
    if not missing and action is None:
        return None
    result = {}
    if missing:
        result["missing"] = missing
    if action is not None:
        result["action"] = action
    return result


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
        bootstrap = getattr(current, "bootstrap", None)
        if isinstance(bootstrap, dict):
            # Auth maintenance may have changed authorized_keys even though
            # the caller's business operation has not been submitted.
            result["bootstrap"] = {
                key: bootstrap[key] for key in (
                    "status", "installation", "verification", "key_verified",
                    "exit_code", "error", "cleanup_error", "profile_saved",
                    "credential_retained", "credential_ref",
                ) if key in bootstrap
            }
        for key in ("exit_code", "stderr_tail"):
            if getattr(current, key, None) is not None:
                result[key] = getattr(current, key)
        if (getattr(current, "category", None) == "caller"
                and getattr(current, "submission_state", "not_sent") == "not_sent"):
            # Repair metadata is trusted only for the known local caller
            # branch. A transport/remote/uncertain failure must never carry a
            # caller action even if an exception happens to expose one.
            repair = _input_repair(getattr(current, "input_repair", None))
            if repair is not None:
                result["input_repair"] = repair
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


def caller_error(message, exception_type=ValueError, *, repair=None):
    """Only known pre-dispatch input checks may use this classification."""
    error = exception_type(message)
    error.category = "caller"
    if repair is not None:
        error.input_repair = repair
    return error
