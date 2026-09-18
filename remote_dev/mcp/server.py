#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import uuid
import threading
import io
from mindie_diagnostics import wrap_context, bind_context
from remote_dev.observability import protocol_streams
from concurrent.futures import ThreadPoolExecutor
from remote_dev.core.cancellation import request_context
from typing import Any

os.environ.setdefault("REMOTE_DEV_SESSION_ID", f"mcp-{os.getpid()}-{uuid.uuid4().hex[:8]}")

from remote_dev import package_version
from remote_dev.mcp.tools import call_tool, canonical_name, list_resources, list_tools, read_resource
from remote_dev.runtime import process_identity, runtime_status

LOADED_RUNTIME = process_identity("remote-dev")
LOADED_VERSION = package_version()
_OUTPUT_LOCK = threading.Lock()
_DISPATCHER = None
_PROTOCOL_WRITER = None


def encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def send(payload: dict[str, Any], *, framed: bool = False) -> None:
    with _OUTPUT_LOCK:
        encoded = encode_payload(payload)
        if _PROTOCOL_WRITER is not None:
            _PROTOCOL_WRITER.write((f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") if framed else b"") + encoded + (b"" if framed else b"\n"))
            _PROTOCOL_WRITER.flush()
            return
        if framed:
            sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
        else:
            sys.stdout.write(encoded.decode("utf-8") + "\n")
            sys.stdout.flush()


def result(request_id: Any, value: dict[str, Any], *, framed: bool = False) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value}, framed=framed)


def error(request_id: Any, code: int, message: str, data: Any | None = None, *, framed: bool = False) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    send(payload, framed=framed)


def tool_text(payload: dict[str, Any]) -> str:
    """Keep failures actionable for clients that only consume MCP text."""
    text = str(payload.get("text") or "")
    details = payload.get("result") or {}
    if details.get("outcome") in {"success", "cancelled"}:
        return text
    status = str(details.get("status") or details.get("outcome") or "failed")
    parts = [f"Remote tool failed ({status})."]
    for value in (details.get("summary"), details.get("error")):
        if value and str(value) not in text:
            parts.append(str(value))
    if text.strip():
        parts.append(text.rstrip())
    return "\n".join(parts) + "\n"


def handle(message: dict[str, Any], *, framed: bool = False) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None and method and method.startswith("notifications/"):
        return
    try:
        if method == "initialize":
            result(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                    },
                    "serverInfo": {"name": "remote-dev", "version": LOADED_VERSION},
                },
                framed=framed,
            )
        elif method == "tools/list":
            result(request_id, {"tools": list_tools()}, framed=framed)
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                raise ValueError("tools/call requires string name")
            if not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be an object")
            with bind_context((params.get("_meta") or {}).get("mindie_diagnostics") or {}):
                payload = call_tool(name, arguments)
            payload["result"]["runtime"] = runtime_status(LOADED_RUNTIME)
            result(
                request_id,
                {
                    "content": [{"type": "text", "text": tool_text(payload)}],
                    "structuredContent": payload.get("result", {}),
                    "isError": payload.get("result", {}).get("outcome") not in {"success", "cancelled"},
                },
                framed=framed,
            )
        elif method == "resources/list":
            result(request_id, {"resources": list_resources()}, framed=framed)
        elif method == "resources/read":
            content = read_resource(str(params.get("uri", "remote://endpoints")))
            result(request_id, {"contents": [content]}, framed=framed)
        else:
            error(request_id, -32601, f"method not found: {method}", framed=framed)
    except Exception as exc:  # noqa: BLE001
        from remote_dev.core.errors import error_details
        error(request_id, -32000, str(exc), {**error_details(exc),
              **({"diagnostics": exc.diagnostics} if hasattr(exc, "diagnostics") else {})}, framed=framed)


class Dispatcher:
    """Bounded request workers; the input reader always remains cancellable."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.capacity = threading.BoundedSemaphore(32)
        self.control_executor = ThreadPoolExecutor(max_workers=2)
        self.control_capacity = threading.BoundedSemaphore(8)
        self.lock = threading.Lock()
        self.pending = {}

    def dispatch(self, message, framed=False):
        if message.get("method") == "notifications/cancelled":
            identifier = (message.get("params") or {}).get("requestId")
            with self.lock:
                event = self.pending.get(identifier)
                if event is not None:
                    event.set()
            return
        if message.get("method") not in {"tools/call", "resources/read"}:
            handle(message, framed=framed)
            return
        identifier = message.get("id")
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}
        name = params.get("name")
        name = canonical_name(name) if isinstance(name, str) else None
        short_control = name in {"remote.job_status", "remote.job_tail", "remote.job_stop"}
        if name == "remote.job_stdin":
            delay = arguments.get("yield_time_ms")
            if delay is None:
                delay = 250 if arguments.get("chars") or arguments.get("eof") else 1000
            short_control = type(delay) in (int, float) and 0 <= delay <= 250
        capacity = self.control_capacity if short_control else self.capacity
        executor = self.control_executor if short_control else self.executor
        with self.lock:
            if identifier in self.pending:
                error(identifier, -32600, "request id is already running", framed=framed)
                return
            if not capacity.acquire(blocking=False):
                error(identifier, -32000, "request capacity exhausted; not executed", framed=framed)
                return
            event = threading.Event()
            self.pending[identifier] = event
        def execute():
            try:
                with request_context(event):
                    if event.is_set():
                        error(identifier, -32800, "request cancelled before execution", framed=framed)
                    else:
                        handle(message, framed=framed)
            finally:
                with self.lock:
                    self.pending.pop(identifier, None)
                capacity.release()
        executor.submit(wrap_context(execute))

    def close(self):
        with self.lock:
            for event in self.pending.values():
                event.set()
        self.executor.shutdown(wait=True)
        self.control_executor.shutdown(wait=True)


def dispatch(message, framed=False):
    if _DISPATCHER is None:
        handle(message, framed=framed)
    else:
        _DISPATCHER.dispatch(message, framed)


def read_framed_messages() -> int:
    while True:
        headers: dict[str, str] = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return 0
            if line in {b"\r\n", b"\n"}:
                break
            text = line.decode("ascii", errors="replace").strip()
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.lower()] = value.strip()
        length_text = headers.get("content-length")
        if not length_text:
            error(None, -32600, "missing Content-Length header", framed=True)
            continue
        body = sys.stdin.buffer.read(int(length_text))
        try:
            message = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            error(None, -32700, f"parse error: {exc}", framed=True)
            continue
        if isinstance(message, dict):
            dispatch(message, framed=True)
        else:
            error(None, -32600, "request must be an object", framed=True)


def read_line_messages() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            error(None, -32700, f"parse error: {exc}")
            continue
        if isinstance(message, dict):
            dispatch(message)
        else:
            error(None, -32600, "request must be an object")
    return 0


def main() -> int:
    global _PROTOCOL_WRITER
    with protocol_streams() as (reader, writer):
        original = sys.stdin
        sys.stdin = io.TextIOWrapper(reader, encoding="utf-8")
        _PROTOCOL_WRITER = writer
        try:
            return _serve()
        finally:
            sys.stdin.detach()
            sys.stdin = original
            _PROTOCOL_WRITER = None


def _serve() -> int:
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        peeked = sys.stdin.buffer.peek(16)
    except AttributeError:
        peeked = b""
    global _DISPATCHER
    _DISPATCHER = Dispatcher()
    try:
        if peeked.startswith(b"Content-Length:"):
            return read_framed_messages()
        return read_line_messages()
    finally:
        _DISPATCHER.close()
        from remote_dev.core.rpc_transport import close_connections
        close_connections()
        _DISPATCHER = None


if __name__ == "__main__":
    raise SystemExit(main())
