"""Isolated contracts for the strict 1:1 seven-harness tool surface.

[Role]
Pin `tools/list` and `tools/call` to the same per-harness boundary without SSH.
[Invariants]
Every bound profile advertises only its native tools plus the declared
remote-only support tools; no cross-harness alias leaks in either direction.
[Timeouts & Locks]
Pure in-memory catalog and dispatch tests; no I/O or locks.
[Side-Effect Policy]
Side-effect free.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.mcp.tools as tools
from remote_dev.core import file_ops
from remote_dev.mcp.harness_profiles import (
    NATIVE_PROFILES,
    PROFILE_SUPPORT_TOOLS,
    specs_for,
    support_tools_for,
)
from remote_dev.mcp.native_spec import PROFILES, resolve_profile
from remote_dev.mcp.outcome import operation_outcome
from remote_dev.mcp.schemas import ALIASES, TOOL_SCHEMAS, harness_catalog

sys.path.insert(0, str(Path(__file__).parent))
from _worker_runner import run_worker_script


def run_file_op(payload: dict) -> dict:
    process = run_worker_script(file_ops.REMOTE_FILE_PY, payload)
    if process.returncode != 0:
        raise AssertionError(f"file worker failed: {process.stderr}\n{process.stdout}")
    return json.loads(process.stdout.strip().splitlines()[-1])

EXPECTED_NATIVE = {
    "codex": {
        "remote_exec_command", "remote_write_stdin", "remote_view_image", "remote_apply_patch",
    },
    "claude": {
        "remote_Read", "remote_Write", "remote_Edit", "remote_Bash", "remote_Glob",
        "remote_Grep", "remote_TaskOutput", "remote_NotebookEdit", "remote_TaskStop",
    },
    "kimi": {
        "remote_Read", "remote_Write", "remote_Edit", "remote_Bash", "remote_Glob",
        "remote_Grep", "remote_ReadMediaFile", "remote_TaskOutput", "remote_TaskStop",
        "remote_TaskList",
    },
    "dsh": {
        "remote_read", "remote_write", "remote_edit", "remote_bash", "remote_glob",
        "remote_grep", "remote_job_output", "remote_job_list", "remote_job_kill",
        "remote_read_image",
    },
    "wb": {
        "remote_Read", "remote_Write", "remote_Edit", "remote_MultiEdit", "remote_Bash",
        "remote_Glob", "remote_Grep", "remote_TaskOutput", "remote_NotebookEdit",
        "remote_TaskStop", "remote_ViewImage",
    },
    "agy": {
        "remote_read_file", "remote_write_file", "remote_edit_file", "remote_run_command",
        "remote_manage_task", "remote_find_by_name", "remote_grep_search", "remote_list_dir",
    },
    "oc": {
        "remote_read", "remote_write", "remote_edit", "remote_bash", "remote_glob",
        "remote_grep", "remote_list",
    },
}
EXPECTED_SUPPORT = {
    profile: {name.replace(".", "_") for name in names}
    for profile, names in PROFILE_SUPPORT_TOOLS.items()
}
EXPECTED_TOTAL = {
    "codex": 10, "claude": 13, "kimi": 14, "dsh": 14, "wb": 15, "agy": 12, "oc": 14,
}


class SurfaceCatalogTests(unittest.TestCase):
    def test_all_official_profiles_and_aliases(self) -> None:
        self.assertEqual(NATIVE_PROFILES, ("codex", "claude", "kimi", "dsh", "wb", "agy", "oc"))
        self.assertEqual(PROFILES[0], "canonical")
        for alias, resolved in (
            ("workbuddy", "wb"), ("antigravity", "agy"), ("opencode", "oc"), ("cc", "claude"),
        ):
            self.assertEqual(resolve_profile(alias), resolved)

    def test_bound_catalogs_are_native_plus_support_only(self) -> None:
        for profile in NATIVE_PROFILES:
            with self.subTest(profile=profile):
                names = [tool["name"] for tool in harness_catalog(profile)]
                self.assertEqual(len(names), len(set(names)), names)
                self.assertEqual(set(names), EXPECTED_NATIVE[profile] | EXPECTED_SUPPORT[profile])
                self.assertEqual(len(names), EXPECTED_TOTAL[profile])
                self.assertNotIn("remote_job_tail", names)
                if profile not in {"claude", "wb"}:
                    self.assertNotIn("remote_BashOutput", names)
                    self.assertNotIn("remote_KillShell", names)

    def test_native_specs_and_catalog_are_aligned(self) -> None:
        for profile in NATIVE_PROFILES:
            with self.subTest(profile=profile):
                wires = {spec.wire for spec in specs_for(profile).values()}
                self.assertEqual(wires, EXPECTED_NATIVE[profile])
                self.assertTrue(all("." not in wire for wire in wires))

    def test_canonical_catalog_is_only_the_twenty_portable_aliases(self) -> None:
        names = [tool["name"] for tool in harness_catalog("canonical")]
        self.assertEqual(names, list(ALIASES))
        self.assertEqual(len(names), 20)
        self.assertNotIn("remote_Read", names)
        self.assertNotIn("remote_exec_command", names)
        self.assertNotIn("remote_manage_task", names)

    def test_retired_job_aliases_are_gone_from_claude_and_wb(self) -> None:
        for profile in ("claude", "wb"):
            with self.subTest(profile=profile):
                names = {tool["name"] for tool in harness_catalog(profile)}
                self.assertNotIn("remote_BashOutput", names)
                self.assertNotIn("remote_KillShell", names)
                self.assertIn("remote_TaskOutput", names)
                self.assertIn("remote_TaskStop", names)


class StrictCallBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def fake(self, name, args, *, profile="canonical"):
        self.calls.append((name, dict(args or {})))
        return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

    def test_bound_profile_rejects_unadvertised_and_other_harness_tools(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            for profile, name in (
                ("agy", "remote_Read"),
                ("agy", "remote_read"),
                ("oc", "remote_Read"),
                ("oc", "remote.Bash"),
                ("codex", "remote_bash"),
                ("claude", "remote_exec_command"),
                ("claude", "remote_BashOutput"),
                ("wb", "remote_KillShell"),
                ("dsh", "remote_TaskOutput"),
            ):
                with self.subTest(profile=profile, name=name):
                    with self.assertRaisesRegex(
                        ValueError,
                        f"{name} is not available under harness profile {profile!r}; "
                        "use this host's advertised remote_\\* tools",
                    ):
                        tools.call_tool(name, {"command": "true"}, profile=profile)
        self.assertEqual(self.calls, [])

    def test_support_tools_accept_wire_and_dotted_forms(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_auth", {"host": "example.invalid", "password": "x"}, profile="agy")
            tools.call_tool("remote.auth", {"action": "status", "host": "example.invalid"}, profile="agy")
            tools.call_tool("remote.job_output", {"job_id": "j"}, profile="oc")
            tools.call_tool("remote_job_status", {"job_id": "j"}, profile="codex")
        self.assertEqual([name for name, _ in self.calls],
                         ["remote.auth", "remote.auth", "remote.job_output", "remote.job_status"])
        with self.assertRaises(ValueError):
            tools.call_tool("remote_job_output", {"job_id": "j"}, profile="codex")

    def test_canonical_accepts_only_portable_canonical_names(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_read", {"file_path": "/tmp/x"})
            tools.call_tool("remote.read", {"file_path": "/tmp/x"})
            for name in ("remote_Read", "remote_exec_command", "remote_manage_task"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        tools.call_tool(name, {})


class AgyNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def fake(self, name, args, *, profile="canonical"):
        self.calls.append((name, dict(args or {})))
        return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

    def test_schemas_mirror_antigravity_fields(self) -> None:
        catalog = {tool["name"]: tool for tool in harness_catalog("agy")}
        read = catalog["remote_read_file"]["inputSchema"]
        self.assertEqual(read["required"], ["Path"])
        for key in ("Path", "StartLine", "EndLine", "ByteOffset"):
            self.assertIn(key, read["properties"])
        run = catalog["remote_run_command"]["inputSchema"]
        self.assertEqual(run["required"], ["CommandLine"])
        self.assertIn("IsDaemon", run["properties"])
        self.assertIn("WaitMsBeforeAsync", run["properties"])
        task = catalog["remote_manage_task"]["inputSchema"]
        self.assertEqual(task["properties"]["Action"]["enum"], ["list", "status", "send_input", "kill"])

    def test_argument_normalization(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_read_file", {
                "Path": "/tmp/a.py", "StartLine": 2, "EndLine": 5, "ByteOffset": 3,
            }, profile="agy")
            self.assertEqual(self.calls[-1][0], "remote.read")
            self.assertEqual(self.calls[-1][1]["file_path"], "/tmp/a.py")
            self.assertEqual(self.calls[-1][1]["start_line"], 2)
            self.assertEqual(self.calls[-1][1]["end_line"], 5)
            self.assertEqual(self.calls[-1][1]["content_offset"], 3)

            tools.call_tool("remote_edit_file", {
                "Path": "/tmp/a.py", "Target": "old", "Replacement": "new",
            }, profile="agy")
            self.assertEqual(self.calls[-1][1]["old_string"], "old")
            self.assertEqual(self.calls[-1][1]["new_string"], "new")

            tools.call_tool("remote_run_command", {
                "CommandLine": "sleep 1", "IsDaemon": True, "WaitMsBeforeAsync": 750,
            }, profile="agy")
            self.assertEqual(self.calls[-1][1]["command"], "sleep 1")
            self.assertTrue(self.calls[-1][1]["run_in_background"])
            self.assertEqual(self.calls[-1][1]["yield_time_ms"], 750)

            tools.call_tool("remote_manage_task", {
                "Action": "send_input", "TaskId": "job-1", "Input": "yes\n",
            }, profile="agy")
            self.assertEqual(self.calls[-1][1]["action"], "send_input")
            self.assertEqual(self.calls[-1][1]["job_id"], "job-1")
            self.assertEqual(self.calls[-1][1]["chars"], "yes\n")

            tools.call_tool("remote_grep_search", {
                "Query": "needle", "Includes": ["*.py"], "IsRegex": False, "MatchPerLine": False,
            }, profile="agy")
            self.assertEqual(self.calls[-1][1]["pattern"], "needle")
            self.assertEqual(self.calls[-1][1]["includes"], ["*.py"])
            self.assertTrue(self.calls[-1][1]["fixed_strings"])
            self.assertEqual(self.calls[-1][1]["output_mode"], "files_with_matches")

    def test_schema_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError):
            tools.call_tool("remote_read_file", {"Path": "/tmp/a", "path": "/tmp/b"}, profile="agy")

    def test_append_true_defaults_overwrite_false(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_write_file", {
                "Path": "/tmp/a", "Content": "tail", "Append": True,
            }, profile="agy")
            self.assertFalse(self.calls[-1][1]["overwrite"])
            self.assertTrue(self.calls[-1][1]["append"])

            tools.call_tool("remote_write_file", {
                "Path": "/tmp/a", "Content": "tail", "Append": True, "Overwrite": True,
            }, profile="agy")
            self.assertTrue(self.calls[-1][1]["overwrite"])
            self.assertTrue(self.calls[-1][1]["append"])

    def test_start_line_byte_offset_slices_selected_range_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "multibyte.txt"
            sample.write_bytes("头\nαβ\n尾\n".encode("utf-8"))
            with mock.patch.object(tools, "_call_canonical", self.fake):
                tools.call_tool("remote_read_file", {
                    "Path": str(sample), "StartLine": 1, "EndLine": 2, "ByteOffset": 4,
                }, profile="agy")
            normalized = dict(self.calls[-1][1])
            self.assertEqual(normalized["content_offset"], 4)
            payload = {
                **normalized,
                "op": "read", "root": directory, "cwd": directory,
            }
            data = run_file_op(payload)
            self.assertEqual(data["status"], "partial")
            self.assertEqual(data["file"]["content"], "2 | αβ")
            self.assertEqual(data["file"]["line_start"], 2)

    def test_run_command_exit_code_one_is_a_completed_command_outcome(self) -> None:
        payload = {
            "result": {
                "state": "failed",
                "exit_code": 1,
                "outcome": "failed",
                "status": "failed",
            },
        }
        self.assertEqual(operation_outcome("remote_run_command", payload), "succeeded")
        self.assertEqual(operation_outcome("remote_bash", payload), "succeeded")


class CliCanonicalDispatchTests(unittest.TestCase):
    def test_standalone_cli_forces_canonical_under_bound_profile_env(self) -> None:
        from remote_dev.cli import run_tool_main

        calls: list[tuple[str, str]] = []

        def fake(name, args, *, profile="canonical"):
            calls.append((name, profile))
            return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

        with mock.patch.dict(os.environ, {"REMOTE_DEV_HARNESS_PROFILE": "codex"}), \
                mock.patch.object(tools, "_call_canonical", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            code = run_tool_main("ls", ["--host", "192.0.2.10", "--port", "22", "--path", "/tmp"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("remote.ls", "canonical")])


class OcNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def fake(self, name, args, *, profile="canonical"):
        self.calls.append((name, dict(args or {})))
        return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

    def test_schemas_mirror_opencode_fields(self) -> None:
        catalog = {tool["name"]: tool for tool in harness_catalog("oc")}
        self.assertEqual(catalog["remote_read"]["inputSchema"]["required"], [])
        self.assertEqual(catalog["remote_edit"]["inputSchema"]["required"], [])
        self.assertEqual(catalog["remote_write"]["inputSchema"]["required"], ["content"])
        self.assertIn("file_path", catalog["remote_read"]["inputSchema"]["properties"])
        self.assertEqual(catalog["remote_write"]["inputSchema"]["properties"]["overwrite"]["default"], True)
        self.assertIn("replaceAll", catalog["remote_edit"]["inputSchema"]["properties"])
        self.assertIn("run_in_background", catalog["remote_bash"]["inputSchema"]["properties"])
        self.assertIn("ignore", catalog["remote_list"]["inputSchema"]["properties"])

    def test_both_spellings_normalize_to_canonical(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_read", {"filePath": "/tmp/a", "offset": 2, "limit": 5}, profile="oc")
            self.assertEqual(self.calls[-1][1]["file_path"], "/tmp/a")
            self.assertEqual(self.calls[-1][1]["offset"], 2)
            self.assertEqual(self.calls[-1][1]["limit"], 5)
            tools.call_tool("remote_read", {"file_path": "/tmp/b"}, profile="oc")
            self.assertEqual(self.calls[-1][1]["file_path"], "/tmp/b")

            tools.call_tool("remote_write", {"filePath": "/tmp/a", "content": "x"}, profile="oc")
            self.assertTrue(self.calls[-1][1]["overwrite"])
            tools.call_tool("remote_write", {
                "file_path": "/tmp/a", "content": "x", "overwrite": False,
            }, profile="oc")
            self.assertFalse(self.calls[-1][1]["overwrite"])

            tools.call_tool("remote_edit", {
                "file_path": "/tmp/a", "old_string": "a", "new_string": "b", "replace_all": True,
            }, profile="oc")
            self.assertEqual(self.calls[-1][1]["old_string"], "a")
            self.assertEqual(self.calls[-1][1]["new_string"], "b")
            self.assertTrue(self.calls[-1][1]["replace_all"])

            tools.call_tool("remote_bash", {
                "command": "true", "timeout": 1000, "cwd": "/w", "description": "x",
            }, profile="oc")
            self.assertEqual(self.calls[-1][1]["timeout_ms"], 1000)
            self.assertEqual(self.calls[-1][1]["cwd"], "/w")

            tools.call_tool("remote_grep", {"pattern": "x", "include": "*.py"}, profile="oc")
            self.assertEqual(self.calls[-1][1]["glob"], "*.py")
            tools.call_tool("remote_list", {"path": "/w", "ignore": ["*.log"]}, profile="oc")
            self.assertEqual(self.calls[-1][1]["ignore_globs"], ["*.log"])

    def test_dotted_canonical_tools_are_not_native_wires(self) -> None:
        with self.assertRaises(ValueError):
            tools.call_tool("remote.read", {"filePath": "/tmp/a"}, profile="oc")

    def test_snake_case_schema_validation_and_normalizer_required_fields(self) -> None:
        with mock.patch.object(tools, "_call_canonical", self.fake):
            tools.call_tool("remote_read", {"file_path": "/tmp/a"}, profile="oc")
            self.assertEqual(self.calls[-1][1]["file_path"], "/tmp/a")
            tools.call_tool("remote_edit", {
                "file_path": "/tmp/a", "old_string": "old", "new_string": "",
            }, profile="oc")
            self.assertEqual(self.calls[-1][1]["new_string"], "")
        for name, arguments in (
            ("remote_read", {}),
            ("remote_edit", {"file_path": "/tmp/a", "old_string": "old"}),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                tools.call_tool(name, arguments, profile="oc")


if __name__ == "__main__":
    unittest.main()
