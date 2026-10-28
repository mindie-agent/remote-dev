from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.core.job_ops as job_ops
import remote_dev.core.state_store as state_store
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError, error_details
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS


def _supervisor(**overrides):
    row = {
        "state": "running",
        "quiet": False,
        "receipt": {"pid": 42, "supervision": "subreaper"},
        "processes": [{"pid": 42}],
        "unknown": [],
        "result": None,
        "remote_dir": "/srv/app/.remote-dev/jobs/job-test123",
        "gate_open": True,
    }
    row.update(overrides)
    return row


class RemoteJobControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_root = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.temp.name)  # type: ignore[assignment]
        self.addCleanup(setattr, state_store, "substrate_root", original_root)
        self.endpoint = Endpoint(host="127.0.0.1", port=46000, root="/srv/app", cwd="/srv/app")
        self.job_id = "job-test123"
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": self.job_id,
            "target": self.endpoint.to_result_target(),
            "remote_dir": f"{self.endpoint.root}/.remote-dev/jobs/{self.job_id}",
            "started_at": "2026-09-01T00:00:00Z",
            "state": "running",
            "stdin_cursors": {"stdout_offset": 0, "stderr_offset": 0},
            "authorization": {"token": "secret-start-gate-token", "job_id": self.job_id},
        }
        state_store.atomic_write_json(state_store.job_record_path(self.endpoint, self.job_id), record)
        self.record_path = state_store.job_record_path(self.endpoint, self.job_id)

    def _status(self, supervisor) -> dict:
        with mock.patch.object(job_ops, "control", return_value=supervisor):
            return job_ops.remote_job_status(self.endpoint, job_id=self.job_id)["result"]

    def test_running_supervisor_is_reported_as_running(self) -> None:
        result = self._status(_supervisor(state="running", quiet=False))
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["outcome"], "success")
        self.assertFalse(result["job"]["quiet"])
        self.assertEqual(result["job"]["remote_status"]["state"], "running")
        self.assertNotIn("authorization", result["job"])
        self.assertEqual(json.loads(self.record_path.read_text(encoding="utf-8"))["authorization"]["token"], "secret-start-gate-token")

    def test_succeeded_supervisor_is_reported_as_succeeded(self) -> None:
        result = self._status(_supervisor(state="succeeded", quiet=True, result={"state": "succeeded", "exit_code": 0}))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["job"]["quiet"])

    def test_uncertain_supervisor_is_not_quiet_success_of_the_job(self) -> None:
        result = self._status(_supervisor(state="uncertain", quiet=False, unknown=["supervisor lost"]))
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["outcome"], "failed")
        self.assertFalse(result["job"]["quiet"])

    def test_tail_uses_supervisor_logs_not_shell_sentinels(self) -> None:
        supervisor = _supervisor(stdout="worker boot ok", stderr="no errors")
        with mock.patch.object(job_ops, "control", return_value=supervisor) as mocked:
            payload = job_ops.remote_job_tail(self.endpoint, job_id=self.job_id)
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[2], "tail")
        result = payload["result"]
        self.assertEqual(result["missing_logs"], [])
        self.assertEqual(result["status"], "ok")
        self.assertIn("worker boot ok", payload["text"])
        self.assertIn("no errors", payload["text"])

    def test_missing_log_files_are_reported(self) -> None:
        supervisor = _supervisor()
        with mock.patch.object(job_ops, "control", return_value=supervisor):
            result = job_ops.remote_job_tail(self.endpoint, job_id=self.job_id)["result"]
        self.assertEqual(result["missing_logs"], ["stdout", "stderr"])
        self.assertEqual(result["status"], "log_not_found")
        self.assertEqual(result["outcome"], "failed")

    def test_stop_quiet_cancelled_is_cancelled(self) -> None:
        supervisor = _supervisor(state="cancelled", quiet=True, result={"state": "cancelled"})
        with mock.patch.object(job_ops, "control", return_value=supervisor):
            result = job_ops.remote_job_stop(self.endpoint, job_id=self.job_id)["result"]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["outcome"], "cancelled")

    def test_status_failure_omits_authorization_and_exposes_actionable_text(self) -> None:
        error = RemoteExecutionError(
            "ssh: connect to host 127.0.0.1 port 46000: Connection refused",
            category="remote_execution",
            submission_state="not_sent",
        )
        with mock.patch.object(job_ops, "control", side_effect=error):
            payload = job_ops.remote_job_status(self.endpoint, job_id=self.job_id)
        result = payload["result"]
        self.assertEqual(result["outcome"], "failed")
        self.assertNotIn("authorization", result["job"])
        self.assertNotIn("secret-start-gate-token", json.dumps(result))
        self.assertNotIn("secret-start-gate-token", payload["text"])
        self.assertIn(self.job_id, payload["text"])
        self.assertIn("not_sent", payload["text"])
        self.assertIn("Connection refused", payload["text"])
        saved = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["authorization"]["token"], "secret-start-gate-token")

    def test_stop_still_running_is_failed(self) -> None:
        supervisor = _supervisor(state="running", quiet=False)
        with mock.patch.object(job_ops, "STOP_DRAIN_SECONDS", 0):
            with mock.patch.object(job_ops, "control", return_value=supervisor):
                result = job_ops.remote_job_stop(self.endpoint, job_id=self.job_id, force=True)["result"]
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["status"], "running")

    @unittest.skipUnless(os.name == "posix", "requires POSIX SIGTERM/SIGKILL escalation")
    def test_stop_sigterm_ignored_subprocess_waits_for_sigkill_and_returns_cancelled(self) -> None:
        import signal
        import subprocess
        import sys
        import threading
        import time

        self.assertGreaterEqual(job_ops._default_stop_drain_seconds(), 5.0)
        with mock.patch.dict(os.environ, {"REMOTE_DEV_STOP_GRACE_SECONDS": "0.12", "REMOTE_DEV_STOP_DRAIN_SECONDS": "1.5"}):
            self.assertAlmostEqual(job_ops._stop_drain_seconds(), 1.5)
            ready_file = Path(self.temp.name) / "sigterm_ignored.ready"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib, signal, time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"pathlib.Path({str(ready_file)!r}).write_text('ready'); "
                        "time.sleep(30)"
                    ),
                ]
            )
            self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
            deadline = time.monotonic() + 2.0
            while not ready_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_file.exists())

            state_lock = threading.Lock()
            current = _supervisor(
                state="running",
                quiet=False,
                processes=[{"pid": proc.pid}],
                result=None,
            )

            def reap_after_grace() -> None:
                grace = float(os.environ["REMOTE_DEV_STOP_GRACE_SECONDS"])
                try:
                    proc.terminate()
                except TimeoutError:
                    raise
                except OSError:
                    pass
                time.sleep(grace)
                # Process ignored SIGTERM, so it must still be alive before SIGKILL escalation.
                still_alive_after_grace = proc.poll() is None
                try:
                    proc.kill()
                except TimeoutError:
                    raise
                except OSError:
                    pass
                proc.wait(timeout=2.0)
                with state_lock:
                    current.update(
                        state="cancelled",
                        quiet=True,
                        processes=[],
                        result={
                            "state": "cancelled",
                            "exit_code": proc.returncode,
                            "descendants_drained": still_alive_after_grace and proc.poll() is not None,
                        },
                    )

            def fake_control(_endpoint, _job_id, action, **_params):
                if action == "stop":
                    threading.Thread(target=reap_after_grace, daemon=True).start()
                with state_lock:
                    return dict(current)

            started = time.monotonic()
            with mock.patch.object(job_ops, "control", side_effect=fake_control):
                payload = job_ops.remote_job_stop(self.endpoint, job_id=self.job_id, force=False)
            elapsed = time.monotonic() - started

        result = payload["result"]
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertLess(elapsed, 1.5)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["descendants_drained"])
        self.assertTrue(result["quiet"])
        self.assertTrue(result["remote_status"]["result"]["descendants_drained"])

    def test_other_task_job_is_rejected_before_remote_control(self) -> None:
        original = self.record_path.read_bytes()
        child_root = Path(self.temp.name) / "separate-task"
        calls = (job_ops.remote_job_status, job_ops.remote_job_tail,
                 job_ops.remote_job_stdin, job_ops.remote_job_stop)
        with mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": str(child_root)}), \
                mock.patch.object(job_ops, "control") as control, \
                mock.patch("remote_dev.observability.confirmed_failure") as report:
            for function in calls:
                for endpoint in (self.endpoint, None):
                    with self.subTest(function=function.__name__, explicit_endpoint=endpoint is not None):
                        with self.assertRaises(FileNotFoundError) as caught:
                            function(endpoint, job_id=self.job_id)
                        details = error_details(caught.exception)
                        self.assertEqual(details["category"], "caller")
                        self.assertEqual(details["submission_state"], "not_sent")
                        self.assertFalse(details["retryable"])
                        self.assertNotIn(str(self.record_path), str(caught.exception))
                        self.assertNotIn("secret-start-gate-token", str(caught.exception))
            control.assert_not_called()
            report.assert_not_called()
        self.assertEqual(self.record_path.read_bytes(), original)
        child_record = child_root / "endpoints" / self.endpoint.endpoint_id / "jobs" / f"{self.job_id}.json"
        self.assertFalse(child_record.exists())

    def test_missing_job_does_not_disclose_other_task_existence(self) -> None:
        child_root = Path(self.temp.name) / "separate-task"
        original = self.record_path.read_bytes()
        messages = []
        with mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": str(child_root)}):
            for parent_exists in (True, False):
                if not parent_exists:
                    self.record_path.unlink()
                with self.assertRaises(FileNotFoundError) as caught:
                    job_ops._load_record(self.endpoint, self.job_id)
                messages.append((str(caught.exception), error_details(caught.exception)))
        self.assertEqual(messages[0], messages[1])
        self.record_path.write_bytes(original)

    def test_unreadable_or_corrupt_record_is_not_missing_job(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("unreadable")):
            with self.assertRaises(PermissionError) as caught:
                job_ops._load_record(self.endpoint, self.job_id)
        self.assertNotEqual(error_details(caught.exception)["category"], "caller")
        self.record_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError) as caught:
            job_ops._load_record(self.endpoint, self.job_id)
        self.assertNotEqual(error_details(caught.exception)["category"], "caller")
        self.assertEqual(self.record_path.read_text(encoding="utf-8"), "{bad")

    def test_stop_transport_uncertainty_is_not_changed_to_not_sent(self) -> None:
        error = RemoteExecutionError("response lost", category="rpc_disconnected",
                                     submission_state="uncertain")
        with mock.patch.object(job_ops, "control", side_effect=error) as control:
            result = job_ops.remote_job_stop(self.endpoint, job_id=self.job_id)["result"]
        control.assert_called_once()
        self.assertEqual(result["error_details"]["category"], "rpc_disconnected")
        self.assertEqual(result["error_details"]["submission_state"], "uncertain")
        self.assertEqual(result["outcome"], "failed")


class TailBudgetAndDeadlineTests(unittest.TestCase):
    FULL = "a" * 64

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_root = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.temp.name)  # type: ignore[assignment]
        self.addCleanup(setattr, state_store, "substrate_root", original_root)
        self.endpoint = Endpoint(host="127.0.0.1", port=46000, root="/srv/app", cwd="/srv/app",
                                 container=self.FULL)
        self.job_id = "job-budget1"
        state_store.atomic_write_json(
            state_store.job_record_path(self.endpoint, self.job_id),
            {"job_id": self.job_id, "target": self.endpoint.to_result_target(),
             "remote_dir": "/srv/app/.remote-dev/jobs/job-budget1"})

    def _tail(self, stdout="", stderr="", tool=None, **kwargs):
        tool = tool or job_ops.remote_job_tail
        with mock.patch.object(job_ops, "control", return_value=_supervisor(stdout=stdout, stderr=stderr)):
            return tool(self.endpoint, job_id=self.job_id, **kwargs)

    def test_tiny_budget_keeps_newest_log_bytes_not_labels(self) -> None:
        for tool in (job_ops.remote_job_tail, job_ops.remote_job_output):
            payload = self._tail("DATA-ABCD", "", tool=tool, max_output_tokens=1)
            result = payload["result"]
            self.assertEqual(result["preview"]["tail"], "ABCD")
            self.assertEqual(result["omitted_bytes"], 5)
            self.assertTrue(result["truncated"])
            self.assertIn("ABCD", payload["text"])
            self.assertNotIn("DATA-", payload["text"])

    def test_budget_both_streams_and_unicode(self) -> None:
        payload = self._tail("out-1234\n", "err-\u4f60\u597d\n", max_output_tokens=3)
        result = payload["result"]
        body = result["preview"]["tail"]
        self.assertLessEqual(len(body.encode()), 6)
        self.assertTrue(body.endswith("\u597d\n") or body.endswith("\u597d"))
        self.assertEqual(result["omitted_bytes"], len("out-1234\nerr-\u4f60\u597d\n".encode()) - len(body.encode()))
        body.encode().decode("utf-8")

    def test_budget_selects_real_tail_before_repeat_folding(self) -> None:
        raw = "repeat-long-line\n" * 100
        result = self._tail(raw, "", max_output_tokens=1)["result"]
        self.assertEqual(result["preview"]["tail"], raw[-4:])
        self.assertEqual(result["omitted_bytes"], len(raw.encode()) - 4)
        self.assertFalse(result["repeated_lines_folded"])
        unbudgeted = self._tail(raw, "")["result"]
        self.assertTrue(unbudgeted["repeated_lines_folded"])
        self.assertTrue(unbudgeted["truncated"])
        self.assertGreater(unbudgeted["omitted_bytes"], 0)

    def test_budget_preserves_trailing_newline(self) -> None:
        result = self._tail("abc\n", "", max_output_tokens=2)["result"]
        self.assertEqual(result["preview"]["tail"], "abc\n")
        self.assertEqual(result["omitted_bytes"], 0)

    def test_text_limit_truncation_reports_omitted_raw_bytes(self) -> None:
        raw = "".join(f"line {i}\n" for i in range(5000))
        for kwargs in ({}, {"max_output_tokens": 100000}):
            result = self._tail(raw, "", **kwargs)["result"]
            self.assertTrue(result["truncated"], kwargs)
            self.assertGreater(result["omitted_bytes"], 0)
            kept = len(raw.encode()) - result["omitted_bytes"]
            self.assertGreater(kept, 0)
            self.assertLess(kept, len(raw.encode()))

    def test_untruncated_reports_zero_and_empty_streams(self) -> None:
        result = self._tail("", "", max_output_tokens=1)["result"]
        self.assertEqual((result["truncated"], result["omitted_bytes"]), (False, 0))
        result = self._tail("small", "")["result"]
        self.assertEqual((result["truncated"], result["omitted_bytes"]), (False, 0))

    def _clock(self):
        state = {"now": 1000.0}
        fake = mock.Mock()
        fake.monotonic = lambda: state["now"]
        fake.sleep = lambda seconds: state.__setitem__("now", state["now"] + seconds)
        return state, fake

    def test_pin_time_is_deducted_before_control(self) -> None:
        for tool in (job_ops.remote_job_tail, job_ops.remote_job_output):
            state, fake = self._clock()
            selector = Endpoint(host="127.0.0.1", port=46000, root="/srv/app", cwd="/srv/app",
                                container="named")

            def pin(endpoint, *, timeout_ms=None):
                if endpoint.container != "named":
                    return endpoint
                self.assertEqual(timeout_ms, 1000)
                state["now"] += 0.9
                return self.endpoint

            seen = {}

            def fake_control(_endpoint, _job, _action, **params):
                seen.update(params)
                return _supervisor(stdout="ok", stderr="")

            with mock.patch.object(job_ops, "time", fake), \
                    mock.patch.object(job_ops._container_endpoint, "pin_container_endpoint", pin), \
                    mock.patch.object(job_ops, "control", fake_control):
                tool(selector, job_id=self.job_id, timeout_ms=1000)
            self.assertLessEqual(seen["timeout_ms"], 101)
            self.assertGreaterEqual(seen["timeout_ms"], 1)

    def test_expired_pin_prevents_control(self) -> None:
        state, fake = self._clock()
        selector = Endpoint(host="127.0.0.1", port=46000, root="/srv/app", cwd="/srv/app",
                            container="named")

        def pin(endpoint, *, timeout_ms=None):
            if endpoint.container != "named":
                return endpoint
            state["now"] += 1.0
            return self.endpoint

        with mock.patch.object(job_ops, "time", fake), \
                mock.patch.object(job_ops._container_endpoint, "pin_container_endpoint", pin), \
                mock.patch.object(job_ops, "control") as control:
            with self.assertRaises(TimeoutError):
                job_ops.remote_job_tail(selector, job_id=self.job_id, timeout_ms=1000)
        control.assert_not_called()

    def test_expired_record_resolution_prevents_control(self) -> None:
        state, fake = self._clock()
        original = job_ops._load_record

        def slow_load(endpoint, job_id):
            state["now"] += 1.0
            return original(endpoint, job_id)

        with mock.patch.object(job_ops, "time", fake), \
                mock.patch.object(job_ops, "_load_record", slow_load), \
                mock.patch.object(job_ops, "control") as control:
            with self.assertRaises(TimeoutError):
                job_ops.remote_job_output(self.endpoint, job_id=self.job_id, timeout_ms=1000)
        control.assert_not_called()


class StartRemoteJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_root = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.temp.name)  # type: ignore[assignment]
        self.addCleanup(setattr, state_store, "substrate_root", original_root)
        self.endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app", cwd="/srv/app")

    def test_start_combines_prepare_and_go_through_control(self) -> None:
        calls = []

        def fake_control(_endpoint, job_id, action, **params):
            calls.append((action, params))
            if action == "launch":
                return _supervisor(state="running", remote_dir=f"/srv/app/.remote-dev/jobs/{job_id}")
            raise AssertionError(action)

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(self.endpoint, command="echo ok", job_id="job-start1")
        self.assertEqual([item[0] for item in calls], ["launch"])
        self.assertEqual(calls[0][1]["spec"]["command"], "echo ok")
        self.assertEqual(calls[0][1]["spec"]["cwd"], "/srv/app")
        self.assertIn("authorization", calls[0][1])
        self.assertEqual(payload["result"]["status"], "running")
        self.assertEqual(payload["result"]["outcome"], "success")
        record = state_store.read_json(Path(payload["result"]["refs"]["job_record"]))
        self.assertEqual(record["job_id"], "job-start1")
        self.assertEqual(record["remote_dir"], "/srv/app/.remote-dev/jobs/job-start1")

    def test_start_does_not_build_a_nohup_shell_runner(self) -> None:
        with mock.patch.object(job_ops, "control", return_value=_supervisor(state="prepared", gate_open=True)):
            job_ops.start_remote_job(self.endpoint, command="sleep 1", job_id="job-nopath")
        # control is the only remote path; there is no run_script/nohup helper left.
        self.assertFalse(hasattr(job_ops, "run_script"))

    def test_runtime_env_is_folded_into_the_supervisor_command(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app", cwd="/srv/app", runtime_env_file="/etc/profile.d/toolchain.sh")
        calls = []

        def fake_control(_endpoint, job_id, action, **params):
            calls.append((action, params))
            return _supervisor(state="prepared" if action == "prepare" else "running", gate_open=action == "go")

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(endpoint, command="echo ok", job_id="job-runtime-env")
        command = calls[0][1]["spec"]["command"]
        self.assertIn("/etc/profile.d/toolchain.sh", command)
        self.assertIn("echo ok", command)
        record = state_store.read_json(Path(payload["result"]["refs"]["job_record"]))
        self.assertEqual(record["runtime_env_file"], "/etc/profile.d/toolchain.sh")
        restored = job_ops.endpoint_from_job_record(record)
        self.assertEqual(restored.runtime_env_file, "/etc/profile.d/toolchain.sh")

    def test_start_uncertain_failure_text_includes_original_job_id(self) -> None:
        error = RemoteExecutionError(
            "ssh: connect to host 1.2.3.4 port 46000: Connection timed out",
            category="remote_execution",
            submission_state="uncertain",
        )
        with mock.patch.object(job_ops, "control", side_effect=error):
            payload = job_ops.start_remote_job(self.endpoint, command="echo ok", job_id="job-uncertain-1")
        self.assertEqual(payload["result"]["status"], "submission_uncertain")
        self.assertIn("job-uncertain-1", payload["text"])
        self.assertIn("uncertain", payload["text"])
        self.assertIn("Connection timed out", payload["text"])
        self.assertNotIn("authorization", json.dumps(payload["result"]))
        record = state_store.read_json(state_store.job_record_path(self.endpoint, "job-uncertain-1"))
        self.assertIn("authorization", record)
        self.assertNotIn(record["authorization"]["token"], payload["text"])

    def test_missing_cwd_does_not_open_the_start_gate(self) -> None:
        calls = []

        def fake_control(_endpoint, _job_id, action, **params):
            calls.append(action)
            raise FileNotFoundError("command cwd does not exist")

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.start_remote_job(self.endpoint, command="touch should-not-exist", cwd="/srv/app/missing", job_id="job-missing")
        self.assertEqual(calls, ["launch"])
        self.assertEqual(payload["result"]["outcome"], "failed")
        self.assertEqual(payload["result"]["status"], "cwd_not_found")

    def test_duplicate_local_job_id_is_blocked_without_control(self) -> None:
        state_store.atomic_write_json(
            state_store.job_record_path(self.endpoint, "job-existing"),
            {"job_id": "job-existing", "target": self.endpoint.to_result_target()},
        )
        with mock.patch.object(job_ops, "control", side_effect=AssertionError("control must not run")):
            payload = job_ops.start_remote_job(self.endpoint, command="echo ok", job_id="job-existing")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "job_id_exists")

    def test_tail_clamps_lines(self) -> None:
        job_id = "job-tail-test"
        state_store.atomic_write_json(
            state_store.job_record_path(self.endpoint, job_id),
            {"job_id": job_id, "target": self.endpoint.to_result_target(), "remote_dir": "/srv/app/.remote-dev/jobs/job-tail-test"},
        )
        seen = {}

        def fake_control(_endpoint, _job_id, action, **params):
            seen.update(params)
            return _supervisor(stdout="x" * (MAX_TEXT_CHARS * 2), stderr="")

        with mock.patch.object(job_ops, "control", fake_control):
            payload = job_ops.remote_job_tail(None, job_id=job_id, lines=100000)
        self.assertEqual(seen["lines"], MAX_JOB_TAIL_LINES)
        self.assertIn("clamped", payload["result"]["warnings"][0])
        self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)

    def test_tail_folds_consecutive_repeated_log_lines_preserving_diagnostics(self) -> None:
        job_id = "job-tail-repeat-fold"
        state_store.atomic_write_json(
            state_store.job_record_path(self.endpoint, job_id),
            {"job_id": job_id, "target": self.endpoint.to_result_target(), "remote_dir": "/srv/app/.remote-dev/jobs/job-tail-repeat-fold"},
        )
        noisy_stdout = (
            "[1/64] Building AscendC kernel tilings...\n"
            + ("warning: unused parameter 'workspace_ptr' [-Wunused-parameter]\n" * 400)
            + ("retry_probe: waiting for HCCL rank 0...\n" * 2)
            + "error: AICore UB overflow at fused_rmsnorm.cpp:142 (expected <= 196608B, got 262144B)\n"
        )
        with mock.patch.object(job_ops, "control", return_value=_supervisor(stdout=noisy_stdout, stderr="")):
            payload = job_ops.remote_job_tail(None, job_id=job_id, lines=500)
        text = payload["text"]
        self.assertIn("[1/64] Building AscendC kernel tilings...", text)
        self.assertIn("[previous line repeated 399 more times]", text)
        self.assertEqual(text.count("retry_probe: waiting for HCCL rank 0..."), 2)
        self.assertIn("error: AICore UB overflow at fused_rmsnorm.cpp:142", text)
        self.assertNotIn("<remote-dev text truncated", text)

    def test_running_session_never_reports_exit_code_in_summary(self) -> None:
        row = _supervisor(
            state="running",
            quiet=False,
            result={"state": "succeeded", "exit_code": 0, "descendants_drained": False},
            stdout="still draining\n",
            stdout_offset=15,
            stdout_bytes_remaining=0,
            stderr="",
            stderr_offset=0,
            stderr_bytes_remaining=0,
        )
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops.start_remote_job(self.endpoint, command="echo draining", job_id="job-draining")
        self.assertEqual(payload["result"]["status"], "running")
        self.assertIsNone(payload["result"]["exit_code"])
        self.assertNotIn("Exit code:", payload["result"]["summary"])


@unittest.skipUnless(sys.platform == "linux", "exercises the Linux remote process worker")
class WorkerExchangeAndSubreaperStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        from remote_dev.processes import worker as worker_module

        self.worker = worker_module
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _create_job_dir(self, job_id: str, *, supervision: str = "subreaper") -> Path:
        job_dir = self.root / ".remote-dev" / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "receipt.json").write_text(
            json.dumps(
                {
                    "pid": 4242,
                    "ppid": 1,
                    "pgid": 4242,
                    "start_ticks": "12345",
                    "boot_id": "boot-test-1",
                    "marker": "m" * 32,
                    "supervision": supervision,
                    "job_id": job_id,
                    "prepared_at": 1000.0,
                }
            ),
            encoding="utf-8",
        )
        (job_dir / "go.json").write_text(
            json.dumps({"authorization": {"token": "t"}, "opened_at": 1000.0, "valid_until": None}),
            encoding="utf-8",
        )
        return job_dir

    def test_job_status_with_drained_result_and_exiting_subreaper_reports_succeeded_and_quiet(self) -> None:
        job_dir = self._create_job_dir("job-subreaper-race")
        (job_dir / "result.json").write_text(
            json.dumps({"state": "succeeded", "exit_code": 0, "descendants_drained": True, "finished_at": 1001.0}),
            encoding="utf-8",
        )
        exiting_anchor = {"pid": 4242, "ppid": 1, "pgid": 4242, "start_ticks": "12345", "state": "R"}
        with mock.patch.object(self.worker, "boot_id", return_value="boot-test-1"), \
                mock.patch.object(self.worker, "process_identity", return_value=exiting_anchor):
            status = self.worker.job_status(job_dir)
        self.assertEqual(status["state"], "succeeded")
        self.assertTrue(status["quiet"])
        self.assertEqual(status["processes"], [])
        self.assertEqual(status["result"]["exit_code"], 0)

    def test_fast_command_emitting_output_and_exiting_within_50ms_completes_in_single_exchange(self) -> None:
        import threading
        import time

        job_id = "job-fast-coalesce"
        job_dir = self._create_job_dir(job_id)
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")

        def writer() -> None:
            time.sleep(0.005)
            with stdout_path.open("ab") as stream:
                stream.write(b"first line\n")
                stream.flush()
            time.sleep(0.035)
            with stdout_path.open("ab") as stream:
                stream.write(b"second line\n")
                stream.flush()
            self.worker.atomic_json(
                job_dir / "result.json",
                {"state": "succeeded", "exit_code": 0, "descendants_drained": True, "finished_at": time.time()},
            )

        thread = threading.Thread(target=writer, daemon=True)
        exiting_anchor = {"pid": 4242, "ppid": 1, "pgid": 4242, "start_ticks": "12345", "state": "R"}
        with mock.patch.object(self.worker, "boot_id", return_value="boot-test-1"), \
                mock.patch.object(self.worker, "process_identity", return_value=exiting_anchor):
            thread.start()
            row = self.worker.control_job(
                {
                    "action": "exchange",
                    "root": str(self.root),
                    "job_id": job_id,
                    "stdout_offset": 0,
                    "stderr_offset": 0,
                    "max_bytes": 8192,
                    "shared_budget": True,
                    "wait_for_exit": False,
                    "yield_time_ms": 2000,
                },
                "",
            )
            thread.join(timeout=1)
        self.assertEqual(row["state"], "succeeded")
        self.assertTrue(row["quiet"])
        self.assertEqual(row["result"]["exit_code"], 0)
        self.assertEqual(row["stdout"], "first line\nsecond line\n")
        self.assertEqual(row["stdout_bytes_remaining"], 0)


if __name__ == "__main__":
    unittest.main()
