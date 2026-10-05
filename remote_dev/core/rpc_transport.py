"""Reusable binary-pipe SSH RPC, identical on Windows and POSIX clients.

Never retries a submitted request: a lost reply is an unknown outcome. A new
call can reconnect; persistent jobs remain discoverable by their job id.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
import contextlib
import itertools
import hashlib
import json
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .cancellation import current_event
from .errors import RemoteExecutionError, record_cleanup_failure
from .execution import timeout_value
from .local_process import OwnedProcess
from mindie_diagnostics import get_recorder, current_context
from remote_dev.observability import observed_operation, current_tool
from .container_endpoint import pin_container_endpoint


class RpcConnection:
    def __init__(self, endpoint):
        from .ssh_transport import ssh_command
        endpoint = pin_container_endpoint(endpoint)
        source = (Path(__file__).parents[1] / "processes" / "rpc_worker.py").read_text(encoding="utf-8")
        helper = (Path(__file__).parents[1] / "processes" / "mutation.py").read_text(encoding="utf-8")
        source = source.replace("# REMOTE_DEV_MUTATION_LOCK", helper)
        transport_endpoint = replace(endpoint, ssh_mux=False, keepalive=True)
        self.owner = OwnedProcess(
            ssh_command(transport_endpoint, "python3 -u -c " + shlex.quote(source)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.proc = self.owner.process
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending = {}
        self.sent_codes = OrderedDict()
        self.sequence = 0
        self.closed = False
        self.close_lock = threading.Lock()
        self.stop_lock = threading.Lock()
        self.cleanup_error = None
        self.error_tail = bytearray()
        self.ready = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.errors = threading.Thread(target=self._stderr, daemon=True)
        self.errors.start()
        self.reader.start()

    def _stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.read1(1024)
                if not chunk:
                    return
                self.error_tail.extend(chunk)
                del self.error_tail[:-4000]
        except (OSError, ValueError):
            return

    def _stop_owner(self):
        with self.stop_lock:
            self.owner.stop()

    def _read(self):
        failure = "SSH RPC disconnected; submitted operation outcome may be unknown"
        try:
            for line in self.proc.stdout:
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict) or not ("result" in value or "error" in value or value.get("ready")):
                    raise ValueError("SSH RPC returned an invalid response object")
                if value.get("id") == 0 and value.get("ready"):
                    self.ready.set()
                    continue
                with self.state_lock:
                    waiter = self.pending.get(value.get("id"))
                if waiter is not None:
                    waiter.put(value)
        except (OSError, ValueError) as exc:
            failure = f"SSH RPC protocol failed: {exc}; submitted operation outcome may be unknown"
        finally:
            # EOF/protocol loss is a failed channel, not a slow operation.
            # Reclaim its owned family so proxy children cannot retain pipes.
            try:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=.1)
                self._stop_owner()
            except Exception as exc:
                self.cleanup_error = f"{type(exc).__name__}: {exc}"
            self.errors.join(timeout=1)
            self._fail(failure)

    def _fail(self, reason):
        with self.state_lock:
            self.closed = True
            waiters = list(self.pending.values())
        self.ready.set()
        detail = self.error_tail.decode("utf-8", "replace")
        for waiter in waiters:
            waiter.put({"error": {"type": "RemoteExecutionError", "message": reason + (": " + detail if detail else ""),
                                  "category": "rpc_disconnected", "submission_state": "uncertain",
                                  "exit_code": self.proc.poll(), "stderr_tail": detail,
                                  **({"cleanup_error": self.cleanup_error} if self.cleanup_error else {})}})

    def _send(self, value):
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, kind, source, payload, timeout_ms):
        timeout_value(timeout_ms)
        event = current_event()
        started = time.monotonic()
        reused = self.ready.is_set() and not self.closed
        while not self.ready.wait(0.05):
            if event is not None and event.is_set():
                raise RemoteExecutionError("SSH RPC cancelled before submission; request was not sent", category="cancelled", submission_state="not_sent")
            # The SSH process/reader reports refusal, EOF and heartbeat loss.
            # Quiet startup is not evidence that the connection failed.
            if self.proc.poll() is not None:
                self._fail("SSH RPC exited before readiness")
        connected = time.monotonic()
        if event is not None and event.is_set():
            raise RemoteExecutionError("SSH RPC cancelled before submission; request was not sent", category="cancelled", submission_state="not_sent")
        if self.closed:
            detail = self.error_tail.decode("utf-8", "replace").strip()
            raise RemoteExecutionError("SSH RPC unavailable; request was not sent" + (": " + detail if detail else ""), category="connection_unavailable", submission_state="not_sent", retryable=True)
        code_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
        waiter = queue.Queue()
        with self.write_lock:
            if self.closed:
                raise RemoteExecutionError("SSH RPC disconnected before submission; request was not sent", category="rpc_disconnected", submission_state="not_sent", retryable=True)
            self.sequence += 1
            identifier = self.sequence
            with self.state_lock:
                self.pending[identifier] = waiter
            message = {"id": identifier, "kind": kind, "code_key": code_key,
                       "payload": payload, "timeout_ms": timeout_ms, "diagnostics_context": current_context()}
            if code_key not in self.sent_codes:
                message["code"] = source
            try:
                self._send(message)
                self.sent_codes[code_key] = None
                self.sent_codes.move_to_end(code_key)
                if len(self.sent_codes) > 32:
                    self.sent_codes.popitem(last=False)
            except (OSError, ValueError) as exc:
                with self.state_lock:
                    self.pending.pop(identifier, None)
                failure = RemoteExecutionError("SSH RPC send failed; operation outcome may be unknown", category="rpc_send", submission_state="uncertain")
                try:
                    self.close()
                except Exception as cleanup_error:
                    record_cleanup_failure(failure, cleanup_error)
                raise failure from exc
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000 + (5 if kind == "python" else 0)
        event = current_event()
        cancel_sent = False
        try:
            while True:
                remaining = 60 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    with self.write_lock:
                        with contextlib.suppress(OSError, ValueError):
                            self._send({"kind": "cancel", "request_id": identifier})
                    message = ("SSH RPC cancellation unconfirmed; inspect the original job; outcome may be unknown"
                               if cancel_sent else "SSH RPC request timed out; inspect the original job before retrying; outcome may be unknown")
                    raise RemoteExecutionError(message, category="cancel_unconfirmed" if cancel_sent else "rpc_timeout", submission_state="uncertain")
                if event is not None and event.is_set() and not cancel_sent:
                    with self.write_lock:
                        self._send({"kind": "cancel", "request_id": identifier})
                    cancel_sent = True
                    # Cancellation ends business execution. This bound only
                    # waits for its acknowledgement and never starts a retry.
                    deadline = time.monotonic() + 5
                try:
                    value = waiter.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    if self.proc.poll() is not None and not self.reader.is_alive():
                        self._fail("SSH RPC process exited; submitted operation outcome may be unknown")
                    continue
                if "error" in value:
                    error = value["error"]
                    exception = {"ValueError": ValueError, "FileNotFoundError": FileNotFoundError,
                                 "NotADirectoryError": NotADirectoryError}.get(error.get("type"), RemoteExecutionError)
                    if exception is RemoteExecutionError:
                        failure = exception(error.get("message", "SSH RPC failed"),
                                        category=error.get("category", "remote_execution"),
                                        submission_state=error.get("submission_state", "acknowledged"),
                                        retryable=error.get("retryable", False))
                        for key in ("exit_code", "stderr_tail", "cleanup_error"):
                            if error.get(key) is not None:
                                setattr(failure, key, error[key])
                        raise failure
                    failure = exception(error.get("message", "SSH RPC failed"))
                    failure.category = error.get("category", "remote_execution")
                    failure.submission_state = error.get("submission_state", "acknowledged")
                    failure.retryable = bool(error.get("retryable", False))
                    raise failure
                result = value["result"]
                remote_timing = value.get("diagnostics") or {}
                get_recorder("remote-dev").event("DEBUG", "rpc.remote_completed",
                    elapsed_ms=remote_timing.get("elapsed_ms"), clock_domain=remote_timing.get("clock_domain"),
                    remote_pid=remote_timing.get("pid"), submission_state="acknowledged")
                if isinstance(result, dict):
                    result["transport"] = {"connection_reused": reused,
                                           "connection_wait_ms": round((connected-started)*1000),
                                           "rpc_ms": round((time.monotonic()-connected)*1000)}
                return result
        finally:
            with self.state_lock:
                self.pending.pop(identifier, None)

    def close(self):
        with self.close_lock:
            failures = []
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()
            try:
                if self.proc.poll() is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self.proc.wait(timeout=2)
                self._stop_owner()
            except Exception as exc:
                failures.append(exc)
            for thread, stream in ((self.reader, self.proc.stdout), (self.errors, self.proc.stderr)):
                thread.join(timeout=1)
                try:
                    # BufferedReader.close must not wait behind a stuck read
                    # when process cleanup itself has failed.
                    if thread.is_alive():
                        raise RuntimeError("SSH RPC pipe reader did not stop during cleanup")
                    stream.close()
                except Exception as exc:
                    failures.append(exc)
            self._fail("SSH RPC connection closed; submitted operation outcome may be unknown")
            if self.cleanup_error:
                failures.append(RuntimeError(self.cleanup_error))
            if failures:
                failure = RemoteExecutionError("SSH RPC cleanup failed", category="cleanup")
                for exc in failures:
                    record_cleanup_failure(failure, exc)
                raise failure


@dataclass
class _Entry:
    connection: object = None
    active: int = 0
    last_used: float = 0
    use_order: int = 0


_pool = {}
_use_order = itertools.count(1)
_pool_lock = threading.Condition()
_POOL_LIMIT = 32
_IDLE_SECONDS = 300
_reaper_started = False
_cleanup_failures = []


def _idle_connections(now):
    expired = [key for key, entry in _pool.items()
               if entry.connection is not None and not entry.active
               and now - entry.last_used >= _IDLE_SECONDS]
    return [_pool.pop(key).connection for key in expired]


def _reap_idle():
    while True:
        with _pool_lock:
            _pool_lock.wait(timeout=min(60, _IDLE_SECONDS))
            connections = _idle_connections(time.monotonic())
            if connections:
                _pool_lock.notify_all()
        for connection in connections:
            try:
                connection.close()
            except Exception as exc:
                with _pool_lock:
                    _cleanup_failures.append(exc)


def _acquire(endpoint, key):
    global _reaper_started
    event = current_event()
    while True:
        retired = None
        with _pool_lock:
            if event is not None and event.is_set():
                raise RemoteExecutionError("SSH RPC cancelled while waiting for a connection; request was not sent", category="cancelled", submission_state="not_sent")
            entry = _pool.get(key)
            if entry is not None and entry.connection is not None:
                connection = entry.connection
                if not connection.closed and connection.proc.poll() is None:
                    entry.active += 1
                    return entry
                # A failed transport cannot service its existing requests. They
                # retain their entry until finally; replacement never replays them.
                retired = _pool.pop(key).connection
                entry = None
            if entry is None:
                if len(_pool) >= _POOL_LIMIT:
                    idle = [(item.use_order, candidate) for candidate, item in _pool.items()
                            if item.connection is not None and not item.active]
                    if idle:
                        _, candidate = min(idle)
                        retired = _pool.pop(candidate).connection
                    else:
                        raise RemoteExecutionError("SSH RPC connection capacity is in use; request was not sent", category="connection_capacity", submission_state="not_sent", retryable=True)
                entry = _Entry(active=1)
                _pool[key] = entry  # Reserve before opening, coalescing this key.
                if not _reaper_started:
                    threading.Thread(target=_reap_idle, daemon=True).start()
                    _reaper_started = True
            else:
                _pool_lock.wait(timeout=0.05)
                continue
        # Neither SSH process startup nor shutdown holds the global pool lock.
        try:
            if retired is not None:
                retired.close()
            connection = RpcConnection(endpoint)
        except BaseException:
            with _pool_lock:
                if _pool.get(key) is entry:
                    _pool.pop(key)
                _pool_lock.notify_all()
            raise
        with _pool_lock:
            registered = _pool.get(key) is entry
            if registered:
                entry.connection = connection
            _pool_lock.notify_all()
        if not registered:
            connection.close()
            raise RemoteExecutionError("SSH RPC pool closed before submission; request was not sent", category="connection_unavailable", submission_state="not_sent", retryable=True)
        return entry


@observed_operation(lambda endpoint, kind, source, payload, **kwargs: "rpc." + kind, level="DEBUG")
def request(endpoint, kind, source, payload, *, timeout_ms=None):
    timeout_value(timeout_ms)
    # The transport has no working-tree state: each operation carries its own
    # root/cwd, and the worker resolves job paths within that request's root.
    # Share the authenticated SSH channel across roots within one fixed
    # container (or the host), keeping authentication and connection-timeout
    # choices separate. Code caches are source-digest keyed; job receipts,
    # locks and cancellation remain per request/job.
    endpoint = pin_container_endpoint(endpoint, timeout_ms=timeout_ms)
    key = (endpoint.host, endpoint.port, endpoint.user, endpoint.identity_file,
           endpoint.connect_timeout_ms, endpoint.container)
    started = time.monotonic()
    with get_recorder("remote-dev").operation("rpc.pool.acquire", level="DEBUG"):
        entry = _acquire(endpoint, key)
    acquired = time.monotonic()
    try:
        result = entry.connection.request(kind, source, payload, timeout_ms)
        if isinstance(result, dict):
            result["transport"]["pool_wait_ms"] = round((acquired-started)*1000)
            with _pool_lock:
                if _cleanup_failures:
                    failures = [f"{type(exc).__name__}: {exc}"[:1000] for exc in _cleanup_failures]
                    result["transport"]["cleanup_errors"] = failures
                    active = current_tool()
                    if active is not None:
                        active.setdefault("cleanup_errors", []).extend(failures)
                    _cleanup_failures.clear()
        return result
    finally:
        with _pool_lock:
            entry.active -= 1
            entry.last_used = time.monotonic()
            entry.use_order = next(_use_order)
            _pool_lock.notify_all()


def close_connections():
    with _pool_lock:
        connections = [entry.connection for entry in _pool.values() if entry.connection is not None]
        _pool.clear()
        failures = list(_cleanup_failures)
        _cleanup_failures.clear()
        _pool_lock.notify_all()
    for connection in connections:
        try:
            connection.close()
        except Exception as exc:
            failures.append(exc)
    if failures:
        failure = RemoteExecutionError("SSH RPC pool cleanup failed", category="cleanup")
        for exc in failures:
            record_cleanup_failure(failure, exc)
        raise failure


atexit.register(close_connections)
