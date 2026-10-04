"""Linux process-level checks for the shipped remote worker.

No NPU, SSH, or coordinator. Skipped on macOS/Windows: the remote worker
target is Linux /proc + prctl child-subreaper.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from remote_dev.processes import worker_source


def load_worker():
    path = Path(__file__).resolve().parents[1] / "remote_dev" / "processes" / "worker.py"
    spec = importlib.util.spec_from_file_location("remote_dev_process_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc identity and prctl subreaper")
class ProcessWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_worker()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = worker_source()
        self.identifiers = []

    def tearDown(self):
        for identifier in self.identifiers:
            for _ in range(50):
                status = self.call(identifier, "stop", force=True)
                if status.get("quiet"):
                    break
                time.sleep(0.02)
        self.temp.cleanup()

    def call(self, identifier, action, **args):
        return self.worker.control_job(
            {"root": str(self.root), "job_id": identifier, "action": action, **args},
            self.source,
        )

    def prepare(self, letter, command, timeout=10, **spec_extra):
        identifier = "job-" + letter * 8
        self.identifiers.append(identifier)
        status = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": command, "env": {}, "timeout_seconds": timeout, **spec_extra})
        return identifier, status

    def go(self, identifier):
        return self.call(identifier, "go", authorization={"token": identifier, "kind": "test"})

    def until(self, identifier, condition):
        status = None
        for _ in range(100):
            status = self.call(identifier, "status")
            if condition(status):
                return status
            time.sleep(0.05)
        self.fail("process worker did not reach the expected bounded state: " + json.dumps(status))

    def test_waiting_gate_is_idempotent_and_command_does_not_run_early(self):
        identifier, first = self.prepare("a", "printf completed > result.txt")
        second = self.call(identifier, "prepare", spec={"cwd": str(self.root), "command": "printf completed > result.txt", "env": {}, "timeout_seconds": 10})
        self.assertEqual(first["receipt"]["pid"], second["receipt"]["pid"])
        self.assertEqual(second["state"], "prepared")
        self.assertFalse((self.root / "result.txt").exists())
        self.go(identifier)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "succeeded")
        self.assertEqual((self.root / "result.txt").read_text(), "completed")

    def test_correlation_is_receipt_metadata_not_launch_identity_and_timings_are_drained(self):
        identifier = "job-" + "z" * 8
        self.identifiers.append(identifier)
        spec = {"cwd": str(self.root), "command": "printf once >> counted; sleep .15 &", "env": {}, "timeout_seconds": 5}
        context = {"trace_id": "a" * 32, "operation_id": "b" * 32, "user": "not-authority"}
        first = self.call(identifier, "prepare", spec=spec, diagnostics_context=context)
        second = self.call(identifier, "prepare", spec=spec, diagnostics_context={"trace_id": "c" * 32})
        self.assertEqual(first["receipt"]["pid"], second["receipt"]["pid"])
        self.assertEqual(second["receipt"]["diagnostics_context"], {"trace_id": "a" * 32, "operation_id": "b" * 32})
        self.go(identifier)
        result = self.until(identifier, lambda row: row["quiet"])["result"]
        self.assertTrue(result["descendants_drained"])
        self.assertEqual((self.root / "counted").read_text(), "once")
        self.assertGreater(result["timings"]["descendant_drain_ms"], 50)
        self.assertGreaterEqual(result["timings"]["spawn_ms"], 0)
        self.assertGreaterEqual(result["timings"]["shell_ms"], 0)

    def test_stop_clean_environment_daemon_keeps_the_other_family_alive(self):
        command = (
            "setsid env -u REMOTE_DEV_JOB_TOKEN "
            + shlex.quote(sys.executable)
            + " -c "
            + shlex.quote("import os,time; from pathlib import Path; Path('daemon.pid').write_text(str(os.getpid())); time.sleep(60)")
            + " &"
        )
        a, _ = self.prepare("a", command, timeout=60)
        b, before = self.prepare("b", "sleep 60 & wait", timeout=60)
        self.go(a)
        self.go(b)
        self.until(a, lambda row: (self.root / "daemon.pid").exists())
        daemon = int((self.root / "daemon.pid").read_text())
        observed = self.call(a, "status")
        self.assertIn(daemon, [row["pid"] for row in observed["processes"]])
        self.assertNotIn(b"REMOTE_DEV_JOB_TOKEN=", Path(f"/proc/{daemon}/environ").read_bytes())
        self.assertNotEqual(self.worker.process_identity(daemon)["pgid"], observed["receipt"]["pgid"])
        self.call(a, "stop", force=True)
        self.until(a, lambda row: row["quiet"])
        self.assertIsNone(self.worker.process_identity(daemon))
        after = self.call(b, "status")
        self.assertFalse(after["quiet"])
        self.assertEqual(before["receipt"]["pid"], after["receipt"]["pid"])

    def test_explicit_prepared_deadline_expires_without_running_command(self):
        identifier, prepared = self.prepare("p", "touch should-not-run", prepared_timeout_seconds=1)
        self.assertEqual(prepared["receipt"]["prepared_timeout_seconds"], 1)
        result = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["result"]["reason"], "start gate not opened")
        self.assertFalse((self.root / "should-not-run").exists())
        with self.assertRaisesRegex(RuntimeError, "not a verified waiting supervisor"):
            self.go(identifier)

    def test_prepared_wait_does_not_consume_command_runtime_timeout(self):
        identifier, _ = self.prepare("q", "printf ran > completed", timeout=0.2, prepared_timeout_seconds=2)
        time.sleep(0.4)
        self.assertEqual(self.call(identifier, "status")["state"], "prepared")
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "succeeded")
        self.assertEqual((self.root / "completed").read_text(), "ran")

    def test_prepared_deadline_default_long_override_and_invalid_values(self):
        _, default = self.prepare("r", "true")
        self.assertEqual(default["receipt"]["prepared_timeout_seconds"], 120)
        _, queued = self.prepare("s", "true", prepared_timeout_seconds=7200)
        self.assertEqual(queued["receipt"]["prepared_timeout_seconds"], 7200)
        for value in (None, 0, 0.5, -1, True, 86401, float("inf"), float("nan"), "120"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "prepared_timeout_seconds"):
                self.call("job-invalid-timeout", "prepare", spec={"cwd":str(self.root), "command":"true",
                          "env":{}, "timeout_seconds":10, "prepared_timeout_seconds":value})

    def test_stop_before_go_never_executes_and_unknown_receipt_is_not_free(self):
        identifier, _ = self.prepare("a", "touch should-not-exist")
        self.call(identifier, "stop", force=True)
        self.until(identifier, lambda row: row["quiet"])
        with self.assertRaises(RuntimeError):
            self.go(identifier)
        self.assertFalse((self.root / "should-not-exist").exists())
        directory = self.root / ".remote-dev/jobs" / "job-bbbbbbbb"
        directory.mkdir(parents=True)
        (directory / "intent.json").write_text("{}")
        status = self.call("job-bbbbbbbb", "status")
        self.assertEqual(status["state"], "uncertain")
        self.assertFalse(status["quiet"])

    def test_timeout_is_not_reported_as_a_success_or_manual_cancel(self):
        identifier, _ = self.prepare("a", "sleep 60", timeout=1)
        self.go(identifier)
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "timeout")

    def test_background_descendant_cannot_outlive_the_bounded_execution_unobserved(self):
        identifier, _ = self.prepare("a", "sleep 60 &", timeout=1)
        self.go(identifier)
        observed = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(observed["state"], "timeout")
        self.assertTrue(observed["result"]["descendants_drained"])

    def test_lost_supervisor_cannot_report_quiet(self):
        identifier, first = self.prepare("a", "touch should-not-run")
        os.kill(first["receipt"]["pid"], signal.SIGKILL)
        observed = self.until(identifier, lambda row: not row.get("processes"))
        self.assertEqual(observed["state"], "uncertain")
        self.assertFalse(observed["quiet"])
        self.assertTrue(first["receipt"]["process_guard"].get("retain_until_release"))
        self.assertFalse((self.root / "should-not-run").exists())

    def test_legacy_receipt_stop_without_result_reports_cancelled(self):
        identifier = "job-cccccccc"
        directory = self.root / ".remote-dev/jobs" / identifier
        directory.mkdir(parents=True)
        pid = 2**22
        while Path(f"/proc/{pid}").exists():
            pid -= 1
        (directory / "receipt.json").write_text(json.dumps({
            "pid": pid, "ppid": 1, "pgid": pid, "start_ticks": "0",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "marker": "d" * 32, "job_id": identifier, "prepared_at": time.time()}))
        (directory / "stop.json").write_text("{}")
        observed = self.call(identifier, "status")
        self.assertEqual(observed["state"], "cancelled")
        self.assertTrue(observed["quiet"])

    def test_jobs_live_under_remote_dev_not_vaws_runtime(self):
        identifier, status = self.prepare("z", "true")
        remote_dir = Path(status["remote_dir"])
        self.assertEqual(remote_dir, self.root / ".remote-dev" / "jobs" / identifier)
        self.assertFalse((self.root / ".vaws-runtime").exists())

    def test_interactive_stdin_pipe_roundtrip(self):
        # Real FIFO->pipe proxy: bytes written through the "stdin" action reach
        # the child, output is read incrementally, and eof lets the job finish.
        identifier, status = self.prepare(
            "i", "while read -r line; do printf 'got:%s\\n' \"$line\"; done", timeout=10, interactive=True)
        self.assertEqual(status["state"], "prepared")
        directory = self.root / ".remote-dev/jobs" / identifier
        self.assertTrue((directory / "stdin.pipe").exists())
        self.go(identifier)
        reply = self.call(identifier, "stdin", data="hello\n")
        self.assertTrue(reply["accepted"])
        self.assertEqual(reply["written"], 6)
        seen = {"offset": 0, "text": ""}

        def echoed(_row):
            row = self.call(identifier, "tail", stdout_offset=seen["offset"], max_bytes=4096)
            seen["offset"] = row.get("stdout_offset", seen["offset"])
            seen["text"] += row.get("stdout", "")
            return "got:hello" in seen["text"]

        self.until(identifier, echoed)
        # Incremental cursor: a re-read at the same offset returns nothing new.
        row = self.call(identifier, "tail", stdout_offset=seen["offset"], max_bytes=4096)
        self.assertEqual(row.get("stdout", ""), "")
        reply = self.call(identifier, "stdin", data="", eof=True)
        self.assertTrue(reply["accepted"])
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["state"], "succeeded")
        # Data writes after eof are refused; output polls still work.
        reply = self.call(identifier, "stdin", data="late\n")
        self.assertFalse(reply["accepted"])
        reply = self.call(identifier, "stdin", data="")
        self.assertTrue(reply["accepted"])
        self.assertTrue(reply["polled_terminal"])

    def read_until(self, identifier, needle, *, data="", eof=False):
        output = ""
        offset = 0
        for index in range(60):
            row = self.call(identifier, "exchange", data=data if index == 0 else "",
                            eof=eof if index == 0 else False, stdout_offset=offset,
                            stderr_offset=0, shared_budget=True, max_bytes=32768, yield_time_ms=100)
            output += row.get("stdout", "")
            offset = row.get("stdout_offset", offset)
            if needle in output:
                return output
        self.fail("missing output: " + repr(output))

    def test_pty_has_terminal_size_unicode_input_and_ctrl_c_signal(self):
        code = ("import os,sys,time; print('READY', os.isatty(0), os.isatty(1), "
                "os.get_terminal_size(0), flush=True); "
                "print('GOT:' + input(), flush=True); time.sleep(30)")
        identifier, _ = self.prepare("p", shlex.quote(sys.executable) + " -u -c " + shlex.quote(code), tty=True)
        self.go(identifier)
        ready = self.read_until(identifier, "READY True True")
        self.assertIn("columns=80, lines=24", ready)
        output = self.read_until(identifier, "GOT:你好", data="你好\n")
        self.assertIn("你好", output)
        self.call(identifier, "stdin", data="\x03")
        status = self.until(identifier, lambda row: row["quiet"])
        self.assertIn(status["result"]["exit_code"], (-signal.SIGINT, 128 + signal.SIGINT))
        self.assertTrue(status["result"]["descendants_drained"])

    def test_pty_eof_after_complete_line_allows_cat_to_finish(self):
        identifier, _ = self.prepare("e", "cat", tty=True)
        self.go(identifier)
        self.read_until(identifier, "hello", data="hello\n")
        self.call(identifier, "stdin", data="", eof=True)
        status = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(status["result"]["exit_code"], 0)

    def test_pipe_control_byte_is_data_and_does_not_signal(self):
        code = "import sys; print('BYTE', sys.stdin.buffer.read(1)[0], flush=True)"
        identifier, _ = self.prepare("n", shlex.quote(sys.executable) + " -u -c " + shlex.quote(code), interactive=True)
        self.go(identifier)
        self.read_until(identifier, "BYTE 3", data="\x03")
        self.assertEqual(self.until(identifier, lambda row: row["quiet"])["result"]["exit_code"], 0)

    def test_verified_live_supervisor_never_scans_unrelated_proc_entries(self):
        identifier, _ = self.prepare("f", "sleep 10")
        self.go(identifier)
        original = Path.iterdir
        def limited(path):
            if str(path) == "/proc":
                raise AssertionError("live status must walk only the owned family")
            return original(path)
        from unittest import mock
        with mock.patch.object(Path, "iterdir", limited):
            status = self.call(identifier, "status")
            self.assertFalse(status["quiet"])
            self.call(identifier, "stop", force=True)
        self.until(identifier, lambda row: row["quiet"])

    def test_launch_and_empty_exchange_use_the_same_terminal_snapshot(self):
        identifier = "job-combined"
        self.identifiers.append(identifier)
        row = self.call(identifier, "launch", spec={"command": "printf done", "cwd": str(self.root),
                        "env": {}, "timeout_seconds": 10}, authorization={"token": "test"},
                        stdout_offset=0, stderr_offset=0, max_bytes=8, shared_budget=True, yield_time_ms=1000)
        self.until(identifier, lambda row: row["quiet"])
        last = self.call(identifier, "exchange", stdout_offset=row.get("stdout_offset", 0),
                         stderr_offset=0, max_bytes=8, shared_budget=True, yield_time_ms=100)
        self.assertEqual(row.get("stdout", "") + last.get("stdout", ""), "done")
        self.assertEqual(last["state"], "succeeded")
        self.assertEqual(last["result"]["exit_code"], 0)
        self.assertTrue(last["quiet"])

    def test_cancelled_launch_does_not_open_the_gate(self):
        import threading
        event = threading.Event()
        event.set()
        identifier = "job-never-run"
        self.identifiers.append(identifier)
        self.worker.control_job({"root": str(self.root), "job_id": identifier, "action": "launch",
                                 "spec": {"cwd": str(self.root), "command": "touch forbidden", "env": {}, "timeout_seconds": 10},
                                 "authorization": {"token": "test"}}, self.source, event)
        self.until(identifier, lambda row: row["quiet"])
        self.assertFalse((self.root / "forbidden").exists())

    def test_cancelled_exchange_waits_for_owned_family_to_drain(self):
        import threading
        event = threading.Event()
        identifier, _ = self.prepare("cancel", "sleep 30")
        self.go(identifier)
        event.set()
        row = self.worker.control_job({"root": str(self.root), "job_id": identifier,
            "action": "exchange", "stdout_offset": 0, "stderr_offset": 0,
            "wait_for_exit": True, "yield_time_ms": 30000}, self.source, event)
        self.assertEqual(row["state"], "cancelled")
        self.assertTrue(row["quiet"])
        self.assertTrue(row["cancellation_requested"])

    def test_synchronous_capture_waits_past_early_output_until_exit(self):
        identifier = "job-sync-capture"
        self.identifiers.append(identifier)
        code = "import sys,time; print('early',flush=True); time.sleep(.2); print('end')"
        started = time.monotonic()
        row = self.call(identifier, "launch", spec={"command": shlex.quote(sys.executable)+" -c "+shlex.quote(code),
                        "cwd": str(self.root), "env": {}, "timeout_seconds": 10}, authorization={"token": "test"},
                        stdout_offset=0, stderr_offset=0, max_bytes=32768, shared_budget=True,
                        wait_for_exit=True, yield_time_ms=1000)
        self.assertGreaterEqual(time.monotonic()-started, .2)
        self.assertEqual(row["stdout"], "early\nend\n")
        self.assertEqual(row["result"]["exit_code"], 0)
        self.assertTrue(row["result"]["descendants_drained"])

    def test_synchronous_capture_still_yields_and_reports_command_timeout(self):
        identifier = "job-sync-yield"
        self.identifiers.append(identifier)
        row = self.call(identifier, "launch", spec={"command": "printf early; sleep 30",
                        "cwd": str(self.root), "env": {}, "timeout_seconds": .4}, authorization={"token": "test"},
                        stdout_offset=0, stderr_offset=0, max_bytes=32768, shared_budget=True,
                        wait_for_exit=True, yield_time_ms=200)
        self.assertEqual(row["stdout"], "early")
        self.assertFalse(row["quiet"])
        terminal = self.until(identifier, lambda row: row["quiet"])
        self.assertEqual(terminal["state"], "timeout")
        self.assertTrue(terminal["result"]["descendants_drained"])

    def test_shared_budget_counts_invalid_utf8_expansion_without_losing_bytes(self):
        code = "import os; os.write(1,b'\\xff'*9+'你好'.encode()); os.write(2,'世界'.encode())"
        identifier, _ = self.prepare("bytes", shlex.quote(sys.executable) + " -c " + shlex.quote(code))
        self.go(identifier)
        self.until(identifier, lambda row: row["quiet"])
        offsets = dict(stdout_offset=0, stderr_offset=0)
        collected = dict(stdout="", stderr="")
        for _ in range(20):
            row = self.call(identifier, "tail", **offsets, max_bytes=4, shared_budget=True)
            self.assertLessEqual(sum(len(row[name].encode()) for name in collected), 4)
            for name in collected:
                collected[name] += row[name]
                offsets[name + "_offset"] = row[name + "_offset"]
            if not any(row[name + "_bytes_remaining"] for name in collected):
                break
        self.assertEqual(collected, dict(stdout='\ufffd' * 9 + '你好', stderr='世界'))



@unittest.skipIf(sys.platform == "win32", "Linux worker is not a native Windows module")
class ProcessWorkerEntrypointTests(unittest.TestCase):
    def test_precancelled_launch_does_not_prepare_or_spawn(self):
        import threading
        from unittest import mock
        worker = load_worker()
        event = threading.Event()
        event.set()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                worker.subprocess, "Popen", side_effect=AssertionError("cancelled launch must not spawn")):
            request = {"root": directory, "job_id": "job-never-run", "action": "launch",
                       "spec": {"cwd": directory, "command": "touch forbidden", "env": {},
                                "timeout_seconds": None}, "authorization": {"token": "test"}}
            result = worker.control_job(request, worker_source(), event)
            self.assertEqual(result, {"state": "absent", "quiet": True, "cancellation_requested": True})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_main_without_control_bootstrap_fails_with_clear_message(self):
        script = Path(__file__).resolve().parents[1] / "remote_dev" / "processes" / "worker.py"
        request = {"root": "/tmp", "job_id": "job-eeeeeeee", "action": "status"}
        completed = subprocess.run(
            [sys.executable, str(script), json.dumps(request)],
            capture_output=True, text=True, check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WORKER_SOURCE is undefined", completed.stderr)
        self.assertIn("remote_dev.processes.control", completed.stderr)


if __name__ == "__main__":
    unittest.main()
