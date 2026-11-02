"""Trusted per-harness native profile contracts.

These tests do not invent native payloads: each case is derived from the
installed 0.153.4 / 2.1.269 / 2.3.1 / 0.2.0-rc.2 tool schemas captured under
/Volumes/data/agent-runs/remote-native-parity-20261025/baseline.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import remote_dev.mcp.tools as tools
from remote_dev.mcp.harness_profiles import (
    HarnessProfileError,
    PROFILE_ENV,
    resolve_profile,
)


class ProfileSelectionTests(unittest.TestCase):
    def test_explicit_profile_wins_and_unknown_is_rejected(self) -> None:
        self.assertEqual(resolve_profile("KIMI"), "kimi")
        self.assertEqual(resolve_profile("WB"), "wb")
        self.assertEqual(resolve_profile("workbuddy"), "wb")
        self.assertEqual(resolve_profile("canonical"), "canonical")
        with self.assertRaises(HarnessProfileError):
            resolve_profile("claude-code")

    def test_environment_is_server_side_only(self) -> None:
        with mock.patch.dict(os.environ, {PROFILE_ENV: "dsh"}):
            self.assertEqual(resolve_profile(), "dsh")
            with mock.patch.dict(os.environ, {PROFILE_ENV: "bogus"}):
                with self.assertRaises(HarnessProfileError):
                    resolve_profile()

    def test_no_implicit_guessing_without_profile(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PROFILE_ENV, None)
            self.assertEqual(resolve_profile(), "canonical")
            with self.assertRaises(ValueError):
                tools.call_tool("remote_Read", {"path": "/tmp/x"}, profile="canonical")


class CatalogTests(unittest.TestCase):
    def test_native_wire_names_are_exact(self) -> None:
        names = {tool["name"] for tool in tools.harness_catalog("kimi")}
        for name in (
            "remote_Read", "remote_Write", "remote_Edit", "remote_Bash",
            "remote_Glob", "remote_Grep", "remote_TaskOutput", "remote_TaskStop",
            "remote_TaskList", "remote_ReadMediaFile",
        ):
            self.assertIn(name, names)
        names = {tool["name"] for tool in tools.harness_catalog("claude")}
        for name in (
            "remote_Read", "remote_Write", "remote_Edit", "remote_Bash",
            "remote_Glob", "remote_Grep", "remote_TaskOutput", "remote_BashOutput",
            "remote_TaskStop", "remote_KillShell",
        ):
            self.assertIn(name, names)
        names = {tool["name"] for tool in tools.harness_catalog("dsh")}
        for name in ("remote_read", "remote_write", "remote_edit", "remote_bash",
                     "remote_glob", "remote_grep", "remote_job_output",
                     "remote_job_list", "remote_job_kill", "remote_read_image"):
            self.assertIn(name, names)
        names = {tool["name"] for tool in tools.harness_catalog("codex")}
        for name in ("remote_exec_command", "remote_write_stdin", "remote_view_image",
                     "remote_apply_patch"):
            self.assertIn(name, names)
        names = {tool["name"] for tool in tools.harness_catalog("wb")}
        for name in (
            "remote_Read", "remote_Write", "remote_Edit", "remote_MultiEdit",
            "remote_Bash", "remote_Glob", "remote_Grep", "remote_TaskOutput",
            "remote_BashOutput", "remote_NotebookEdit", "remote_TaskStop",
            "remote_KillShell", "remote_ViewImage",
        ):
            self.assertIn(name, names)

    def test_dsh_native_schema_matches_local_fields(self) -> None:
        by_name = {tool["name"]: tool for tool in tools.harness_catalog("dsh")}
        bash = by_name["remote_bash"]["inputSchema"]
        self.assertEqual(bash["required"], ["command", "description"])
        self.assertIn("timeoutMs", bash["properties"])
        self.assertIn("run_in_background", bash["properties"])
        job = by_name["remote_job_output"]["inputSchema"]
        self.assertEqual(job["required"], ["job_id"])
        self.assertIn("wait", job["properties"])
        self.assertIn("timeout_ms", job["properties"])
        # Canonical-compatibility fields are accepted server-side but never
        # advertised on the native wire schema.
        self.assertNotIn("lines", job["properties"])
        read = by_name["remote_read"]["inputSchema"]
        self.assertNotIn("default", read["properties"]["limit"])
        self.assertNotIn("maximum", read["properties"]["limit"])

    def test_kimi_seconds_and_claude_milliseconds_are_not_guessed(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake(name, args, *, profile="canonical"):
            calls.append((name, dict(args)))
            return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

        with mock.patch.object(tools, "_call_canonical", fake):
            tools.call_tool(
                "remote_Bash",
                {"command": "sleep 1", "timeout": 60, "description": "wait",
                 "run_in_background": True},
                profile="kimi",
            )
            self.assertEqual(calls[-1][1]["timeout_ms"], 60000)
            self.assertEqual(calls[-1][1]["yield_time_ms"], 0)
            tools.call_tool("remote_Bash", {"command": "sleep 1", "timeout": 60000}, profile="claude")
            self.assertEqual(calls[-1][1]["yield_time_ms"], 60000)
            self.assertNotIn("timeout_ms", calls[-1][1])
            tools.call_tool("remote_bash", {"command": "x", "description": "y", "timeoutMs": 1500}, profile="dsh")
            self.assertEqual(calls[-1][1]["yield_time_ms"], 1500)

    def test_native_defaults_align_with_local_tools(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake(name, args, *, profile="canonical"):
            calls.append((name, dict(args)))
            return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

        with mock.patch.object(tools, "_call_canonical", fake):
            tools.call_tool("remote_Read", {"path": "/tmp/x"}, profile="kimi")
            self.assertEqual(calls[-1][1]["limit"], 1000000)
            self.assertEqual(calls[-1][1]["max_chars"], 100000)
            tools.call_tool("remote_Write", {"path": "/tmp/x", "content": "x"}, profile="kimi")
            self.assertTrue(calls[-1][1]["overwrite"])
            self.assertTrue(calls[-1][1]["create_dirs"])
            tools.call_tool("remote_read", {"file_path": "/tmp/x"}, profile="dsh")
            self.assertEqual(calls[-1][1]["limit"], 2000)
            tools.call_tool("remote_write", {"file_path": "/tmp/x", "content": "x"}, profile="dsh")
            self.assertTrue(calls[-1][1]["overwrite"])
            tools.call_tool("remote_exec_command", {"cmd": "echo hi"}, profile="codex")
            self.assertEqual(calls[-1][1]["yield_time_ms"], 10000)
            self.assertFalse(calls[-1][1]["tty"])

    def test_native_unknown_and_wrong_types_are_rejected(self) -> None:
        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_Read", {"path": "/tmp/x", "bogus": 1}, profile="kimi")
        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_Bash", {"command": "x", "timeout": "60"}, profile="kimi")
        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_Glob", {"pattern": "*", "head_limit": -1}, profile="kimi")
        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_Read", {"file_path": "/tmp/x"}, profile="kimi")

    def test_high_order_permissions_are_not_swallowed(self) -> None:
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_bash",
                {"command": "x", "description": "y", "sandbox_permissions": "danger-full-access",
                 "justification": "z"},
                profile="dsh",
            )
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_exec_command",
                {"cmd": "x", "sandbox_permissions": "require_escalated", "justification": "z"},
                profile="codex",
            )
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_Bash", {"command": "x", "dangerouslyDisableSandbox": True}, profile="claude"
            )

    def test_native_names_reject_canonical_only_fields(self) -> None:
        cases = [
            ("remote_job_output", {"job_id": "j", "lines": 20}, "dsh"),
            ("remote_job_output", {"job_id": "j", "yield_time_ms": 20}, "dsh"),
            ("remote_read", {"path": "/tmp/x", "start_line": 2}, "dsh"),
            ("remote_exec_command", {"cmd": "x", "timeout_ms": 5}, "codex"),
            ("remote_write_stdin", {"session_id": 1, "eof": True}, "codex"),
            ("remote_Bash", {"command": "x", "cmd": "x"}, "claude"),
            ("remote_Grep", {"pattern": "x", "cmd": "x"}, "claude"),
            ("remote_TaskOutput", {"task_id": "j", "block": False}, "kimi"),
            ("remote_Read", {"path": "/tmp/x", "file_path": "/tmp/x"}, "kimi"),
        ]
        for name, args, profile in cases:
            with self.subTest(name=name, profile=profile):
                with self.assertRaises((HarnessProfileError, ValueError)):
                    tools.call_tool(name, args, profile=profile)

    def test_native_name_from_another_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tools.call_tool("remote_TaskList", {"active_only": True}, profile="claude")

    def test_wb_native_tools_and_handlers(self) -> None:
        from pathlib import Path

        calls: list[tuple[str, dict]] = []

        def fake(name, args, *, profile="canonical"):
            calls.append((name, dict(args)))
            return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

        with mock.patch.object(tools, "_call_canonical", fake):
            tools.call_tool(
                "remote_MultiEdit",
                {
                    "file_path": "/tmp/wb.py",
                    "edits": [{"old_string": "a", "new_string": "b", "replace_all": True}],
                },
                profile="wb",
            )
            self.assertEqual(calls[-1][0], "remote.multi_edit")
            self.assertEqual(calls[-1][1]["file_path"], "/tmp/wb.py")
            self.assertEqual(
                calls[-1][1]["edits"],
                [{"old_string": "a", "new_string": "b", "replace_all": True}],
            )

            tools.call_tool(
                "remote_Glob",
                {"pattern": "*.py", "limit": 25, "offset": 10},
                profile="wb",
            )
            self.assertEqual(calls[-1][0], "remote.glob")
            self.assertEqual(calls[-1][1]["limit"], 25)
            self.assertEqual(calls[-1][1]["offset"], 10)

        # Invalid regex in TaskOutput/BashOutput filter is rejected before dispatch
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_TaskOutput",
                {"task_id": "job-1", "filter": "[unclosed"},
                profile="wb",
            )
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_MultiEdit",
                {"file_path": "/tmp/wb.py", "edits": []},
                profile="wb",
            )

        # BashOutput / TaskOutput incremental read vs full=true snapshot + regex filter
        record = {"job_id": "j-wb", "session_scope": "scope", "state": "succeeded"}
        import remote_dev.mcp.native_handlers_claude as claude_handlers
        stdout_state = {"text": "ERROR: bad\nINFO: ok\nERROR: second\n"}

        def fake_status(name, args, *, profile="canonical"):
            return {"text": "", "result": {"status": "succeeded", "job": {"state": "succeeded", "remote_status": {"exit_code": 0}}}}

        with (
            mock.patch(
                "remote_dev.core.state_store.find_job_record",
                return_value=(Path("/tmp/fake-wb-job.json"), record),
            ),
            mock.patch.object(claude_handlers, "_assert_visible", return_value=None),
            mock.patch.object(claude_handlers, "_job_file", side_effect=lambda _p, stream: Path(f"/tmp/fake-wb-{stream}.log")),
            mock.patch.object(
                claude_handlers,
                "_tail_text",
                side_effect=lambda p, _lim=32000: (stdout_state["text"], False) if "stdout" in str(p) else ("", False),
            ),
            mock.patch.object(tools, "_call_canonical", fake_status),
        ):
            payload = tools.call_tool(
                "remote_BashOutput",
                {"shell_id": "j-wb", "filter": "^ERROR:"},
                profile="wb",
            )
            self.assertEqual(payload["result"]["task"]["output"], "ERROR: bad\nERROR: second")
            self.assertIn("ERROR: bad\nERROR: second", payload["text"])
            self.assertNotIn("INFO: ok", payload["text"])

            # Second incremental read with no new lines returns empty output
            payload2 = tools.call_tool(
                "remote_TaskOutput",
                {"task_id": "j-wb"},
                profile="wb",
            )
            self.assertEqual(payload2["result"]["task"]["output"], "")
            self.assertNotIn("<output>", payload2["text"])

            # Appended output is returned incrementally on next read
            stdout_state["text"] += "ERROR: third\n"
            payload3 = tools.call_tool(
                "remote_TaskOutput",
                {"task_id": "j-wb", "filter": "error"},
                profile="wb",
            )
            self.assertEqual(payload3["result"]["task"]["output"], "ERROR: third")

            # full=True returns the full accumulated output snapshot
            payload4 = tools.call_tool(
                "remote_TaskOutput",
                {"task_id": "j-wb", "full": True},
                profile="wb",
            )
            self.assertEqual(
                payload4["result"]["task"]["output"],
                "ERROR: bad\nINFO: ok\nERROR: second\nERROR: third",
            )

        # Persisted on-disk job record + >39 KB log incremental read + recording_failure honesty
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            rec_path = Path(tmpdir) / "j-wb-disk.json"
            stdout_log = Path(tmpdir) / "j-wb-disk.stdout.log"
            stderr_log = Path(tmpdir) / "j-wb-disk.stderr.log"
            initial_rec = {
                "job_id": "j-wb-disk",
                "session_scope": "scope",
                "state": "running",
                "stdin_cursors": {"stdout_offset": 0},
                "local_output_offsets": {"stdout": 0},
            }
            rec_path.write_text(json.dumps(initial_rec), encoding="utf-8")
            large_body = "".join(f"line {i:05d}: abcdefghijklmnopqrstuvwxyz\n" for i in range(1000))
            stdout_log.write_bytes(large_body.encode("utf-8"))
            stderr_log.write_bytes(b"")

            def fake_status_and_refresh(name, args, *, profile="canonical"):
                if name == "remote.job_stdin":
                    # Simulate _refresh_job_output updating the on-disk record
                    on_disk = json.loads(rec_path.read_text(encoding="utf-8"))
                    on_disk["state"] = "succeeded"
                    on_disk["stdin_cursors"] = {"stdout_offset": stdout_log.stat().st_size}
                    on_disk["local_output_offsets"] = {"stdout": stdout_log.stat().st_size}
                    rec_path.write_text(json.dumps(on_disk), encoding="utf-8")
                    return {"text": "", "result": {"outcome": "success", "status": "ok", "state": "succeeded"}}
                return {
                    "text": "",
                    "result": {
                        "outcome": "success",
                        "status": "succeeded",
                        "job": {"state": "succeeded", "remote_status": {"exit_code": 0}},
                    },
                }

            with (
                mock.patch("remote_dev.core.state_store.find_job_record", return_value=(rec_path, dict(initial_rec))),
                mock.patch.object(claude_handlers, "_assert_visible", return_value=None),
                mock.patch.object(tools, "_call_canonical", fake_status_and_refresh),
            ):
                first_disk = tools.call_tool("remote_TaskOutput", {"task_id": "j-wb-disk"}, profile="wb")
                self.assertEqual(first_disk["result"]["outcome"], "success")
                saved_after_first = json.loads(rec_path.read_text(encoding="utf-8"))
                self.assertEqual(saved_after_first["state"], "succeeded")
                self.assertEqual(saved_after_first["stdin_cursors"]["stdout_offset"], len(large_body.encode("utf-8")))
                self.assertEqual(saved_after_first["wb_output_offsets"]["stdout_offset"], len(large_body.encode("utf-8")))

                # Append new output beyond the 8KB preview window and verify incremental slice captures it
                stdout_log.write_bytes((large_body + "NEW TAIL LINE\n").encode("utf-8"))
                second_disk = tools.call_tool("remote_TaskOutput", {"task_id": "j-wb-disk"}, profile="wb")
                self.assertEqual(second_disk["result"]["task"]["output"], "NEW TAIL LINE")

                # Simulate cursor persistence failure -> surfaces local_recording_failed
                with mock.patch("remote_dev.core.state_store.atomic_write_json", side_effect=OSError("disk full")):
                    failed_commit = tools.call_tool("remote_TaskOutput", {"task_id": "j-wb-disk"}, profile="wb")
                self.assertEqual(failed_commit["result"]["status"], "local_recording_failed")
                self.assertTrue(failed_commit["result"]["operation_completed"])

        # ViewImage dispatches through _read_media_payload and attaches query metadata
        fake_media = {
            "text": "remote_ViewImage /tmp/a.png (image/png, 64x64).\n",
            "result": {"outcome": "success", "status": "ok", "summary": "remote_ViewImage /tmp/a.png (image/png, 64x64)."},
            "content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}],
        }
        with mock.patch("remote_dev.mcp.native_handlers_wb._read_media_payload", return_value=fake_media):
            img_payload = tools.call_tool(
                "remote_ViewImage",
                {"image_path": "/tmp/a.png", "query": "check NPU utilization chart"},
                profile="wb",
            )
        self.assertEqual(img_payload["result"]["query"], "check NPU utilization chart")
        self.assertIn("check NPU utilization chart", img_payload["text"])


if __name__ == "__main__":
    unittest.main()
