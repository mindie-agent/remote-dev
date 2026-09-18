from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from remote_dev.cli import TOOL_NAMES
from remote_dev.mcp.schemas import TOOL_SCHEMAS


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "remote_dev", *args], capture_output=True, text=True, check=False)


class CliHelpTests(unittest.TestCase):
    def test_parser_feedback_does_not_import_transport_or_start_processes(self) -> None:
        guard = '''
import importlib.abc, sys
class NoBackend(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("remote_dev.core.") and (fullname.endswith("_ops") or fullname.endswith("ssh_transport")):
            raise AssertionError("parser loaded backend: " + fullname)
sys.meta_path.insert(0, NoBackend())
def audit(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system"}:
        raise AssertionError("parser attempted side effect: " + event)
sys.addaudithook(audit)
'''
        with tempfile.TemporaryDirectory() as tmp:
            # macOS temporary directories can start at the /var system
            # symlink. Diagnostics deliberately require a canonical root.
            root = Path(tmp).resolve()
            (root / "sitecustomize.py").write_text(guard, encoding="utf-8")
            home = root / "home"
            home.mkdir()
            env = os.environ.copy()
            source = str(Path(__file__).resolve().parents[1])
            diagnostics = root / "diagnostics"
            env.update(PYTHONPATH=os.pathsep.join((str(root), source)), HOME=str(home), USERPROFILE=str(home),
                       LOCALAPPDATA=str(home / "AppData" / "Local"), XDG_STATE_HOME=str(home / ".local" / "state"),
                       MINDIE_DIAGNOSTICS_ROOT=str(diagnostics))
            for key in list(env):
                if key.startswith("REMOTE_DEV_"):
                    env.pop(key)
            cases = [(["--help"], 0), (["--unknown-option"], 2)]
            cases.extend(([name.replace("_", "-"), "--help"], 0) for name in TOOL_NAMES)
            cases.append((["read"], 1))
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    result = subprocess.run(
                        [sys.executable, "-m", "remote_dev", *argv], env=env,
                        capture_output=True, encoding="utf-8", timeout=15,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertNotIn("AssertionError", result.stdout + result.stderr)
                    if argv == ["read"]:
                        payload = json.loads(result.stdout)["result"]
                        self.assertEqual(payload["status"], "endpoint_required")
                        self.assertEqual(payload["error_details"]["category"], "caller")
                        self.assertEqual(payload["error_details"]["submission_state"], "not_sent")
                        self.assertFalse(payload["diagnostics"]["logging_failed"])
                        events = [json.loads(line) for path in diagnostics.glob("events/*/*.jsonl")
                                  for line in path.read_text(encoding="utf-8").splitlines()]
                        ended = [event for event in events if event["event"] == "operation.end"]
                        self.assertEqual(len(ended), 1)
                        self.assertEqual(ended[0]["attributes"]["category"], "caller")
                    else:
                        self.assertFalse(diagnostics.exists(), "parser-only feedback must not start logging")
            self.assertEqual(list(home.iterdir()), [])

    def test_cli_wrappers_have_help(self) -> None:
        expected = {name.removeprefix("remote.") for name in TOOL_SCHEMAS}
        self.assertEqual(set(TOOL_NAMES), expected)
        proc = _cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)
        self.assertIn("server", proc.stdout)
        self.assertIn("status", proc.stdout)
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                help_proc = _cli(name.replace("_", "-"), "--help")
                self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
                self.assertIn("usage:", help_proc.stdout)

    def test_cli_consumes_local_input_files_before_tool_argument_validation(self) -> None:
        from unittest import mock
        from remote_dev import cli
        from remote_dev.mcp import tools
        with tempfile.TemporaryDirectory() as directory:
            content = Path(directory) / "content.txt"
            content.write_text("file body", encoding="utf-8")
            arguments = Path(directory) / "arguments.json"
            arguments.write_text(json.dumps({"file_path": "/tmp/example", "overwrite": True}), encoding="utf-8")
            args = cli.build_parser("write").parse_args(["--host", "example.invalid", "--port", "22",
                "--content-file", str(content), "--input-json", str(arguments)])
            with mock.patch.object(tools, "remote_write", return_value={"ok": True}) as write:
                self.assertTrue(cli.run_tool("write", args)["ok"])
                self.assertEqual(write.call_args.kwargs["content"], "file body")
                self.assertEqual(write.call_args.kwargs["file_path"], "/tmp/example")
                self.assertTrue(write.call_args.kwargs["overwrite"])

    def test_cli_keeps_transport_policy_out_of_developer_tools(self) -> None:
        from remote_dev.cli import build_parser, endpoint_payload

        parser = build_parser("probe")
        default_args = parser.parse_args(["--host", "192.0.2.10", "--port", "22"])
        default_payload = endpoint_payload(default_args)
        self.assertNotIn("ssh_mux", default_payload)
        self.assertNotIn("keepalive", default_payload)
        for flag in ("--ssh-mux", "--no-ssh-mux", "--keepalive", "--long-stream"):
            with self.subTest(flag=flag):
                result = _cli("probe", "--host", "192.0.2.10", "--port", "22", flag)
                self.assertEqual(result.returncode, 2)
                self.assertIn("unrecognized arguments", result.stderr)

    def test_cli_endpoint_flags_are_explicit_only(self) -> None:
        proc = _cli("bash", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in (
            "--host",
            "--port",
            "--user",
            "--root",
            "--cwd",
            "--alias",
            "--selector",
            "--runtime-env-file",
        ):
            self.assertIn(flag, proc.stdout)
        for legacy in ("--session-id", "--session-file", "--machine", "--ssh-mux", "--no-ssh-mux", "--keepalive", "--long-stream"):
            self.assertNotIn(legacy, proc.stdout)

    def test_cli_selector_without_resolver_is_endpoint_required(self) -> None:
        proc = _cli("probe", "--selector", "lab=gpu-1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["tool"], "remote.probe")
        self.assertEqual(payload["result"]["status"], "endpoint_required")
        self.assertIn("registered resolvers", payload["result"]["error"])

    def test_cli_bad_selector_item_is_invalid_input(self) -> None:
        proc = _cli("probe", "--selector", "novalue")
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["status"], "invalid_input")

    def test_package_import_does_not_mutate_sys_path(self) -> None:
        code = (
            "import sys\n"
            "before = list(sys.path)\n"
            "import remote_dev.mcp.tools, remote_dev.mcp.server, remote_dev.core.endpoint\n"
            "print([p for p in sys.path if p not in before])\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_cli_errors_return_result_contract_without_traceback(self) -> None:
        proc = _cli("job-status", "--job-id", "job-does-not-exist")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "remote.job_status")
        self.assertEqual(payload["result"]["outcome"], "needs_input")


if __name__ == "__main__":
    unittest.main()
