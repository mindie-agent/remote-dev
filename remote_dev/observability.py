"""
[Role] Structured operation span adapter over `mindie_diagnostics` and MCP stdio descriptor guard (`observability.py`).
[Invariants]
- Logs only operation names and public outcome metadata (`outcome`, `status`, `state`, `job_id`, `duration_ms`); never inspects or logs user commands, environment variables, or file contents.
- Authentication-maintenance facts stay in this lightweight context; local-only calls never load SSH authentication or transports.
- `protocol_streams()` duplicates and redirects `stdin`/`stdout` before serving MCP so child processes or stray library prints cannot corrupt the JSON-RPC wire stream.
[Timeouts & Locks]
- Uses `contextvars.ContextVar` for per-request span correlation and non-blocking `stderr` warning emission.
[Side-Effect Policy]
- Emits structured diagnostic events to `mindie_diagnostics` and attaches `diagnostics` summaries to tool results.
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
from pathlib import Path

from mindie_diagnostics import get_recorder, current_context, bind_context, wrap_context
from remote_dev.core.errors import RemoteDevError, error_details

_SOURCE_ROOT = Path(__file__).resolve().parent
_tool = contextvars.ContextVar("remote_dev_tool_operation", default=None)
_authentication_event = contextvars.ContextVar("remote_dev_auth_maintenance", default=None)

_REPORT_OPERATIONS = frozenset({
    "remote.read", "remote.write", "remote.edit", "remote.multi_edit", "remote.bash",
    "remote.glob", "remote.grep", "remote.ls", "remote.apply_patch", "remote.job_status",
    "remote.job_tail", "remote.job_output", "remote.job_stop", "remote.job_stdin", "remote.artifact_manifest",
    "remote.artifact_pull", "remote.artifact_push", "remote.context_snapshot",
    "remote.probe", "remote.python",
})
_COORDINATOR_OPERATIONS = frozenset({
    "mindie.session", "mindie.run", "mindie.execution", "mindie.finish",
    "mindie.message", "mindie.hosts", "mindie.probe", "mindie.container", "mindie.lease",
})
_REPORT_STAGES = frozenset({"tool_call", "protocol_decode", "state_contract", "receipt_contract"})
_REPORT_CATEGORIES = frozenset({"internal_exception", "command_protocol", "contract_violation"})
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
        if seen_count > 32:
            return False
        if getattr(current, "category", None) is not None or getattr(current, "_mindie_caller_error", False):
            return False
        if isinstance(current, _NOT_INTERNAL):
            return False
        for cls in type(current).__mro__:
            if cls.__name__ in {
                "CoordinationError",
                "ManagedTaskContextRequired",
                "MachineDirectoryUnavailable",
                "HostQueueUnavailable",
            }:
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
    """Optional static warning; never change shared stderr flags or block."""
    global _failure_warning_sent
    if _failure_warning_sent:
        return
    _failure_warning_sent = True
    try:
        if os.name == "posix":
            descriptor = sys.stderr.fileno()
            if not os.get_blocking(descriptor):
                os.write(descriptor, b"remote-dev: shared failure report unavailable\n")
    except Exception:
        pass


def _remember_diagnostic(reference):
    active = current_tool()
    if not isinstance(active, dict):
        return
    active["diagnostic"] = reference
    active["_failure_anchor"] = _FailureAnchor(reference)


def confirmed_failure(
    operation,
    *,
    stage,
    category,
    exception=None,
    reason_code=None,
    contract_name=None,
    component="remote-dev",
    source_root=None,
):
    """Report one confirmed internal failure. Unknown stage/category is not sent."""
    if component == "mindie-coordinator":
        if not isinstance(operation, str) or operation not in _COORDINATOR_OPERATIONS:
            operation = "mindie.tool"
    elif not isinstance(operation, str) or operation not in _REPORT_OPERATIONS:
        operation = "remote.tool"
    if not isinstance(stage, str) or not isinstance(category, str) or stage not in _REPORT_STAGES or category not in _REPORT_CATEGORIES:
        return None
    if exception is not None:
        seen_chain: set[int] = set()
        pending_chain = [exception]
        while pending_chain and len(seen_chain) < 32:
            cur_exc = pending_chain.pop(0)
            if cur_exc is None or id(cur_exc) in seen_chain:
                continue
            seen_chain.add(id(cur_exc))
            anchor = getattr(cur_exc, "_mindie_failure_anchor", None)
            if type(anchor) is _FailureAnchor:
                try:
                    exception._mindie_failure_anchor = anchor
                    exception.mindie_diagnostic = anchor.diagnostic
                except Exception:
                    pass
                _remember_diagnostic(anchor.diagnostic)
                return anchor.diagnostic
            if component == "mindie-coordinator":
                coord_marker = getattr(cur_exc, "_mindie_coordinator_diag", None)
                if coord_marker is not None:
                    try:
                        from mindie_coordinator.diagnostics import _CoordinatorFailureMarker
                        if type(coord_marker) is _CoordinatorFailureMarker and isinstance(coord_marker.result, dict):
                            inc = coord_marker.result.get("incident_id")
                            ref = (
                                {"incident_id": inc, "logging_failed": bool(coord_marker.result.get("logging_failed", False))}
                                if isinstance(inc, str) and len(inc) == 32 and all(c in _HEX for c in inc)
                                else {"logging_failed": True}
                            )
                            exception._mindie_failure_anchor = _FailureAnchor(ref)
                            exception.mindie_diagnostic = ref
                            _remember_diagnostic(ref)
                            return ref
                    except Exception:
                        pass
            if getattr(cur_exc, "__cause__", None) is not None:
                pending_chain.append(cur_exc.__cause__)
            if getattr(cur_exc, "__context__", None) is not None and not getattr(cur_exc, "__suppress_context__", False):
                pending_chain.append(cur_exc.__context__)
    reported = None
    effective_root = source_root if source_root is not None else (_SOURCE_ROOT if component == "remote-dev" else None)
    if effective_root is None and component == "mindie-coordinator":
        try:
            from mindie_coordinator.diagnostics import _SOURCE_ROOT as _coord_root
            effective_root = _coord_root
        except Exception:
            pass
    try:
        from mindie_diagnostics.integration import record_failure
        active = current_tool()
        elapsed_ms = (time.monotonic() - active["started"]) * 1000 if active else None
        extra_kwargs = {}
        if effective_root is not None:
            extra_kwargs["source_root"] = effective_root
        if reason_code is not None:
            extra_kwargs["reason_code"] = reason_code
        if contract_name is not None:
            extra_kwargs["contract_name"] = contract_name
        try:
            reported = record_failure(
                component, operation, stage=stage, category=category, exception=exception,
                elapsed_ms=elapsed_ms, **extra_kwargs,
            )
        except TypeError:
            reported = record_failure(
                component, operation, stage=stage, category=category, exception=exception,
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


def record_contract_violation(
    operation,
    *,
    stage="state_contract",
    contract_name,
    reason_code,
    exception=None,
    component="remote-dev",
    source_root=None,
):
    """Record a deterministic local state or protocol contract violation."""
    return confirmed_failure(
        operation,
        stage=stage,
        category="contract_violation",
        exception=exception,
        reason_code=reason_code,
        contract_name=contract_name,
        component=component,
        source_root=source_root,
    )


def current_tool():
    return _tool.get()


@contextlib.contextmanager
def detached_tool():
    """Background work keeps trace correlation, not a live caller's result scope."""
    token = _tool.set(None)
    auth_token = _authentication_event.set(None)
    try:
        yield
    finally:
        _authentication_event.reset(auth_token)
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
                auth_token = _authentication_event.set(None)
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
                        if component in {"remote-dev", "mindie-coordinator"} and _internal_exception(exc):
                            confirmed_failure(
                                label,
                                stage="tool_call",
                                category="internal_exception",
                                exception=exc,
                                component=component,
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
                    authentication = _authentication_event.get()
                    _authentication_event.reset(auth_token)
                    if authentication is not None and previous is not None:
                        _authentication_event.set(authentication)
                    _tool.reset(token)
            minimal_result = bool(isinstance(value, dict) and value.pop("_native_minimal", False))
            if isinstance(result, dict) and authentication is not None:
                result["authentication"] = authentication
                if authentication.get("receipt_unconfirmed") is True:
                    result["receipt_warning"] = authentication["receipt_warning"]
                    result["receipt_unconfirmed"] = True
                bootstrap = authentication.get("bootstrap") or {}
                if bootstrap.get("status") in {"failed", "unknown"}:
                    warning = "SSH password login succeeded; public-key maintenance " + bootstrap["status"] + "."
                    result.setdefault("warnings", []).append(warning)
                    if isinstance(value, dict) and "text" in value:
                        value["text"] = str(value["text"]) + "\n" + warning + "\n"
                if isinstance(value, dict) and value.get("structuredContent") is result:
                    value["content"] = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
            if isinstance(result, dict) and not minimal_result:
                if operation.get("cleanup_errors"):
                    result["cleanup_errors"] = operation["cleanup_errors"]
                    warning = "Prior idle connection cleanup failed: " + "; ".join(operation["cleanup_errors"])
                    result.setdefault("warnings", []).append(warning)
                    if isinstance(value, dict) and "text" in value:
                        value["text"] = str(value["text"]) + "\n" + warning + "\n"
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
