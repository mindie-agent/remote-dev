from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from remote_dev.core import rpc_transport, ssh_transport
from local_ssh import local_python_ssh
from remote_dev.core.cancellation import request_context
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError

PYTHON_ECHO = """import json,sys,time
p=json.load(sys.stdin)
time.sleep(p.get('delay',0))
print(json.dumps(p,ensure_ascii=False))
"""
CONTROL = """calls=0
def control_job(payload, source, event):
    global calls
    calls += 1
    if payload.get('wait'):
        event.wait(5)
    return {'state': 'cancelled' if event.is_set() else 'succeeded', 'calls': calls, 'value': payload.get('value')}
"""


class RpcTests(unittest.TestCase):
    def setUp(self):
        rpc_transport.close_connections()
        self.addCleanup(rpc_transport.close_connections)
        adapter = local_python_ssh()
        adapter.__enter__()
        self.addCleanup(adapter.__exit__, None, None, None)
        self.endpoint = Endpoint(host="192.0.2.10", port=22, root="/tmp")

    def request(self, value, **kwargs):
        return rpc_transport.request(self.endpoint, "control", CONTROL, value, **kwargs)

    def test_binary_pipe_preserves_unicode_newlines_and_code_is_cached(self):
        value = "中文\r\nUnix\n' \" $ ` \\"
        first = self.request({"value": value})
        second = self.request({"value": value})
        self.assertEqual(first["value"], value)
        self.assertEqual(second["value"], value)
        self.assertEqual([first["calls"], second["calls"]], [1, 2])
        connection = next(iter(rpc_transport._pool.values())).connection
        self.assertEqual(len(connection.sent_codes), 1)
        self.assertTrue(second["transport"]["connection_reused"])

    def test_real_python_payload_roundtrip_and_timeout(self):
        result = ssh_transport.run_remote_python(self.endpoint, PYTHON_ECHO, {"value": "世界\n"}, timeout_ms=2000)
        self.assertEqual(result["value"], "世界\n")
        if sys.platform == "linux":
            result = ssh_transport.run_remote_python(self.endpoint, PYTHON_ECHO, {"delay": 30}, timeout_ms=100)
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(ssh_transport.run_remote_python(self.endpoint, PYTHON_ECHO, {"alive": True})["alive"], True)

    def test_code_cache_eviction_can_reload_without_replaying_a_request(self):
        for index in range(35):
            source = CONTROL + "\n# version " + str(index)
            row = rpc_transport.request(self.endpoint, "control", source, {"value": index})
            self.assertEqual(row["value"], index)
        connection = next(iter(rpc_transport._pool.values())).connection
        self.assertEqual(len(connection.sent_codes), 32)
        self.assertEqual(self.request({"value": "reloaded"})["value"], "reloaded")

    def test_remote_mutations_serialize_and_lock_wait_is_cancellable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter"
            path.write_text("0")
            source = """import json,sys,time
from pathlib import Path
p=json.load(sys.stdin); path=Path(p['path'])
n=int(path.read_text()); time.sleep(p.get('delay', 0.1)); path.write_text(str(n+1))
print(json.dumps({'status':'ok'}))
"""
            args = {"path": str(path), "_mutation": True}
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(ssh_transport.run_remote_python, self.endpoint, source, args) for _ in range(2)]
                for future in futures:
                    self.assertEqual(future.result(timeout=5)["status"], "ok")
            self.assertEqual(path.read_text(), "2")
            from remote_dev.processes.mutation import remote_mutation_lock
            event = threading.Event()
            def waiting():
                with request_context(event):
                    return rpc_transport.request(self.endpoint, "python", source, args)
            with remote_mutation_lock(), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(waiting)
                time.sleep(0.1)
                event.set()
                with self.assertRaisesRegex(RemoteExecutionError, "mutation cancelled"):
                    future.result(timeout=3)
            self.assertEqual(path.read_text(), "2")

    def test_fast_python_request_overtakes_slow_request_on_same_channel(self):
        self.request({})  # Establish the transport before comparing requests.
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(ssh_transport.run_remote_python, self.endpoint, PYTHON_ECHO, {"delay": 1})
            fast = pool.submit(ssh_transport.run_remote_python, self.endpoint, PYTHON_ECHO, {"delay": 0})
            self.assertEqual(fast.result(timeout=3)["delay"], 0)
            self.assertFalse(slow.done())
            self.assertEqual(slow.result(timeout=3)["delay"], 1)

    def test_cancellation_is_forwarded_without_cancelling_other_requests(self):
        self.request({})
        cancelled = threading.Event()
        def slow():
            with request_context(cancelled):
                return self.request({"wait": True})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(slow)
            time.sleep(0.1)
            cancelled.set()
            self.assertEqual(future.result(timeout=3)["state"], "cancelled")
        self.assertEqual(self.request({})["state"], "succeeded")

    def test_disconnect_reports_unknown_outcome_without_replay(self):
        self.request({})
        connection = next(iter(rpc_transport._pool.values())).connection
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, {"wait": True})
            time.sleep(0.1)
            connection.proc.kill()
            with self.assertRaisesRegex(RemoteExecutionError, "unknown"):
                future.result(timeout=3)
        # Only an explicitly new operation reconnects, with a fresh code cache.
        self.assertEqual(self.request({})["calls"], 1)

    def test_roots_share_transport_but_connection_identities_do_not_cross(self):
        from dataclasses import replace

        self.request({})
        sibling = rpc_transport.request(replace(self.endpoint, root="/other"), "control", CONTROL, {})
        self.assertEqual(sibling["calls"], 2)
        self.assertTrue(sibling["transport"]["connection_reused"])
        for field, value in (("host", "other.example"), ("port", 46001), ("user", "other"),
                             ("identity_file", "/client/other-key"), ("connect_timeout_ms", 2000)):
            row = rpc_transport.request(replace(self.endpoint, **{field: value}), "control", CONTROL, {})
            self.assertEqual(row["calls"], 1)
        self.assertEqual(len(rpc_transport._pool), 6)

    def test_cancelled_before_submission_does_not_execute(self):
        self.request({})
        cancelled = threading.Event()
        cancelled.set()
        with request_context(cancelled), self.assertRaisesRegex(RemoteExecutionError, "not sent"):
            self.request({})
        self.assertEqual(self.request({})["calls"], 2)

    def test_send_failure_keeps_uncertain_state_when_close_also_fails(self):
        from remote_dev.core.errors import error_details
        self.request({})
        connection = next(iter(rpc_transport._pool.values())).connection
        primary = BrokenPipeError("send fixture")
        close = connection.close
        def failed_close():
            close()
            raise OSError("cleanup fixture")
        with mock.patch.object(connection, "_send", side_effect=primary) as send, mock.patch.object(connection, "close", side_effect=failed_close):
            with self.assertRaises(RemoteExecutionError) as caught:
                self.request({"value": "never replay"})
        self.assertIs(caught.exception.__cause__, primary)
        self.assertEqual(caught.exception.submission_state, "uncertain")
        self.assertEqual(caught.exception.category, "rpc_send")
        self.assertIn("cleanup fixture", error_details(caught.exception)["cleanup_error"])
        self.assertEqual(send.call_count, 1)

    def test_non_json_and_failed_python_are_structured_errors(self):
        failed = ssh_transport.run_remote_python(self.endpoint, "raise SystemExit(7)", {})
        self.assertEqual(failed["exit_code"], 7)
        invalid = ssh_transport.run_remote_python(self.endpoint, "print('plain text')", {})
        self.assertIn("non-JSON", invalid["error"])
        self.assertEqual(invalid["error_details"]["submission_state"], "acknowledged")

    def test_remote_value_error_retains_acknowledgment_without_becoming_caller_error(self):
        from remote_dev.core.errors import error_details
        source = "def control_job(payload, source, event): raise ValueError('internal decode failed')"
        with self.assertRaises(ValueError) as caught:
            rpc_transport.request(self.endpoint, "control", source, {})
        details = error_details(caught.exception)
        self.assertEqual(details["category"], "remote_worker")
        self.assertEqual(details["submission_state"], "acknowledged")
        self.assertFalse(details["retryable"])

    def test_control_requests_have_capacity_when_all_normal_slots_are_waiting(self):
        self.request({})
        connection = next(iter(rpc_transport._pool.values())).connection
        cancelled = threading.Event()
        def wait():
            with request_context(cancelled):
                return self.request({"action": "launch", "wait": True})
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(wait) for _ in range(32)]
            try:
                deadline = time.monotonic() + 3
                while len(connection.pending) < 32 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(connection.pending), 32)
                started = time.monotonic()
                for action in ("status", "stop", "stdin", "tail"):
                    self.assertEqual(self.request({"action": action})["state"], "succeeded")
                self.assertLess(time.monotonic() - started, 2)
                self.assertFalse(any(future.done() for future in futures))
            finally:
                cancelled.set()
            for future in futures:
                try:
                    future.result(timeout=5)
                except RemoteExecutionError as exc:
                    self.assertIn("cancelled before execution", str(exc))
