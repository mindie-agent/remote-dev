"""Observable execute/poll semantics shared by native Windows and POSIX clients."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core import job_ops, state_store
from remote_dev.core.shell_ops import remote_bash
from remote_dev.core.cancellation import current_event
from remote_dev.mcp import server


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, REMOTE_DEV_STATE_DIR=self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.endpoint = Endpoint(host="192.0.2.10", port=22, root="/srv", cwd="/srv/work",
                                 identity_file="/client/key with spaces", runtime_env=False, connect_timeout_ms=1234)
        self.calls = []
        self.stdout = b""
        self.stderr = b""
        self.done = False

    def control(self, endpoint, identifier, action, **params):
        self.calls.append((endpoint, identifier, action, params))
        self.assertIn(action, {"launch", "exchange", "tail"})
        if action == "tail":
            return {
                "state": "succeeded" if self.done else "running",
                "quiet": self.done,
                "stdout": self.stdout.decode()[:32768],
                "stderr": self.stderr.decode()[:32768],
            }
        row = {"state": "succeeded" if self.done else "running", "quiet": self.done,
               "result": {"exit_code": 0} if self.done else None, "accepted": True,
               "written": len(params.get("data", "").encode()), "written_chars": len(params.get("data", "")),
               "eof": params.get("eof", False)}
        budget = params["max_bytes"]
        for name in ("stdout", "stderr"):
            data = getattr(self, name)
            offset = params[name + "_offset"]
            chunk = data[offset:offset + budget]
            row[name] = chunk.decode()
            row[name + "_offset"] = offset + len(chunk)
            row[name + "_bytes_remaining"] = len(data) - offset - len(chunk)
            budget -= len(chunk)
        return row

    def start(self, **kwargs):
        with mock.patch.object(job_ops, "control", self.control):
            return remote_bash(self.endpoint, command="printf sample", **kwargs)["result"]

    def poll(self, identifier, **kwargs):
        with mock.patch.object(job_ops, "control", self.control):
            return job_ops.remote_job_stdin(None, job_id=identifier, **kwargs)["result"]

    def test_default_launch_is_one_rpc_with_writable_input_and_native_yield(self):
        result = self.start()
        self.assertEqual(len(self.calls), 1)
        _, identifier, action, parameters = self.calls[0]
        self.assertEqual(action, "launch")
        self.assertTrue(parameters["spec"]["interactive"])
        self.assertFalse(parameters["spec"]["tty"])
        self.assertEqual(parameters["yield_time_ms"], 10000)
        self.assertFalse(parameters["wait_for_exit"])
        self.assertEqual(result["session_id"], identifier)

    def test_completed_launch_has_consistent_final_observation(self):
        self.done, self.stdout = True, b"finished\n"
        result = self.start()
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(result["quiet"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["preview"]["stdout"], "finished\n")
        self.assertNotIn("job", result)  # No stale nested launch/yield snapshots.

    def test_pty_reaches_the_same_supervisor(self):
        self.start(tty=True)
        self.assertTrue(self.calls[0][3]["spec"]["tty"])

    def test_resumed_session_preserves_identity_and_actual_cwd(self):
        result = self.start(cwd="/srv/other")
        self.poll(result["job_id"], chars="hello 世界\n")
        restored = self.calls[-1][0]
        self.assertEqual(restored.identity_file, self.endpoint.identity_file)
        self.assertEqual(restored.connect_timeout_ms, 1234)
        self.assertEqual(restored.cwd, "/srv/other")
        self.assertFalse(restored.runtime_env)
        self.assertEqual(self.calls[-1][2], "exchange")

    def test_empty_poll_is_legal_without_a_stdin_channel(self):
        self.done = True
        result = self.start(wait=True)
        self.calls.clear()
        self.stdout = b"late log"
        polled = self.poll(result["job_id"])
        self.assertEqual(polled["preview"]["stdout"], "late log")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][3]["data"], "")

    def test_output_budget_and_cursors_cover_both_streams_without_loss(self):
        self.done, self.stdout, self.stderr = True, b"a" * 333, b"b" * 333
        result = self.start(max_output_tokens=64)
        identifier = result["job_id"]
        collected = {"stdout": "", "stderr": ""}
        while True:
            self.assertLessEqual(sum(len(value.encode()) for value in result["preview"].values()), 128)
            self.assertNotIn("new_output", result)
            for name in collected:
                collected[name] += result["preview"][name]
            if result["session_id"] is None:
                break
            result = self.poll(identifier, max_output_tokens=64, yield_time_ms=0)
        self.assertEqual(collected, {"stdout": self.stdout.decode(), "stderr": self.stderr.decode()})
        for name in collected:
            self.assertEqual(Path(result["refs"][name]).read_text(), collected[name])
        self.assertEqual(self.poll(identifier)["preview"], {"stdout": "", "stderr": ""})

    def test_two_concurrent_polls_cannot_replay_the_same_cursor(self):
        result = self.start(yield_time_ms=0)
        self.stdout = b"abcdefgh"
        original = self.control
        def slow(*args, **kwargs):
            time.sleep(0.05)
            return original(*args, **kwargs)
        with mock.patch.object(job_ops, "control", slow), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(job_ops.remote_job_stdin, None, job_id=result["job_id"],
                                   yield_time_ms=0, max_output_tokens=2) for _ in range(2)]
            output = sorted(f.result()["result"]["preview"]["stdout"] for f in futures)
        self.assertEqual(output, ["abcd", "efgh"])

    def test_unknown_launch_retains_recovery_identity_without_replay(self):
        def lost(endpoint, identifier, action, **parameters):
            record = state_store.read_json(state_store.job_record_path(endpoint, identifier))
            self.assertEqual(record["job_id"], identifier)
            raise RemoteExecutionError("reply lost; outcome unknown")
        with mock.patch.object(job_ops, "control", side_effect=lost) as execute:
            result = remote_bash(self.endpoint, command="touch once")["result"]
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result["session_id"], result["job_id"])
        self.assertTrue(Path(result["refs"]["job_record"]).exists())

    def test_long_poll_does_not_block_stdin_or_replay_consumed_output(self):
        result = self.start(yield_time_ms=0)
        entered, release = threading.Event(), threading.Event()
        writes = []
        def exchange(*args, **kwargs):
            if kwargs.get("data"):
                writes.append(kwargs["data"])
            if kwargs.get("yield_time_ms") == 30000:
                entered.set()
                self.assertTrue(release.wait(3))
            return self.control(*args, **kwargs)
        with mock.patch.object(job_ops, "control", exchange), ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(job_ops.remote_job_stdin, None, job_id=result["job_id"], yield_time_ms=30000)
            self.assertTrue(entered.wait(2))
            self.stdout = b"one response"
            try:
                reply = pool.submit(job_ops.remote_job_stdin, None, job_id=result["job_id"],
                                    chars="input", yield_time_ms=0).result(timeout=2)["result"]
                self.assertEqual(reply["preview"]["stdout"], "one response")
            finally:
                release.set()
            self.assertEqual(waiting.result(timeout=2)["result"]["preview"]["stdout"], "")
        self.assertEqual(writes, ["input"])

    def test_conflicting_long_input_exchange_never_sends_input_twice(self):
        result = self.start(yield_time_ms=0)
        entered, release = threading.Event(), threading.Event()
        writes = []
        def exchange(*args, **kwargs):
            if kwargs.get("data"):
                writes.append(kwargs["data"])
            if kwargs.get("yield_time_ms") == 30000:
                entered.set()
                self.assertTrue(release.wait(3))
            return self.control(*args, **kwargs)
        with mock.patch.object(job_ops, "control", exchange), ThreadPoolExecutor(max_workers=2) as pool:
            waiting = pool.submit(job_ops.remote_job_stdin, None, job_id=result["job_id"],
                                  chars="input", yield_time_ms=30000)
            self.assertTrue(entered.wait(2))
            self.stdout = b"consumed"
            try:
                reply = job_ops.remote_job_stdin(None, job_id=result["job_id"], yield_time_ms=0)["result"]
                self.assertEqual(reply["preview"]["stdout"], "consumed")
            finally:
                release.set()
            reply = waiting.result(timeout=2)["result"]
            self.assertEqual(reply["preview"]["stdout"], "")
            self.assertEqual(reply["stdin"]["written_chars"], 5)
        self.assertEqual(writes, ["input"])

    def test_wait_mode_uses_same_session_and_preserves_full_logs(self):
        self.done, self.stdout = True, b"x" * 40000
        result = self.start(wait=True, max_output_tokens=64)
        self.assertFalse(self.calls[0][3]["spec"]["interactive"])
        self.assertTrue(all(call[3]["wait_for_exit"] for call in self.calls))
        self.assertEqual([call[2] for call in self.calls], ["launch", "exchange", "exchange"])
        self.assertEqual(Path(result["refs"]["stdout"]).read_bytes(), self.stdout)
        self.assertEqual(len(result["preview"]["stdout"]), 128)
        self.assertIsNone(result["session_id"])

    def test_partial_input_and_eof_refusal_remain_explicit(self):
        result = self.start()
        row = {"state": "running", "quiet": False, "accepted": True,
               "written": 3, "written_chars": 1, "stdin_buffer_full": True,
               "eof": False, "eof_deferred": True}
        with mock.patch.object(job_ops, "control", return_value=row):
            result = job_ops.remote_job_stdin(None, job_id=result["job_id"], chars="你好", eof=True)["result"]
        self.assertEqual(result["stdin"]["written_chars"], 1)
        self.assertFalse(result["stdin"]["eof"])
        self.assertTrue(result["stdin"]["eof_deferred"])
        self.assertEqual(len(result["warnings"]), 2)


class McpConcurrencyTests(unittest.TestCase):
    def test_control_calls_have_reserved_slots_under_full_wait_queue(self):
        finished = threading.Event()
        responses = []
        def tool(name, args):
            if name == "remote.bash":
                current_event().wait(5)
            else:
                finished.set()
            return {"text": name, "result": {"outcome": "success"}}
        with mock.patch.object(server, "call_tool", tool), mock.patch.object(server, "runtime_status", return_value={}), \
                mock.patch.object(server, "send", lambda value, **kwargs: responses.append(value)):
            dispatcher = server.Dispatcher()
            try:
                for identifier in range(32):
                    dispatcher.dispatch({"id": identifier, "method": "tools/call", "params": {"name": "remote.bash"}})
                self.assertEqual(len(dispatcher.pending), 32)
                dispatcher.dispatch({"id": 40, "method": "tools/call", "params": {"name": "remote_job_stop"}})
                self.assertTrue(finished.wait(2))
            finally:
                dispatcher.close()
        self.assertEqual(responses[0]["id"], 40)

    def test_fast_call_overtakes_slow_call_and_cancellation_is_scoped(self):
        slow_started = threading.Event()
        fast_finished = threading.Event()
        responses = []
        def tool(name, args):
            event = current_event()
            if name == "slow":
                slow_started.set()
                if not event.wait(5):
                    raise AssertionError("cancellation never arrived")
                outcome = "cancelled"
            else:
                self.assertFalse(event.is_set())
                fast_finished.set()
                outcome = "success"
            return {"text": name, "result": {"outcome": outcome}}
        with mock.patch.object(server, "call_tool", tool), mock.patch.object(server, "runtime_status", return_value={}), \
                mock.patch.object(server, "send", lambda value, **kwargs: responses.append(value)):
            dispatcher = server.Dispatcher()
            try:
                dispatcher.dispatch({"id": 1, "method": "tools/call", "params": {"name": "slow"}})
                self.assertTrue(slow_started.wait(2))
                dispatcher.dispatch({"id": 2, "method": "tools/call", "params": {"name": "fast"}})
                self.assertTrue(fast_finished.wait(2))
                deadline = time.monotonic() + 2
                while not responses and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(responses[0]["id"], 2)
                dispatcher.dispatch({"method": "notifications/cancelled", "params": {"requestId": 1}})
            finally:
                dispatcher.close()
        self.assertEqual([response["id"] for response in responses], [2, 1])
        self.assertEqual(responses[-1]["result"]["structuredContent"]["outcome"], "cancelled")
