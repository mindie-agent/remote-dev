from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.core.job_ops as job_ops
import remote_dev.core.state_store as state_store
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
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
        self.assertEqual(result["outcome"], "success")
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


if __name__ == "__main__":
    unittest.main()
