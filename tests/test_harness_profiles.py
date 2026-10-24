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
    def test_canonical_table_stays_19(self) -> None:
        self.assertEqual(len(tools.harness_catalog("canonical")), 19)
        self.assertEqual(len(tools.harness_catalog("kimi")), 29)
        self.assertEqual(len(tools.harness_catalog("claude")), 30)
        self.assertEqual(len(tools.harness_catalog("dsh")), 22)
        self.assertEqual(len(tools.harness_catalog("codex")), 22)

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
        self.assertEqual(read["properties"]["limit"]["default"], 2000)
        self.assertEqual(read["properties"]["limit"]["maximum"], 2000)

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
        with self.assertRaises((HarnessProfileError, ValueError)):
            tools.call_tool("remote_job_output", {"job_id": "j", "lines": 20}, profile="dsh")
        with self.assertRaises((HarnessProfileError, ValueError)):
            tools.call_tool("remote_read", {"path": "/tmp/x", "start_line": 2}, profile="dsh")
        with self.assertRaises((HarnessProfileError, ValueError)):
            tools.call_tool("remote_exec_command", {"cmd": "x", "timeout_ms": 5}, profile="codex")

    def test_native_name_from_another_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tools.call_tool("remote_TaskList", {"active_only": True}, profile="claude")


if __name__ == "__main__":
    unittest.main()
