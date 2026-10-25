"""Six-harness native-tool parity: Claude Code, Codex, Kimi, Cursor,
Antigravity, and DeepSeek Harness alias dispatch plus read/grep/ls behavior."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_dev.mcp.schemas import normalize_arguments  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402
import remote_dev.core.search_ops as search_ops  # noqa: E402
import remote_dev.mcp.tools as mcp_tools  # noqa: E402
sys.path.insert(0, str(Path(__file__).parent))
from _worker_runner import run_worker_script


def run_file_op(payload: dict) -> dict:
    proc = run_worker_script(file_ops.REMOTE_FILE_PY, payload)
    if proc.returncode != 0:
        raise AssertionError(f"file op helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_grep(payload: dict, *, path_env: str | None = None) -> dict:
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    proc = run_worker_script(search_ops.REMOTE_SEARCH_PY, payload, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"grep helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])



class SixHarnessParityTests(unittest.TestCase):
    """End-to-end parity tests across Codex, Claude Code, Cursor, Kimi Code,
    DeepSeek Harness (DSH), and Antigravity native local tool conventions."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": str(self.tree / ".state")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_harness_tool_name_aliases_resolve_to_canonical_tools(self) -> None:
        from remote_dev.mcp.tools import canonical_name

        cases = {
            "remote_view_file": "remote.read",
            "remote.view_file": "remote.read",
            "remote_read_file": "remote.read",
            "remote_view_file_outline": "remote.read",
            "remote_view_code_item": "remote.read",
            "remote_write_to_file": "remote.write",
            "remote_write_file": "remote.write",
            "remote_replace_file_content": "remote.edit",
            "remote_str_replace_file": "remote.edit",
            "remote_multi_replace_file_content": "remote.multi_edit",
            "remote_exec_command": "remote.bash",
            "remote_run_command": "remote.bash",
            "remote_run_terminal_cmd": "remote.bash",
            "remote_shell": "remote.bash",
            "remote_write_stdin": "remote.job_stdin",
            "remote_send_command_input": "remote.job_stdin",
            "remote_manage_task": "remote.job_status",
            "remote_command_status": "remote.job_status",
            "remote_bash_output": "remote.job_status",
            "remote_task_output": "remote.job_status",
            "remote_job_output": "remote.job_output",
            "remote.JobOutput": "remote.job_output",
            "remote_kill_shell": "remote.job_stop",
            "remote_task_stop": "remote.job_stop",
            "remote_find_by_name": "remote.glob",
            "remote_file_search": "remote.glob",
            "remote_grep_search": "remote.grep",
            "remote_list_dir": "remote.ls",
        }
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(canonical_name(alias), expected)

    def test_antigravity_and_cursor_read_line_ranges_and_content_offset(self) -> None:
        sample = self.tree / "sample.py"
        sample.write_bytes("".join(f"line_{i}\n" for i in range(1, 21)).encode("utf-8"))

        # Antigravity StartLine + EndLine (1-indexed inclusive)
        args = normalize_arguments(
            "remote.read",
            {"AbsolutePath": str(sample), "StartLine": 5, "EndLine": 8},
        )
        self.assertEqual(args["file_path"], str(sample))
        self.assertEqual(args["start_line"], 5)
        self.assertEqual(args["end_line"], 8)

        data = run_file_op({
            "op": "read",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(sample),
            "start_line": 5,
            "end_line": 8,
        })
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["line_start"], 5)
        self.assertEqual(data["file"]["line_end"], 8)
        self.assertIn("5 | line_5", data["file"]["content"])
        self.assertIn("8 | line_8", data["file"]["content"])
        self.assertNotIn("9 | line_9", data["file"]["content"])

        # EndLine only -> reads lines 1..EndLine
        end_only = run_file_op({
            "op": "read",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(sample),
            "end_line": 3,
        })
        self.assertEqual(end_only["file"]["line_start"], 1)
        self.assertEqual(end_only["file"]["line_end"], 3)

        # Byte offsets refer to the remote file's bytes, including its newlines.
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                sample.write_bytes(b"".join(f"line_{i}".encode() + newline for i in range(1, 21)))
                byte_offset = len(b"line_1" + newline + b"line_2" + newline)
                offset_read = run_file_op({
                    "op": "read",
                    "root": str(self.tree),
                    "cwd": str(self.tree),
                    "file_path": str(sample),
                    "content_offset": byte_offset,
                    "limit": 2,
                })
                self.assertEqual(offset_read["status"], "partial")
                self.assertEqual(offset_read["file"]["line_start"], 3)
                self.assertIn("3 | line_3", offset_read["file"]["content"])
                self.assertTrue(offset_read["file"]["sha256"])

    def test_outline_and_code_item_inspection(self) -> None:
        code_file = self.tree / "module.py"
        code_file.write_text(
            "import os\n\n"
            "class AttentionRunner:\n"
            "    def __init__(self, hidden_size: int):\n"
            "        self.hidden_size = hidden_size\n\n"
            "    def forward(self, x):\n"
            "        return x * self.hidden_size\n\n"
            "def top_helper(val):\n"
            "    return val + 1\n",
            encoding="utf-8",
        )
        outline_data = run_file_op({
            "op": "read",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(code_file),
            "outline": True,
        })
        self.assertEqual(outline_data["status"], "ok")
        names = [item["qualified_name"] for item in outline_data["file"]["outline"]]
        self.assertEqual(
            names,
            ["AttentionRunner", "AttentionRunner.__init__", "AttentionRunner.forward", "top_helper"],
        )
        self.assertTrue(outline_data["file"]["sha256"])

        item_data = run_file_op({
            "op": "read",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(code_file),
            "node_paths": ["AttentionRunner.forward"],
        })
        self.assertIn(item_data["status"], {"ok", "partial"})
        self.assertEqual(len(item_data["file"]["code_items"]), 1)
        self.assertEqual(item_data["file"]["code_items"][0]["qualified_name"], "AttentionRunner.forward")
        self.assertIn("return x * self.hidden_size", item_data["file"]["content"])

    def test_write_and_scoped_line_range_edits_across_harnesses(self) -> None:
        # Antigravity write_to_file with TargetFile auto-enables create_dirs
        w_args = normalize_arguments(
            "remote.write",
            {
                "TargetFile": str(self.tree / "nested" / "dir" / "calc.py"),
                "CodeContent": "def f():\n    return 0\n\ndef g():\n    return 0\n",
                "Overwrite": True,
                "Description": "Create calc.py",
            },
        )
        self.assertTrue(w_args["create_dirs"])
        self.assertTrue(w_args["overwrite"])
        self.assertEqual(w_args["file_path"], str(self.tree / "nested" / "dir" / "calc.py"))

        # Kimi WriteFile mode="append"
        kimi_args = normalize_arguments(
            "remote.write",
            {"path": "/tmp/a.py", "content": "x", "mode": "append"},
        )
        self.assertTrue(kimi_args["append"])

        target = self.tree / "scoped.py"
        target.write_text(
            "def first():\n"
            "    return 0\n"
            "\n"
            "def second():\n"
            "    return 0\n",
            encoding="utf-8",
        )
        # Without start_line/end_line, "return 0" is ambiguous (matches 2 times).
        ambiguous = run_file_op({
            "op": "edit",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(target),
            "old_string": "return 0",
            "new_string": "return 42",
            "replace_all": False,
        })
        self.assertEqual(ambiguous["status"], "old_string_not_unique")

        # With Antigravity StartLine=4, EndLine=5, only second() is modified!
        scoped = run_file_op({
            "op": "edit",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(target),
            "old_string": "return 0",
            "new_string": "return 42",
            "replace_all": False,
            "start_line": 4,
            "end_line": 5,
        })
        self.assertEqual(scoped["status"], "edited")
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "def first():\n    return 0\n\ndef second():\n    return 42\n",
        )

        # Antigravity multi_replace_file_content with ReplacementChunks and line delta
        multi_args = normalize_arguments(
            "remote.multi_edit",
            {
                "TargetFile": str(target),
                "ReplacementChunks": [
                    {
                        "TargetContent": "return 0",
                        "ReplacementContent": "val = 1\n    return val",
                        "StartLine": 1,
                        "EndLine": 2,
                    },
                    {
                        "TargetContent": "return 42",
                        "ReplacementContent": "return 99",
                        "StartLine": 4,
                        "EndLine": 5,
                    },
                ],
                "Instruction": "Update both functions",
                "Description": "Multi-chunk edit",
            },
        )
        multi_res = run_file_op({
            "op": "multi_edit",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "file_path": str(target),
            "edits": multi_args["edits"],
        })
        self.assertEqual(multi_res["status"], "edited")
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "def first():\n    val = 1\n    return val\n\ndef second():\n    return 99\n",
        )

    def test_grep_fixed_strings_includes_excludes_and_find_by_name_glob(self) -> None:
        (self.tree / "src").mkdir()
        (self.tree / "build").mkdir()
        (self.tree / "src" / "main.py").write_text("expr = a+b\nother = 1\n", encoding="utf-8")
        (self.tree / "src" / "notes.txt").write_text("expr = a+b in txt\n", encoding="utf-8")
        (self.tree / "build" / "gen.py").write_text("expr = a+b in build\n", encoding="utf-8")

        # Antigravity grep_search with IsRegex=False (literal "a+b"), MatchPerLine=True, Includes=["*.py"], Excludes=["build/**"]
        g_args = normalize_arguments(
            "remote.grep",
            {
                "SearchPath": str(self.tree),
                "Query": "a+b",
                "IsRegex": False,
                "MatchPerLine": True,
                "CaseInsensitive": True,
                "Includes": ["*.py"],
                "Excludes": ["gen.py"],
            },
        )
        self.assertTrue(g_args["fixed_strings"])
        self.assertEqual(g_args["output_mode"], "content")
        self.assertEqual(g_args["includes"], ["*.py"])
        self.assertEqual(g_args["excludes"], ["gen.py"])

        grep_res = run_grep({
            "op": "grep",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "path": str(self.tree),
            "pattern": "a+b",
            "fixed_strings": True,
            "output_mode": "content",
            "includes": ["*.py"],
            "excludes": ["gen.py"],
        })
        self.assertEqual(grep_res["status"], "ok")
        self.assertEqual(len(grep_res["matches"]), 1)
        self.assertIn("main.py", grep_res["matches"][0])

        # Antigravity find_by_name -> remote.glob with Extensions, Excludes, Type, MaxDepth
        f_args = normalize_arguments(
            "remote.glob",
            {
                "SearchDirectory": str(self.tree),
                "Extensions": ["py"],
                "Excludes": ["build"],
                "Type": "file",
                "MaxDepth": 3,
            },
        )
        self.assertEqual(f_args["pattern"], "*")
        self.assertEqual(f_args["extensions"], ["py"])
        self.assertEqual(f_args["excludes"], ["build"])
        self.assertEqual(f_args["entry_type"], "file")
        self.assertEqual(f_args["max_depth"], 3)

        glob_res = run_grep({
            "op": "glob",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "path": str(self.tree),
            "pattern": f_args["pattern"],
            "extensions": f_args["extensions"],
            "excludes": f_args["excludes"],
            "entry_type": f_args["entry_type"],
            "max_depth": f_args["max_depth"],
        })
        self.assertEqual(glob_res["status"], "ok")
        matched_paths = [m["path"] for m in glob_res["matches"]]
        self.assertEqual(matched_paths, [str(self.tree / "src" / "main.py")])

    def test_ls_recursive_and_ignore_globs(self) -> None:
        (self.tree / "pkg").mkdir()
        (self.tree / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (self.tree / "pkg" / "mod.pyc").write_bytes(b"\x00\x01")
        ls_args = normalize_arguments(
            "remote.ls",
            {"DirectoryPath": str(self.tree), "MaxDepth": 2, "Excludes": ["*.pyc"]},
        )
        ls_res = run_file_op({
            "op": "ls",
            "root": str(self.tree),
            "cwd": str(self.tree),
            "path": ls_args["path"],
            "max_depth": ls_args["max_depth"],
            "ignore_globs": ls_args["ignore_globs"],
        })
        self.assertEqual(ls_res["status"], "ok")
        names = [e["name"] for e in ls_res["entries"]]
        self.assertIn("pkg", names)
        self.assertIn("pkg/mod.py", names)
        self.assertNotIn("pkg/mod.pyc", names)

    def test_bash_and_manage_task_dispatch_across_harnesses(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22}
        # Antigravity run_command with CommandLine, Cwd, IsDaemon
        with mock.patch.object(mcp_tools, "remote_bash", return_value={"ok": True}) as run_bash:
            mcp_tools.call_tool(
                "remote_run_command",
                {**endpoint, "CommandLine": "python train.py", "Cwd": "/workspace", "IsDaemon": True},
            )
            self.assertEqual(run_bash.call_args.kwargs["command"], "python train.py")
            self.assertEqual(run_bash.call_args.kwargs["cwd"], "/workspace")
            self.assertEqual(run_bash.call_args.kwargs["yield_time_ms"], 200)

        # Antigravity manage_task Action="list" (no job_id required)
        list_res = mcp_tools.call_tool("remote_manage_task", {"Action": "list"})
        self.assertEqual(list_res["result"]["outcome"], "success")
        self.assertEqual(list_res["result"]["job_count"], 0)

        # Antigravity manage_task Action="kill" -> dispatches to remote_job_stop
        with mock.patch.object(mcp_tools, "remote_job_stop", return_value={"stopped": True}) as run_stop:
            mcp_tools.call_tool("remote_manage_task", {"Action": "kill", "TaskId": "job-123", "force": True})
            self.assertEqual(run_stop.call_args.kwargs["job_id"], "job-123")
            self.assertTrue(run_stop.call_args.kwargs["force"])

        # Antigravity manage_task Action="send_input" -> dispatches to remote_job_stdin
        with mock.patch.object(mcp_tools, "remote_job_stdin", return_value={"sent": True}) as run_stdin:
            mcp_tools.call_tool("remote_manage_task", {"Action": "send_input", "TaskId": "job-123", "Input": "yes\n"})
            self.assertEqual(run_stdin.call_args.kwargs["job_id"], "job-123")
            self.assertEqual(run_stdin.call_args.kwargs["chars"], "yes\n")


if __name__ == "__main__":
    unittest.main()
