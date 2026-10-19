from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS  # noqa: E402
from remote_dev.core.ssh_transport import RemoteCompleted  # noqa: E402
import remote_dev.core.shell_ops as shell_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
import remote_dev.core.job_ops as job_ops  # noqa: E402


class RemoteBashTests(unittest.TestCase):
    def test_remote_bash_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="/tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "cwd_outside_root")
        self.assertEqual(
            payload["result"]["next"]["endpoint_patch"],
            {"root": "/tmp", "cwd": "/tmp"},
        )
        self.assertIn("--root /tmp --cwd /tmp", payload["text"])

    def test_remote_bash_relative_cwd_hint_does_not_suggest_bad_root(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = shell_ops.remote_bash(endpoint, command="pwd", cwd="tmp")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["next"]["suggested_action"], "rerun_with_absolute_cwd")
        self.assertNotIn("endpoint_patch", payload["result"]["next"])

    def test_plain_and_configured_runtime_environment(self) -> None:
        commands = []
        def control(endpoint, job_id, action, **parameters):
            commands.append(parameters["spec"]["command"])
            return {"state": "succeeded", "quiet": True, "stdout": "ok\n", "result": {"exit_code": 0}}
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(__import__("os").environ, REMOTE_DEV_STATE_DIR=tmp), \
             unittest.mock.patch.object(job_ops, "control", control):
            plain = Endpoint(host="192.0.2.10", port=22)
            payload = shell_ops.remote_bash(plain, command="echo token=abc")
            self.assertEqual(commands[-1], "echo token=abc")
            self.assertEqual(Path(payload["result"]["refs"]["stdout"]).read_text(), "ok\n")
            configured = Endpoint(host="192.0.2.10", port=22, runtime_env_file="/etc/profile.d/tool chain.sh")
            payload = shell_ops.remote_bash(configured, command="echo ok")
            self.assertIn(". '/etc/profile.d/tool chain.sh'", commands[-1])
            self.assertEqual(payload["result"]["environment"]["runtime_env_file"], configured.runtime_env_file)
            shell_ops.remote_bash(configured, command="echo ok", runtime_env=False)
            self.assertEqual(commands[-1], "echo ok")

    def test_background_remote_bash_missing_cwd_does_not_start_job(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app")
        original_state_root = state_store.substrate_root
        calls = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_control(_endpoint, _job_id, action, **_params):
                    calls.append(action)
                    raise FileNotFoundError("command cwd does not exist")

                with unittest.mock.patch.object(job_ops, "control", fake_control):
                    payload = shell_ops.remote_bash(
                        endpoint,
                        command="touch /srv/app/should-not-exist",
                        cwd="/srv/app/missing",
                        yield_time_ms=0,
                    )
                self.assertEqual(payload["result"]["outcome"], "failed")
                self.assertEqual(payload["result"]["status"], "cwd_not_found")
                self.assertEqual(calls, ["launch"])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
