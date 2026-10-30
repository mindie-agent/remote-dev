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

    def test_dangerous_commands_blocked_until_confirmed(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=22)
        calls: list[str] = []

        def fake_control(_endpoint, _job_id, action, **parameters):
            calls.append(parameters["spec"]["command"])
            return {"state": "succeeded", "quiet": True, "stdout": "done\n", "result": {"exit_code": 0}}

        blocked_cases = [
            ("npu-smi set -t reset -i 0 -c 0", "npu_reset"),
            ("echo y | sudo -E /usr/local/Ascend/driver/tools/npu-smi set -i 2 -c 0 -t reset", "npu_reset"),
            ("reboot", "reboot"),
            ("sudo shutdown -r now", "reboot"),
            ("systemctl reboot", "reboot"),
            ("init 6", "reboot"),
            ("bash -lc 'sudo reboot'", "reboot"),
            ("shutdown -h now", "shutdown"),
            ("sudo poweroff", "shutdown"),
            ("halt -p", "shutdown"),
            ("systemctl poweroff", "shutdown"),
            ("init 0", "shutdown"),
        ]
        safe_cases = [
            "npu-smi info",
            "npu-smi info -t usages -i 0 -c 0",
            "docker restart mindie-container",
            "docker stop mindie-container",
            "podman restart test",
            "systemctl restart docker",
            "systemctl stop nginx",
            "shutdown -c",
            "echo 'do not reboot or shutdown'",
        ]

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(__import__("os").environ, REMOTE_DEV_STATE_DIR=tmp), \
             unittest.mock.patch.object(job_ops, "control", fake_control):
            for cmd, expected_category in blocked_cases:
                with self.subTest(cmd=cmd):
                    payload = shell_ops.remote_bash(endpoint, command=cmd)
                    self.assertEqual(payload["result"]["outcome"], "blocked")
                    self.assertEqual(payload["result"]["status"], "confirmation_required")
                    self.assertEqual(payload["result"]["category"], expected_category)
                    self.assertTrue(payload["result"]["requires_human_confirmation"])
                    self.assertEqual(payload["result"]["error_details"]["submission_state"], "not_sent")
            self.assertEqual(calls, [])

            for cmd in safe_cases:
                with self.subTest(safe_cmd=cmd):
                    payload = shell_ops.remote_bash(endpoint, command=cmd)
                    self.assertEqual(payload["result"]["outcome"], "success")

            # Confirmed execution via confirm=True, env, or inline prefix succeeds
            payload_flag = shell_ops.remote_bash(
                endpoint, command="npu-smi set -t reset -i 0 -c 0", confirm=True
            )
            self.assertEqual(payload_flag["result"]["outcome"], "success")

            payload_inline = shell_ops.remote_bash(
                endpoint, command="REMOTE_DEV_CONFIRM_DANGEROUS=1 sudo reboot"
            )
            self.assertEqual(payload_inline["result"]["outcome"], "success")

            # job_stdin also intercepts dangerous input unless confirmed
            stdin_blocked = job_ops.remote_job_stdin(
                endpoint, job_id="job-20261030T000000Z-12345678", chars="npu-smi set -t reset -i 0 -c 0\n"
            )
            self.assertEqual(stdin_blocked["result"]["outcome"], "blocked")
            self.assertEqual(stdin_blocked["result"]["status"], "confirmation_required")
            self.assertEqual(stdin_blocked["result"]["category"], "npu_reset")


if __name__ == "__main__":
    unittest.main()
