"""Live catalogs and concurrent resident dispatch for a stable native MCP connection.

[Role]
Observes the committed selected-runtime pointer, emits `notifications/tools/list_changed`,
and dispatches concurrent MCP requests across warm resident helper workers (or an
in-process handler) without cold per-call interpreter startups or single-call blocking.

[Invariants]
- Selection has one authority; pointer changes affect only newly dispatched calls while
  in-flight calls finish on their selected generation without replay or cancellation.
- Concurrent `tools/call` requests (e.g., foreground `remote_bash` alongside
  `remote_job_status` / `remote_read` / `notifications/cancelled`) execute in parallel
  across separate work and control thread pools without `-32000` single-flight rejection.

[Timeouts & Locks]
- One `1s` catalog watcher, serialized wire writes, `660s` tool budget (`15s` metadata
  budget), and `5s` helper stop drain (`outer_timeout >= inner + cleanup_grace`).

[Side-Effect Policy]
- Reads committed bindings and pools owned helper processes; closes idle superseded
  workers and drains all owned processes on transport EOF.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

from remote_dev.core.cancellation import request_context
from remote_dev.core.local_process import OwnedProcess

LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
_CONTROL_TOOL_SUFFIXES = (
    "job_status", "job_tail", "job_output", "job_stop", "job_list", "job_kill",
    "taskoutput", "taskstop", "tasklist",
    "read", "ls", "glob", "grep", "probe", "stat", "status",
)
_MAX_IDLE_PER_KEY = 6


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
    deadline = time.monotonic() + max(0.001, timeout - 5.0)
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
                    process.wait(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
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


def _resident_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Map helper argv onto a persistent line-delimited stdio command when supported."""
    items = [str(part) for part in argv]
    if "--once" not in items:
        return items, True
    filtered = [part for part in items if part != "--once"]
    for idx in range(1, len(filtered)):
        candidate = filtered[idx]
        if candidate.startswith("-"):
            continue
        if Path(candidate).name == "mcp_server.py" and idx + 1 < len(filtered):
            prefix = filtered[:idx]
            surface = filtered[idx + 1]
            helper = str(Path(__file__).with_name("_resident_helper.py").resolve())
            return [*prefix, helper, candidate, surface], True
        break
    return items, False


class _ResidentWorker:
    """One warm persistent stdio helper process bound to a specific generation."""

    def __init__(self, argv: list[str], env: dict[str, str] | None):
        self.owner = OwnedProcess(
            argv,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.process = self.owner.process
        self._replies: queue.Queue[bytes | BaseException | None] = queue.Queue()
        self._stderr_tail = bytearray()
        self.usable = True
        self._closed = False
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline()
                if not line:
                    self._replies.put(None)
                    return
                self._replies.put(line)
        except BaseException as exc:  # pragma: no cover - defensive pipe error propagation
            self._replies.put(exc)

    def _read_stderr(self) -> None:
        try:
            assert self.process.stderr is not None
            while True:
                chunk = self.process.stderr.read1(1024) if hasattr(self.process.stderr, "read1") else self.process.stderr.read(1024)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                del self._stderr_tail[:-2000]
        except OSError:
            return

    def is_alive(self) -> bool:
        return self.usable and not self._closed and self.process.poll() is None

    def exchange(
        self,
        message: dict[str, Any],
        *,
        timeout: float = 660.0,
        cancel: threading.Event | None = None,
        max_output: int = 32 * 1024 * 1024,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.001, timeout - 5.0)
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        response: dict[str, Any] | None = None
        primary: BaseException | None = None
        try:
            if cancel is not None and cancel.is_set():
                err = InterruptedError("MCP request cancelled before dispatch")
                err.not_sent = True  # type: ignore[attr-defined]
                raise err
            if self.process.stdin is None or self.process.poll() is not None:
                self.usable = False
                raise RuntimeError("selected MCP helper exited before request dispatch")
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            while True:
                if cancel is not None and cancel.is_set():
                    self.usable = False
                    raise InterruptedError("selected MCP helper cancelled; no automatic replay")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.usable = False
                    raise TimeoutError("selected MCP helper timed out; no automatic replay")
                try:
                    item = self._replies.get(timeout=min(0.05, max(0.001, remaining)))
                except queue.Empty:
                    if self.process.poll() is not None and self._replies.empty():
                        self.usable = False
                        detail = self._stderr_tail.decode("utf-8", errors="replace")
                        raise RuntimeError(f"selected MCP helper failed: {detail}")
                    continue
                if item is None:
                    self.usable = False
                    detail = self._stderr_tail.decode("utf-8", errors="replace")
                    raise RuntimeError(f"selected MCP helper closed stdout: {detail}")
                if isinstance(item, BaseException):
                    self.usable = False
                    raise RuntimeError(f"selected MCP helper read failed: {item}") from item
                if len(item) > max_output:
                    self.usable = False
                    raise ValueError("selected MCP helper output exceeded its capture limit")
                if not item.strip():
                    continue
                parsed = json.loads(item.decode("utf-8"))
                if not isinstance(parsed, dict):
                    self.usable = False
                    raise ValueError("selected MCP helper returned a non-object frame")
                if "id" not in parsed and isinstance(parsed.get("method"), str):
                    # Ignore helper-local notifications; the outer CatalogRefresh owns notifications.
                    continue
                if parsed.get("id") != message.get("id"):
                    self.usable = False
                    raise ValueError("selected MCP helper response identity differs")
                response = parsed
                return response
        except BaseException as exc:
            primary = exc
            self.usable = False
            raise
        finally:
            if not self.usable:
                try:
                    self.close(timeout=5.0)
                except Exception as exc:
                    if primary is not None:
                        primary.add_note("Selected helper cleanup failed: " + str(exc))
                    elif isinstance(response, dict) and isinstance(response.get("result"), dict):
                        res = response["result"]
                        res.setdefault("structuredContent", {}).update(
                            receipt_warning="Selected helper cleanup failed: " + str(exc),
                            receipt_unconfirmed=True,
                        )
                    else:
                        raise

    def close(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.usable = False
        with contextlib.suppress(OSError, ValueError):
            if self.process.stdin is not None:
                self.process.stdin.close()
        self.owner.stop(timeout=timeout)
        self._stdout_thread.join(timeout=0.5)
        self._stderr_thread.join(timeout=0.5)


class _ResidentPool:
    """Warm worker pool keyed by `(tuple(argv), tuple(sorted(env.items())))`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple[Any, ...], list[_ResidentWorker]] = {}
        self._active_key: tuple[Any, ...] | None = None
        self._closed = False

    def call(
        self,
        argv: list[str],
        env: dict[str, str] | None,
        message: dict[str, Any],
        *,
        timeout: float,
        cancel: threading.Event | None,
    ) -> dict[str, Any]:
        resident_cmd, supports_resident = _resident_argv(argv)
        if not supports_resident:
            return selected_rpc(argv, message, env=env, timeout=timeout, cancel=cancel)
        key = (tuple(resident_cmd), tuple(sorted((env or {}).items())))
        stale: list[_ResidentWorker] = []
        worker: _ResidentWorker | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP helper pool is shutting down")
            if self._active_key is not None and self._active_key != key:
                for old_key in [k for k in list(self._idle) if k != key]:
                    stale.extend(self._idle.pop(old_key, ()))
            self._active_key = key
            bucket = self._idle.get(key) or []
            while bucket:
                candidate = bucket.pop()
                if candidate.is_alive():
                    worker = candidate
                    break
                stale.append(candidate)
        for old in stale:
            with contextlib.suppress(Exception):
                old.close(timeout=2.0)
        if worker is None:
            worker = _ResidentWorker(resident_cmd, env)
        try:
            return worker.exchange(message, timeout=timeout, cancel=cancel)
        finally:
            retire = False
            with self._lock:
                if self._closed or not worker.is_alive() or self._active_key != key:
                    retire = True
                else:
                    bucket = self._idle.setdefault(key, [])
                    if len(bucket) >= _MAX_IDLE_PER_KEY:
                        retire = True
                    else:
                        bucket.append(worker)
            if retire:
                with contextlib.suppress(Exception):
                    worker.close(timeout=2.0)

    def retire_superseded(self, current_key: tuple[Any, ...]) -> None:
        stale: list[_ResidentWorker] = []
        with self._lock:
            self._active_key = current_key
            for old_key in [k for k in list(self._idle) if k != current_key]:
                stale.extend(self._idle.pop(old_key, ()))
        for old in stale:
            with contextlib.suppress(Exception):
                old.close(timeout=2.0)

    def close(self) -> None:
        stale: list[_ResidentWorker] = []
        with self._lock:
            self._closed = True
            for workers in self._idle.values():
                stale.extend(workers)
            self._idle.clear()
        for worker in stale:
            with contextlib.suppress(Exception):
                worker.close(timeout=2.0)


def _is_short_control_call(message: dict[str, Any]) -> bool:
    if message.get("method") != "tools/call":
        return True
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    name = str(params.get("name") or "").lower()
    if name.endswith(("job_stdin", "write_stdin")):
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        delay = args.get("yield_time_ms")
        if delay is None:
            delay = 250 if args.get("chars") or args.get("eof") else 1000
        return isinstance(delay, (int, float)) and 0 <= delay <= 250
    return name.endswith(_CONTROL_TOOL_SUFFIXES)


def serve_selected(
    select,
    *,
    reader=None,
    writer=None,
    handler: Callable[[dict[str, Any], threading.Event | None], dict[str, Any]] | None = None,
):
    """Concurrent MCP stdio gateway over a selected runtime generation or in-process handler.

    `select()` returns `(argv, env)` from one committed binding (or `(callable, env)`
    when in-process). Concurrent requests run in parallel across work and control
    lanes without single-flight `-32000` blocking or cold per-request Python startups.
    """
    reader, writer = reader or sys.stdin, writer or sys.stdout
    output_lock = threading.Lock()
    pending_lock = threading.Lock()
    pending: dict[Any, threading.Event] = {}
    pool = _ResidentPool()

    def send(value):
        with output_lock:
            writer.write(json.dumps(value, ensure_ascii=False) + "\n")
            writer.flush()

    def revision():
        target, env = select()
        env_key = tuple(sorted((env or {}).items()))
        if callable(target):
            return (id(target), env_key)
        resident_cmd, supports_resident = _resident_argv(target)
        key = (tuple(resident_cmd if supports_resident else target), env_key)
        if supports_resident:
            pool.retire_superseded(key)
        return tuple(target), env_key

    refresh = CatalogRefresh(revision, send)

    def dispatch(message: dict[str, Any], cancel: threading.Event | None) -> None:
        stage = "select_runtime"
        request_id = message.get("id")
        try:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("MCP request cancelled before dispatch")
            if handler is not None:
                stage = "dispatch_helper"
                with request_context(cancel or threading.Event()):
                    response = handler(message, cancel)
            else:
                target, env = select()
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("MCP request cancelled before dispatch")
                stage = "dispatch_helper"
                timeout = 660.0 if message.get("method") == "tools/call" else 15.0
                if callable(target):
                    with request_context(cancel or threading.Event()):
                        response = target(message, cancel)
                else:
                    response = pool.call(
                        target, env, message, timeout=timeout, cancel=cancel,
                    )
        except Exception as exc:
            not_sent = stage != "dispatch_helper" or bool(getattr(exc, "not_sent", False))
            response = {"jsonrpc": "2.0", "id": request_id, "result": {
                "isError": True, "content": [{"type": "text", "text": str(exc)}],
                "structuredContent": {
                    "error_type": type(exc).__name__, "automatic_retry": False,
                    "stage": stage,
                    "status": "cancelled" if isinstance(exc, InterruptedError) else "helper_failed",
                    "execution": ("outcome_unconfirmed" if not not_sent
                                  and message.get("method") == "tools/call" else "not_started"),
                },
            }}
        finally:
            if request_id is not None:
                with pending_lock:
                    pending.pop(request_id, None)
        refresh.check()
        if response is not None:
            send(response)

    work_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mindie-mcp-work")
    control_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mindie-mcp-ctrl")
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
                cancelled_id = (message.get("params") or {}).get("requestId")
                with pending_lock:
                    cancel_event = pending.get(cancelled_id)
                if cancel_event is not None:
                    cancel_event.set()
                continue
            if "id" not in message:
                continue
            request_id = message["id"]
            if method == "initialize":
                if handler is not None:
                    response = handler(message, None)
                else:
                    target, env = select()
                    if callable(target):
                        response = target(message, None)
                    else:
                        response = pool.call(target, env, message, timeout=15.0, cancel=None)
                response.setdefault("result", {}).setdefault("capabilities", {})["tools"] = {"listChanged": True}
                send(response)
                refresh.start()
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            else:
                cancel_event = threading.Event()
                with pending_lock:
                    if request_id in pending:
                        send({"jsonrpc": "2.0", "id": request_id,
                              "error": {"code": -32600, "message": "request id is already running"}})
                        continue
                    pending[request_id] = cancel_event
                executor = control_executor if _is_short_control_call(message) else work_executor
                executor.submit(dispatch, message, cancel_event)
    finally:
        refresh.close()
        with pending_lock:
            active_events = list(pending.values())
        for event in active_events:
            event.set()
        control_executor.shutdown(wait=True)
        work_executor.shutdown(wait=True)
        pool.close()
    return 0
