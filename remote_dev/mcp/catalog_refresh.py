"""Live catalogs for a stable native MCP connection.

[Role] Observe the existing selected-runtime pointer and invalidate host catalogs.
[Invariants] Selection has one authority; notifications never replay tool calls.
[Timeouts & Locks] One 1s watcher, serialized notifications, bounded owned helpers.
[Side-Effect Policy] Reads bindings and runs exact selected helpers; no activation,
task attachment, network discovery, or termination of another process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from remote_dev.core.local_process import OwnedProcess

LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def profile_selection(config, scripts, surface, *, config_env, plugin_env):
    """Select the interpreter and payload from the adapter's atomic profile."""
    config, scripts = Path(config), Path(scripts)
    value = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    python = value.get("python") or sys.executable
    scripts = Path(value.get("runtime_scripts") or scripts)
    expected = (value.get("product_validation") or {}).get("native_version")
    if expected is not None:
        manifest = json.loads((scripts.parent / "plugin.json").read_text(encoding="utf-8"))
        if manifest.get("version") != expected:
            raise ValueError("selected MCP payload differs from the recorded native version")
    return [python, "-I", str(scripts / "mcp_server.py"), surface, "--once"], {
        config_env: str(config), plugin_env: str(scripts.parent), "PYTHONIOENCODING": "utf-8",
    }


def file_revision(*paths):
    """Atomic bindings and in-place development catalogs both invalidate caches."""
    result = []
    for path in paths:
        path = Path(path)
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            result.append((str(path), None, None))
    return tuple(result)


class CatalogRefresh:
    """A connection-local invalidation signal, never a second runtime selector."""

    def __init__(self, revision, send, *, interval=1.0):
        self.revision, self.send, self.interval = revision, send, interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._seen = revision()
        self._warned = False

    def check(self):
        # Run before publishing a tool response too: native hosts can consume
        # the invalidation before their next inference request in the same turn.
        try:
            with self._lock:
                current = self.revision()
                if current != self._seen:
                    self.send(LIST_CHANGED)
                    self._seen = current
                self._warned = False
        except Exception as exc:
            if not self._warned:
                print(f"MindIE catalog refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                self._warned = True
            return False
        return True

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="mindie-catalog", daemon=True)
            self._thread.start()

    def _watch(self):
        while not self._stop.wait(self.interval):
            self.check()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def selected_rpc(argv, message, *, env=None, timeout=660.0, cancel=None, max_output=32 * 1024 * 1024):
    """One bounded stdio exchange with the selected complete helper generation."""
    # The caller's envelope includes the owned-tree drain, rather than adding
    # another five seconds after the native MCP deadline has already elapsed.
    deadline = time.monotonic() + max(.001, timeout - 5.0)
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        source.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        source.seek(0)
        owner = OwnedProcess(argv, env=env, stdin=source, stdout=out, stderr=err)
        process = owner.process
        response = None
        primary = None
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("selected MCP helper cancelled; no automatic replay")
                if time.monotonic() >= deadline:
                    raise TimeoutError("selected MCP helper timed out; no automatic replay")
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_output:
                    raise ValueError("selected MCP helper output exceeded its capture limit")
                try:
                    process.wait(timeout=min(.05, max(.001, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_output:
                raise ValueError("selected MCP helper output exceeded its capture limit")
            out.seek(0)
            err.seek(0)
            if process.returncode:
                raise RuntimeError("selected MCP helper failed: " + err.read(2000).decode("utf-8", errors="replace"))
            response = json.loads(out.read(max_output))
            if not isinstance(response, dict) or response.get("id") != message.get("id"):
                raise ValueError("selected MCP helper response identity differs")
            return response
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                owner.stop(timeout=5.0)
            except Exception as exc:
                if primary is not None:
                    primary.add_note("Selected helper cleanup failed: " + str(exc))
                elif isinstance(response, dict) and isinstance(response.get("result"), dict):
                    result = response["result"]
                    result.setdefault("structuredContent", {}).update(
                        receipt_warning="Selected helper cleanup failed: " + str(exc),
                        receipt_unconfirmed=True,
                    )
                else:
                    raise


def serve_selected(select, *, reader=None, writer=None):
    """Stable facade for adapters whose helper already processes one request.

    select() returns (argv, env) from one existing committed binding. Requests
    keep that selection until completion; pointer changes only affect new calls.
    """
    reader, writer = reader or sys.stdin, writer or sys.stdout
    output_lock = threading.Lock()
    cancellation = threading.Event()
    worker = None
    request_id = None

    def send(value):
        with output_lock:
            writer.write(json.dumps(value, ensure_ascii=False) + "\n")
            writer.flush()

    def revision():
        argv, env = select()
        return tuple(argv), tuple(sorted((env or {}).items()))

    refresh = CatalogRefresh(revision, send)

    def dispatch(message, cancel):
        stage = "select_runtime"
        try:
            argv, env = select()
            stage = "dispatch_helper"
            response = selected_rpc(
                argv, message, env=env, cancel=cancel,
                timeout=660 if message.get("method") == "tools/call" else 15,
            )
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": message["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": str(exc)}],
                "structuredContent": {
                    "error_type": type(exc).__name__, "automatic_retry": False,
                    "stage": stage,
                    "status": "cancelled" if isinstance(exc, InterruptedError) else "helper_failed",
                    "execution": ("outcome_unconfirmed" if stage == "dispatch_helper"
                                  and message.get("method") == "tools/call" else "not_started"),
                },
            }}
        refresh.check()
        send(response)

    try:
        for line in reader:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("MCP request must be an object")
            except ValueError as exc:
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
                continue
            method = message.get("method")
            if method == "notifications/cancelled":
                if (message.get("params") or {}).get("requestId") == request_id:
                    cancellation.set()
                continue
            if "id" not in message:
                continue
            if method == "initialize":
                argv, env = select()
                response = selected_rpc(argv, message, env=env, timeout=15)
                response["result"].setdefault("capabilities", {})["tools"] = {"listChanged": True}
                send(response)
                refresh.start()
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            elif method == "tools/call":
                if worker is not None and worker.is_alive():
                    send({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32000, "message": "component call already running; not executed"}})
                    continue
                cancellation, request_id = threading.Event(), message["id"]
                worker = threading.Thread(target=dispatch, args=(message, cancellation), daemon=True)
                worker.start()
            else:
                dispatch(message, None)
    finally:
        refresh.close()
        cancellation.set()
        if worker is not None:
            worker.join(timeout=6)
            if worker.is_alive():
                raise TimeoutError("selected MCP call did not drain after cancellation")
    return 0
