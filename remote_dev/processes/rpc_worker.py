"""Ephemeral SSH stdio transport. Process ownership stays in worker.py.

Self-contained, standard-library-only Linux program shipped by the client.
No listener, installation, container, scheduler or independent job registry.
"""
from __future__ import annotations

import concurrent.futures
from collections import OrderedDict
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid


# REMOTE_DEV_MUTATION_LOCK


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    output_lock = threading.Lock()
    pending_lock = threading.Lock()
    pending = {}
    codes = OrderedDict()
    modules = OrderedDict()
    module_lock = threading.Lock()
    process_instance = uuid.uuid4().hex
    capacity = threading.BoundedSemaphore(32)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    control_capacity = threading.BoundedSemaphore(8)
    control_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def send(value):
        with output_lock:
            sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def execute(message, cancelled, admission):
        identifier = message["id"]
        import time
        started = time.monotonic()
        submission_state = "not_executed"
        try:
            if cancelled.is_set():
                raise RuntimeError("request cancelled before execution")
            source = message["_source"]
            submission_state = "acknowledged"
            if message["kind"] == "control":
                with module_lock:
                    namespace = modules.get(message["code_key"])
                    if namespace is None:
                        namespace = {"__name__": "remote_dev_process_worker"}
                        exec(compile(source, "<remote-dev-worker>", "exec"), namespace)
                        modules[message["code_key"]] = namespace
                        if len(modules) > 8:
                            modules.popitem(last=False)
                    modules.move_to_end(message["code_key"])
                result = namespace["control_job"](message["payload"], source, cancelled)
            elif message["kind"] == "python":
                guard = remote_mutation_lock(cancelled) if message["payload"].get("_mutation") else contextlib.nullcontext()
                with guard:
                    import time
                    timeout = message.get("timeout_ms")
                    deadline = None if timeout is None else time.monotonic() + timeout / 1000
                    proc = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True)
                    first_input = json.dumps(message["payload"], ensure_ascii=False).encode("utf-8")
                    timed_out = False
                    try:
                        while True:
                            if cancelled.is_set() or (deadline is not None and time.monotonic() >= deadline):
                                timed_out = not cancelled.is_set()
                                with contextlib.suppress(ProcessLookupError):
                                    os.killpg(proc.pid, signal.SIGKILL)
                                stdout, stderr = proc.communicate()
                                break
                            try:
                                stdout, stderr = proc.communicate(input=first_input, timeout=0.05)
                                break
                            except subprocess.TimeoutExpired:
                                first_input = None
                        result = {"returncode": proc.returncode,
                                  "stdout": stdout.decode("utf-8", "replace"),
                                  "stderr": stderr.decode("utf-8", "replace"),
                                  "timed_out": timed_out, "cancelled": cancelled.is_set()}
                    finally:
                        if proc.poll() is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
            else:
                raise ValueError("unsupported RPC operation")
            send({"id": identifier, "result": result,
                  "diagnostics": {"elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                  "clock_domain": process_instance, "pid": os.getpid(),
                                  "context": message.get("diagnostics_context") or {}}})
        except BaseException as exc:
            send({"id": identifier, "error": {"type": type(exc).__name__, "message": str(exc)[:4000],
                  "category": "remote_worker", "submission_state": submission_state,
                  **({"cleanup_error": exc.cleanup_error} if getattr(exc, "cleanup_error", None) else {})}})
        finally:
            with pending_lock:
                pending.pop(identifier, None)
            admission.release()

    send({"id": 0, "ready": True, "protocol": 1})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("kind") == "cancel":
                with pending_lock:
                    event = pending.get(message.get("request_id"))
                    if event is not None:
                        event.set()
                continue
            identifier = message["id"]
            if "code" in message:
                source = message["code"]
                if hashlib.sha256(source.encode("utf-8")).hexdigest() != message["code_key"]:
                    raise ValueError("RPC code digest mismatch")
                codes[message["code_key"]] = source
            codes.move_to_end(message["code_key"])
            message["_source"] = codes[message["code_key"]]
            if len(codes) > 32:
                codes.popitem(last=False)
            payload = message.get("payload") or {}
            short_control = message.get("kind") == "control" and (
                payload.get("action") in {"status", "tail", "stop", "stdin"}
                or (payload.get("action") == "exchange" and int(payload.get("yield_time_ms") or 0) <= 250))
            admission = control_capacity if short_control else capacity
            selected_executor = control_executor if short_control else executor
            if not admission.acquire(blocking=False):
                send({"id": identifier, "error": {"type": "RuntimeError", "message": "RPC capacity exhausted; request was not executed",
                      "category": "worker_capacity", "submission_state": "not_executed", "retryable": True}})
                continue
            cancelled = threading.Event()
            with pending_lock:
                pending[identifier] = cancelled
            selected_executor.submit(execute, message, cancelled, admission)
    finally:
        with pending_lock:
            for event in pending.values():
                event.set()
        executor.shutdown(wait=True)
        control_executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
