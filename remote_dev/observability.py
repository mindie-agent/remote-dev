"""Thin result adapters over the shared diagnostics recorder.

Only operation names and already-public outcome facts enter logs. Arguments,
commands, environment values and file contents are deliberately not inspected.
"""
from __future__ import annotations

import contextvars
import contextlib
import functools
import json
import time
import os
import sys
from datetime import datetime, timezone

from mindie_diagnostics import get_recorder, current_context, bind_context, wrap_context
from remote_dev.core.errors import RemoteDevError, error_details

_tool = contextvars.ContextVar("remote_dev_tool_operation", default=None)

_REPORT_OPERATIONS = frozenset({
    "remote.read", "remote.write", "remote.edit", "remote.multi_edit", "remote.bash",
    "remote.glob", "remote.grep", "remote.ls", "remote.apply_patch", "remote.job_status",
    "remote.job_tail", "remote.job_stop", "remote.job_stdin", "remote.artifact_manifest",
    "remote.artifact_pull", "remote.artifact_push", "remote.context_snapshot",
    "remote.probe", "remote.python",
})
_REPORT_STAGES = frozenset({"tool_call", "protocol_decode"})
_REPORT_CATEGORIES = frozenset({"internal_exception", "command_protocol"})
_EXACT_INTERNAL = (RuntimeError, AssertionError, KeyError, AttributeError, IndexError, ZeroDivisionError)
_NOT_INTERNAL = (RemoteDevError, ValueError, TypeError, OSError, KeyboardInterrupt, SystemExit, GeneratorExit)
_HEX = frozenset("0123456789abcdef")
_failure_warning_sent = False


class _FailureAnchor:
    """Private proof that this process recorded the attached reference."""

    __slots__ = ("diagnostic",)

    def __init__(self, diagnostic):
        self.diagnostic = diagnostic


def _internal_exception(exc):
    """True only for an exact outer internal type whose chain stays unmarked."""
    seen = set()
    pending = [exc]
    seen_count = 0
    while pending:
        current = pending.pop()
        if current is None:
            continue
        marker = id(current)
        if marker in seen:
            return False
        seen.add(marker)
        seen_count += 1
        if seen_count > 8:
            return False
        if getattr(current, "category", None) is not None:
            return False
        if isinstance(current, _NOT_INTERNAL):
            return False
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            pending.append(cause)
        if context is not None and not getattr(current, "__suppress_context__", False):
            pending.append(context)
    return type(exc) in _EXACT_INTERNAL


def _report_output_valid(value):
    if not isinstance(value, dict):
        return False
    incident = value.get("incident_id")
    logging_failed = value.get("logging_failed")
    recorded = value.get("recorded")
    if type(logging_failed) is not bool or recorded is not True:
        return False
    return (
        type(incident) is str
        and len(incident) == 32
        and all(character in _HEX for character in incident)
    )


def _warn_report_unavailable():
    """One static stderr line, and only when stderr can accept it without blocking."""
    global _failure_warning_sent
    if _failure_warning_sent:
        return
    try:
        import select
        descriptor = sys.stderr.fileno()
        _, writable, _ = select.select([], [descriptor], [], 0)
    except Exception:
        return
    if descriptor not in writable:
        return
    _failure_warning_sent = True
    try:
        os.write(descriptor, b"remote-dev: shared failure report unavailable\n")
    except Exception:
        pass


def _remember_diagnostic(reference):
    active = current_tool()
    if not isinstance(active, dict):
        return
    active["diagnostic"] = reference
    active["_failure_anchor"] = _FailureAnchor(reference)


def confirmed_failure(operation, *, stage, category, exception=None):
    """Report one confirmed internal failure. Unknown stage/category is not sent."""
    if not isinstance(operation, str) or operation not in _REPORT_OPERATIONS:
        operation = "remote.tool"
    if not isinstance(stage, str) or not isinstance(category, str) or stage not in _REPORT_STAGES or category not in _REPORT_CATEGORIES:
        return None
    if exception is not None:
        anchor = getattr(exception, "_mindie_failure_anchor", None)
        if type(anchor) is _FailureAnchor:
            _remember_diagnostic(anchor.diagnostic)
            return anchor.diagnostic
    reported = None
    try:
        from mindie_diagnostics.integration import record_failure
        active = current_tool()
        elapsed_ms = (time.monotonic() - active["started"]) * 1000 if active else None
        reported = record_failure(
            "remote-dev", operation, stage=stage, category=category, exception=exception,
            elapsed_ms=elapsed_ms,
        )
        valid = _report_output_valid(reported)
    except Exception:
        valid = False
    if valid:
        reference = {"incident_id": reported["incident_id"], "logging_failed": reported["logging_failed"]}
    else:
        _warn_report_unavailable()
        reference = {"logging_failed": True}
    if exception is not None:
        try:
            exception.mindie_diagnostic = reference
        except Exception:
            pass
        try:
            exception._mindie_failure_anchor = _FailureAnchor(reference)
        except Exception:
            pass
    _remember_diagnostic(reference)
    return reference


def current_tool():
    return _tool.get()


@contextlib.contextmanager
def detached_tool():
    """Background work keeps trace correlation, not a live caller's result scope."""
    token = _tool.set(None)
    try:
        yield
    finally:
        _tool.reset(token)


def observed_tool(name, *, component="remote-dev"):
    """Observe both direct SDK and CLI/MCP calls, without duplicate tool spans."""
    def decorate(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            label = name(*args, **kwargs) if callable(name) else name
            previous = current_tool()
            if previous is not None and previous["name"] == label:
                return function(*args, **kwargs)
            started_at = datetime.now(timezone.utc).isoformat()
            started = time.monotonic()
            operation = {"name": label, "started_at": started_at, "started": started}
            with get_recorder(component).operation(label) as op:
                operation["op"] = op
                token = _tool.set(operation)
                try:
                    value = function(*args, **kwargs)
                    result = value.get("result", value.get("structuredContent", value)) if isinstance(value, dict) else None
                    if isinstance(result, dict):
                        op.event("DEBUG", "tool.result", **{
                            key: result[key] for key in ("outcome", "status", "state", "execution_id", "job_id", "resources_released", "quiet") if key in result})
                        outcome = result.get("outcome", result.get("state"))
                        if outcome == "needs_input":
                            op.event("WARNING", "tool.needs_input", outcome=outcome)
                        elif outcome == "cancelled":
                            op.event("WARNING", "tool.cancelled", category="cancelled", outcome=outcome)
                        elif outcome in {"failed", "blocked", "timeout"}:
                            error = result.get("error_details")
                            if not error and isinstance(result.get("data"), dict):
                                error = result["data"].get("error_details")
                            error = error if isinstance(error, dict) else {}
                            op.fail(error.get("category", "tool_result"), retryable=error.get("retryable", False),
                                    submission_state=error.get("submission_state"), outcome=outcome,
                                    error_type=error.get("type"), error_code=error.get("error_code"))
                except BaseException as exc:
                    if isinstance(exc, SystemExit) and exc.code in (None, 0):
                        raise
                    try:
                        if component == "remote-dev" and _internal_exception(exc):
                            confirmed_failure(
                                label, stage="tool_call", category="internal_exception", exception=exc,
                            )
                    except Exception:
                        _warn_report_unavailable()
                    details = error_details(exc)
                    op.fail(details.pop("category"), **details)
                    # The original exception type/cause stays intact. A JSON-RPC
                    # error can still point to the operation that failed.
                    try:
                        exc.diagnostics = {"operation_id": op.operation_id, "trace_id": op.trace_id,
                                           "record_ref": op.recorder.record_ref}
                        exc._diagnostic_operation = op
                    except Exception:
                        pass
                    raise
                finally:
                    _tool.reset(token)
            if isinstance(result, dict):
                anchor = operation.get("_failure_anchor")
                diagnostic = operation.get("diagnostic")
                if type(anchor) is _FailureAnchor and anchor.diagnostic is diagnostic:
                    result["diagnostic"] = diagnostic
                result["diagnostics"] = op.summary()
                if "schema_version" in result and "tool" in result:
                    result["invocation_id"] = op.operation_id
                    result["started_at"] = started_at
                    result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                if isinstance(value, dict) and value.get("structuredContent") is result:
                    value["content"] = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
            return value
        return call
    return decorate


def event(level, name, *, component="remote-dev", **attributes):
    """Short standalone diagnostic; callers may supply identifiers, never payloads."""
    get_recorder(component).event(level, name, **attributes)


def observed_operation(name, *, component="remote-dev", level="INFO"):
    """Internal spans have their own lifetime; never rewrite a tool result."""
    def decorate(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            label = name(*args, **kwargs) if callable(name) else name
            active = current_tool()
            parent = active["op"] if active and active["op"].recorder.component == component and active["op"].finished_at is None else None
            scope = parent.phase(label, level=level) if parent else get_recorder(component).operation(label, level=level)
            with scope as op:
                try:
                    return function(*args, **kwargs)
                except BaseException as exc:
                    if isinstance(exc, SystemExit) and exc.code in (None, 0):
                        raise
                    details = error_details(exc)
                    op.fail(details.pop("category"), **details)
                    raise
        return call
    return decorate


@contextlib.contextmanager
def protocol_streams():
    """Reserve protocol descriptors before logging or children can use stdio."""
    with os.fdopen(os.dup(sys.stdin.fileno()), "rb") as reader, \
            os.fdopen(os.dup(sys.stdout.fileno()), "wb") as writer:
        with open(os.devnull, "rb") as empty:
            os.dup2(empty.fileno(), sys.stdin.fileno())
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        yield reader, writer
