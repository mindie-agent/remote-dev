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
from remote_dev.core.errors import error_details

_tool = contextvars.ContextVar("remote_dev_tool_operation", default=None)


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
            with get_recorder(component).operation(label) as op:
                token = _tool.set({"name": label, "op": op, "started_at": started_at, "started": started})
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
