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


CALLER_ERROR_HINTS = {
    "invalid_argument": "Correct the named argument to the type and range declared in the tool schema, then retry; nothing was sent.",
    "endpoint_incomplete": "Provide host together with port (no implicit port 22), or select a configured endpoint with alias.",
    "endpoint_alias_unknown": "Use an alias from the endpoint alias files that defines host and port, or pass host and port explicitly.",
    "unknown_job": "Use the job_id or session_id returned by a command started in this session. Inspect legacy string IDs with remote_job_status or remote_job_tail.",
    "path_policy": "Use an absolute path on the remote endpoint inside its configured root; local filesystem paths are not valid here.",
    "unsupported_shell_for_runtime_env": "runtime_env needs a POSIX-family shell (bash, sh, dash, zsh, ksh); choose one of these or disable runtime_env.",
}

AUTH_ERROR_HINTS = {
    "authentication": "Configure SSH access with remote_auth using password_env, or run remote-dev auth setup --prompt once.",
    "authentication_rejected": "SSH authentication was rejected. Check the login user and credential, then configure access with remote_auth using password_env.",
    "authentication_timeout": "SSH authentication timed out. Check endpoint connectivity before configuring access again.",
    "host_key": "SSH host-key verification failed. Confirm the server fingerprint with its administrator before updating known_hosts; do not automatically delete trust records or bypass verification.",
    "credential_required": "Provide a password once through remote_auth password_env or remote-dev auth setup --prompt; subsequent calls reuse the shared SSH profile.",
    "credential_unavailable": "The saved SSH credential or profile is unavailable. Configure access again with remote_auth using password_env.",
    "configuration": "Check the SSH host, port, user, auth_ref, identity_file and known_hosts_file; configure the matching endpoint again if needed.",
    "connection_unavailable": "Check endpoint connectivity and SSH host-key trust. Do not bypass host-key verification.",
    "local_recording_failed": "Saving local SSH access state failed. Check the shared SSH state directory before configuring access again.",
}


def safe_auth_error_details(details):
    """Project authentication failures without SSH stderr, paths or credentials."""
    category = details.get("category")
    if not isinstance(category, str) or category not in AUTH_ERROR_HINTS:
        return None
    if (category in {"configuration", "connection_unavailable", "local_recording_failed"}
            and details.get("authentication") is not True
            and not isinstance(details.get("bootstrap"), dict)):
        return None
    public = {"category": category, "hint": AUTH_ERROR_HINTS[category],
              "retryable": details.get("retryable") is True}
    state = details.get("submission_state")
    if state in ("not_sent", "acknowledged", "uncertain"):
        public["submission_state"] = state
    if type(details.get("operation_completed")) is bool:
        public["operation_completed"] = details["operation_completed"]
    bootstrap = details.get("bootstrap")
    if isinstance(bootstrap, dict):
        maintenance = {}
        for field in ("status", "installation", "verification"):
            value = bootstrap.get(field)
            if value in ("completed", "failed", "unknown", "not_sent", "already_configured", "not_requested"):
                maintenance[field] = value
        for field in ("key_verified", "profile_saved", "credential_retained"):
            if type(bootstrap.get(field)) is bool:
                maintenance[field] = bootstrap[field]
        if maintenance:
            public["bootstrap"] = maintenance
    return public


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
        if getattr(current, "authentication", False) is True:
            result["authentication"] = True
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


def caller_error(message, exception_type=ValueError, *, error_code="invalid_argument", field=None, repair=None):
    """Only known pre-dispatch input checks may use this classification."""
    error = exception_type(message)
    error.category = "caller"
    error.error_code = error_code if error_code in CALLER_ERROR_HINTS else "invalid_argument"
    error.field = field
    if repair is not None:
        error.input_repair = repair
    return error
