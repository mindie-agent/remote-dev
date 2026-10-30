from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from remote_dev.hooks.guard_common import inspect_command, inspect_payload  # noqa: E402


class HookGuardTests(unittest.TestCase):
    def test_guard_helpers_allow_commands_and_payloads(self) -> None:
        for cmd in (
            "ssh root@1.2.3.4 -p 46000 hostname",
            "sshpass -p secret ssh host",
            "python3 -m compileall .remote-dev",
        ):
            self.assertFalse(inspect_command(cmd).blocked)
        for payload in (
            {"tool_name": "apply_patch", "command": "*** Update File: /vllm-workspace/foo.py"},
            {"tool_name": "mcp__remote-dev__remote_read", "tool_input": {"host": "1.2.3.4", "port": 46000, "root": "/vllm-workspace", "file_path": "/etc/passwd"}},
            {"tool_name": "remote.bash", "arguments": {"host": "1.2.3.4", "port": 46000, "root": "/vllm-workspace", "cwd": "/tmp", "command": "pwd"}},
        ):
            self.assertFalse(inspect_payload(payload).blocked)

    def test_claude_and_codex_hook_entrypoints(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "ssh root@1.2.3.4 hostname"}}
        proc = subprocess.run(
            [sys.executable, "-m", "remote_dev.hooks.claude_remote_guard"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

        codex_proc = subprocess.run(
            [sys.executable, "-m", "remote_dev.hooks.codex_remote_guard"],
            input=json.dumps({"tool_name": "remote.bash", "arguments": {"command": "curl --password secret"}}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(codex_proc.returncode, 0)
        self.assertEqual(json.loads(codex_proc.stdout)["decision"], "allow")

    def test_guard_blocks_unconfirmed_npu_reset_reboot_and_shutdown(self) -> None:
        for cmd in (
            "npu-smi set -t reset -i 0 -c 0",
            "ssh root@1.2.3.4 'npu-smi set -t reset -i 0 -c 0'",
            "sudo reboot",
            "shutdown -h now",
            "poweroff",
        ):
            decision = inspect_command(cmd)
            self.assertTrue(decision.blocked, cmd)
            self.assertIn("confirmation", decision.reason or "")

        self.assertFalse(inspect_command("REMOTE_DEV_CONFIRM_DANGEROUS=1 npu-smi set -t reset -i 0 -c 0").blocked)
        self.assertFalse(inspect_command("docker restart my-container").blocked)
        self.assertFalse(inspect_command("npu-smi info").blocked)


if __name__ == "__main__":
    unittest.main()

