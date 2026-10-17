"""Client-parity behavior: native-habit aliases, search flags, write append,
read-from-end, binary detection, and interactive job stdin.

The REMOTE_*_PY executors run through real local ``python3 -c`` subprocesses
(the same convention as test_grep_fallback.py), so the exact remote-side code
is exercised without SSH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.mcp.schemas import normalize_arguments  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402
import remote_dev.core.job_ops as job_ops  # noqa: E402
import remote_dev.core.search_ops as search_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
import remote_dev.mcp.tools as mcp_tools  # noqa: E402


def run_file_op(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", file_ops.REMOTE_FILE_PY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"file op helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_grep(payload: dict, *, path_env: str | None = None) -> dict:
    env = dict(os.environ)
    if path_env is not None:
        env["PATH"] = path_env
    proc = subprocess.run(
        [sys.executable, "-c", search_ops.REMOTE_SEARCH_PY],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"grep helper failed: {proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class NormalizeArgumentsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": temporary.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_aliases_fold_into_canonical_fields(self) -> None:
        args = normalize_arguments("remote.read", {"path": "/a/b.py", "line_offset": -50, "n_lines": 40})
        self.assertEqual(args, {"file_path": "/a/b.py", "offset": -50, "limit": 40})

    def test_canonical_key_wins_over_alias(self) -> None:
        args = normalize_arguments("remote.read", {"path": "/a.py", "file_path": "/b.py"})
        self.assertEqual(args["file_path"], "/b.py")
        self.assertNotIn("path", args)

    def test_grep_native_flags_and_unknown_keys(self) -> None:
        args = normalize_arguments(
            "remote.grep",
            {"pattern": "x", "-i": True, "-A": 2, "-B": 3, "-C": 1, "-n": False, "head_limit": 10, "output_mode": "count_matches", "lab": "gpu-1"},
        )
        self.assertEqual(args["case_insensitive"], True)
        self.assertEqual(args["after_context"], 2)
        self.assertEqual(args["before_context"], 3)
        self.assertEqual(args["context_lines"], 1)
        self.assertEqual(args["line_numbers"], False)
        self.assertEqual(args["limit"], 10)
        # count_matches is NOT folded into count: Kimi's mode counts total
        # matches per file (rg --count-matches), count counts matching lines.
        self.assertEqual(args["output_mode"], "count_matches")
        self.assertEqual(args["lab"], "gpu-1")
        for alias in ("-i", "-A", "-B", "-C", "-n", "head_limit"):
            self.assertNotIn(alias, args)

    def test_bash_codex_aliases(self) -> None:
        args = normalize_arguments("remote.bash", {"cmd": "ls", "workdir": "/srv/app"})
        self.assertEqual(args["command"], "ls")
        self.assertEqual(args["cwd"], "/srv/app")
        self.assertNotIn("cmd", args)
        self.assertNotIn("workdir", args)

    def test_tools_without_aliases_pass_through(self) -> None:
        args = {"host": "h", "port": 22, "foo": 1}
        self.assertEqual(normalize_arguments("remote.probe", args), args)

    def test_call_tool_applies_read_aliases(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_read", return_value={}) as execute:
            mcp_tools.call_tool("remote_read", {**endpoint, "path": "/x.py", "line_offset": -20, "n_lines": 10})
        self.assertEqual(execute.call_args.kwargs["file_path"], "/x.py")
        self.assertEqual(execute.call_args.kwargs["offset"], -20)
        self.assertEqual(execute.call_args.kwargs["limit"], 10)

    def test_aliased_required_fields_are_enforced_server_side(self) -> None:
        # file_path/command stay out of the wire schema's required list so
        # providers cannot reject alias-only calls; the server rejects a
        # genuinely missing field with an actionable message.
        from remote_dev.mcp.schemas import TOOL_SCHEMAS

        self.assertNotIn("file_path", TOOL_SCHEMAS["remote.read"].get("required", []))
        self.assertNotIn("command", TOOL_SCHEMAS["remote.bash"].get("required", []))
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_read") as execute:
            with self.assertRaisesRegex(ValueError, "file_path.*alias: path"):
                mcp_tools.call_tool("remote_read", endpoint)
            execute.assert_not_called()
        with mock.patch.object(mcp_tools, "remote_bash") as execute:
            with self.assertRaisesRegex(ValueError, "command.*alias: cmd"):
                mcp_tools.call_tool("remote_bash", endpoint)
            execute.assert_not_called()

    def test_call_tool_applies_bash_cmd_alias(self) -> None:
        endpoint = {"host": "example.invalid", "port": 22}
        with mock.patch.object(mcp_tools, "remote_bash", return_value={}) as execute:
            mcp_tools.call_tool("remote_bash", {**endpoint, "cmd": "ls -1", "workdir": "/srv"})
        self.assertEqual(execute.call_args.kwargs["command"], "ls -1")
        self.assertEqual(execute.call_args.kwargs["cwd"], "/srv")


class RemoteReadParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        (self.tree / "lines.txt").write_text("".join(f"line-{i}\n" for i in range(1, 11)), encoding="utf-8")

    def read(self, **overrides) -> dict:
        payload = {"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / "lines.txt")}
        payload.update(overrides)
        return run_file_op(payload)

    def test_negative_offset_reads_from_end(self) -> None:
        data = self.read(offset=-3, limit=2)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["line_start"], 8)
        self.assertIn("8 | line-8", data["file"]["content"])
        self.assertIn("9 | line-9", data["file"]["content"])

    def test_negative_offset_larger_than_file_clamps_to_start(self) -> None:
        data = self.read(offset=-500, limit=2)
        self.assertEqual(data["file"]["line_start"], 1)

    def test_zero_offset_folds_to_default_first_page(self) -> None:
        payload = {"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / "lines.txt"), "offset": 0}
        data = run_file_op(payload)
        self.assertIn(data["status"], {"ok", "partial"})
        self.assertEqual(data["file"]["line_start"], 1)

    def test_binary_file_returns_actionable_error(self) -> None:
        blob = self.tree / "blob.bin"
        blob.write_bytes(b"\x89PNG\x00\x0d\x0a")
        data = run_file_op({"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(blob)})
        self.assertEqual(data["status"], "binary_file")
        self.assertIn("remote.artifact_pull", data["error"])

    def test_utf8_text_with_replacement_chars_is_not_binary(self) -> None:
        text_file = self.tree / "gbk.txt"
        text_file.write_bytes("中文\n".encode("gbk"))
        data = run_file_op({"op": "read", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(text_file)})
        self.assertIn(data["status"], {"ok", "partial"})


class RemoteWriteAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)

    def write(self, name: str, content: str, **flags) -> dict:
        payload = {"op": "write", "root": str(self.tree), "cwd": str(self.tree), "file_path": str(self.tree / name), "content": content}
        payload.update(flags)
        return run_file_op(payload)

    def test_append_extends_existing_file(self) -> None:
        self.write("a.txt", "first\n")
        data = self.write("a.txt", "second\n", append=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual(data["appended"], True)
        self.assertEqual((self.tree / "a.txt").read_text(encoding="utf-8"), "first\nsecond\n")

    def test_append_creates_missing_file(self) -> None:
        data = self.write("new.txt", "created\n", append=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual((self.tree / "new.txt").read_text(encoding="utf-8"), "created\n")

    def test_append_and_overwrite_are_rejected_together(self) -> None:
        self.write("a.txt", "first\n")
        data = self.write("a.txt", "x", append=True, overwrite=True)
        self.assertEqual(data["status"], "invalid_flags")

    def test_append_still_refuses_symlink(self) -> None:
        target = self.tree / "real.txt"
        target.write_text("real\n", encoding="utf-8")
        link = self.tree / "link.txt"
        link.symlink_to(target)
        data = self.write("link.txt", "x", append=True)
        self.assertEqual(data["status"], "symlink_not_allowed")


class RemoteGrepParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        (self.tree / "a.py").write_text("Alpha\nbeta\nGAMMA\ndelta\n", encoding="utf-8")
        (self.tree / "b.py").write_text("nothing here\n", encoding="utf-8")
        hidden = self.tree / ".hidden"
        hidden.mkdir()
        (hidden / "c.py").write_text("alpha in hidden\n", encoding="utf-8")

    def grep(self, path_env: str | None = None, **overrides) -> dict:
        if os.name == "nt" and shutil.which("rg") is None:
            self.skipTest("native Windows search fixture requires ripgrep")
        payload = {"op": "grep", "root": str(self.tree), "cwd": str(self.tree), "path": str(self.tree), "pattern": "alpha"}
        payload.update(overrides)
        return run_grep(payload, path_env=path_env)

    def grep_fallback_path(self) -> str:
        # A PATH containing grep but not rg forces the POSIX fallback branch.
        grep = shutil.which("grep")
        if not grep:
            self.skipTest("POSIX grep fallback requires a local grep executable")
        link_dir = self.tree / "bin"
        link_dir.mkdir(exist_ok=True)
        link = link_dir / "grep"
        if not link.exists():
            link.symlink_to(grep)
        return str(link_dir)

    def search_paths(self):
        # The grep-only symlink/PATH fixture emulates a POSIX remote host.
        # Native Windows still exercises rg even when that fixture is unavailable.
        yield None
        if os.name != "nt" and shutil.which("grep"):
            yield self.grep_fallback_path()

    def test_case_insensitive_content(self) -> None:
        for env in self.search_paths():
            data = self.grep(env, output_mode="content", case_insensitive=True)
            self.assertEqual(data["status"], "ok")
            joined = "\n".join(data["matches"])
            self.assertIn("Alpha", joined)

    def test_context_lines(self) -> None:
        for env in self.search_paths():
            data = self.grep(env, output_mode="content", pattern="beta", context_lines=1)
            joined = "\n".join(data["matches"])
            self.assertIn("Alpha", joined)
            self.assertIn("GAMMA", joined)

    def test_before_and_after_context(self) -> None:
        data = self.grep(None, output_mode="content", pattern="beta", before_context=1, after_context=0)
        joined = "\n".join(data["matches"])
        self.assertIn("Alpha", joined)
        self.assertNotIn("GAMMA", joined)

    def test_line_numbers_can_be_disabled(self) -> None:
        data = self.grep(None, output_mode="content", line_numbers=False)
        self.assertTrue(all("Alpha" in line and ":1:" not in line for line in data["matches"] if "a.py" in line))

    def test_offset_skips_result_lines(self) -> None:
        full = self.grep(None, output_mode="content", case_insensitive=True, pattern="a", limit=50)
        self.assertGreaterEqual(len(full["matches"]), 3)
        skipped = self.grep(None, output_mode="content", case_insensitive=True, pattern="a", offset=1, limit=50)
        self.assertEqual(skipped["matches"], full["matches"][1:])
        self.assertEqual(skipped["offset"], 1)

    def test_hidden_dirs_skipped_by_default_and_included_with_flag(self) -> None:
        default = self.grep(None, output_mode="content", case_insensitive=True, pattern="alpha in hidden")
        self.assertEqual(default["matches"], [])
        included = self.grep(None, output_mode="content", case_insensitive=True, pattern="alpha in hidden", include_ignored=True)
        self.assertTrue(any("c.py" in line for line in included["matches"]))
        if os.name != "nt" and shutil.which("grep"):
            fallback = self.grep(self.grep_fallback_path(), output_mode="content", case_insensitive=True, pattern="alpha in hidden", include_ignored=True)
            self.assertTrue(any("c.py" in line for line in fallback["matches"]))
            self.assertTrue(any("gitignore" in warning for warning in fallback["warnings"]))

    def test_count_and_count_matches_are_semantically_distinct(self) -> None:
        # Distinguishing input: one line holds two matches of the pattern.
        # count (rg -c) counts matching *lines*; count_matches (Kimi habit,
        # rg --count-matches) counts *matches*.
        multi = self.tree / "multi.txt"
        multi.write_text("aa\nbb\n", encoding="utf-8")
        for env in self.search_paths():
            lines = self.grep(env, output_mode="count", pattern="a", glob="multi.txt")
            matches = self.grep(env, output_mode="count_matches", pattern="a", glob="multi.txt")
            self.assertEqual([row.replace("\\", "/") for row in lines["matches"]], [f"{multi.as_posix()}:1"], f"env={env}")
            self.assertEqual([row.replace("\\", "/") for row in matches["matches"]], [f"{multi.as_posix()}:2"], f"env={env}")
            self.assertEqual(matches["total_matches"], 2, f"env={env}")
            self.assertIsNone(lines["total_matches"], f"env={env}")

    def test_new_flags_reach_remote_payload(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        captured = {}

        def fake_run(_endpoint, _script, payload, **_kwargs):
            captured.update(payload)
            return {"status": "ok", "matches": [], "truncated": False, "warnings": []}

        with mock.patch.object(search_ops, "run_remote_python", fake_run):
            search_ops.remote_grep(
                endpoint,
                pattern="x",
                case_insensitive=True,
                before_context=2,
                after_context=3,
                context_lines=1,
                line_numbers=False,
                include_ignored=True,
                offset=5,
            )
        self.assertEqual(captured["case_insensitive"], True)
        self.assertEqual(captured["before_context"], 2)
        self.assertEqual(captured["after_context"], 3)
        self.assertEqual(captured["context_lines"], 1)
        self.assertEqual(captured["line_numbers"], False)
        self.assertEqual(captured["include_ignored"], True)
        self.assertEqual(captured["offset"], 5)

    def test_context_on_non_content_mode_warns(self) -> None:
        data = self.grep(None, output_mode="files_with_matches", context_lines=2)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(any("context" in warning for warning in data["warnings"]))


@unittest.skipIf(os.name == "nt", "remote Linux worker requires fcntl and FIFO semantics")
class WorkerStdinActionTests(unittest.TestCase):
    """The worker's stdin control action is exercised locally (macOS-safe):
    job_status is stubbed, the FIFO and EOF marker are real filesystem objects.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        self.job_dir = self.tree / ".remote-dev" / "jobs" / "job-stdin-local"

    def _control(self, request):
        import remote_dev.processes.worker as worker_mod

        return worker_mod.control_job(request, worker_mod.__file__ and "")

    def test_stdin_write_reaches_fifo_and_eof_marks_file(self) -> None:
        import remote_dev.processes.worker as worker_mod

        root = self.tree
        (self.job_dir).mkdir(parents=True)
        request_base = {"root": str(root), "job_id": "job-stdin-local"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            # Directory exists but job was not prepared as interactive: no FIFO.
            reply = self._control({**request_base, "action": "stdin", "data": "x"})
            self.assertFalse(reply["accepted"])
            self.assertIn("writable remote.bash session", reply["reason"])
            # Prepare the FIFO like an interactive prepare would.
            os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
            reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
            try:
                reply = self._control({**request_base, "action": "stdin", "data": "hello\n", "eof": True})
            finally:
                pass
            self.assertTrue(reply["accepted"])
            self.assertEqual(reply["written"], 6)
            self.assertTrue(reply["eof"])
            self.assertEqual(os.read(reader, 64), b"hello\n")
            os.close(reader)
            self.assertTrue((self.job_dir / "stdin-eof.json").exists())

    def test_stdin_rejected_when_not_running(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": "x"})
        self.assertFalse(reply["accepted"])
        self.assertIn("not running", reply["reason"])

    def test_stdin_empty_poll_on_terminal_job_is_accepted(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": ""})
        self.assertTrue(reply["accepted"])
        self.assertTrue(reply["polled_terminal"])

    def test_stdin_write_after_eof_is_rejected(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        worker_mod.atomic_json(self.job_dir / "stdin-eof.json", {"at": 0})
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": "more"})
        self.assertFalse(reply["accepted"])
        self.assertIn("already closed", reply["reason"])

    def test_stdin_large_write_reports_partial_without_loss_claim(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            big = "x" * (1024 * 1024)
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                reply = self._control({"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin", "data": big})
            self.assertTrue(reply["accepted"])
            self.assertLess(reply["written"], len(big))
            self.assertTrue(reply["stdin_buffer_full"])
            drained = os.read(reader, 2 * 1024 * 1024)
            self.assertEqual(drained, big[: reply["written"]].encode())
        finally:
            os.close(reader)

    def test_stdin_partial_write_defers_eof_and_accepts_retry(self) -> None:
        """Backpressure and EOF together: a partially accepted write with
        eof=true must not close stdin; the exact remainder stays retryable."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            big = "x" * (1024 * 1024)
            request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin"}
            drained = bytearray()
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                first = self._control({**request, "data": big, "eof": True})
                self.assertTrue(first["accepted"])
                self.assertLess(first["written"], len(big))
                self.assertTrue(first["stdin_buffer_full"])
                # EOF is deferred until every byte of this operation has been
                # accepted: no marker file, and the channel stays open.
                self.assertFalse(first["eof"])
                self.assertTrue(first["eof_deferred"])
                self.assertFalse((self.job_dir / "stdin-eof.json").exists())
                drained += os.read(reader, 2 * 1024 * 1024)
                remainder = big[first["written_chars"]:]
                row = first
                for _ in range(1000):
                    if not remainder:
                        break
                    row = self._control({**request, "data": remainder, "eof": True})
                    self.assertTrue(row["accepted"])
                    self.assertGreater(row["written"], 0)
                    drained += os.read(reader, 2 * 1024 * 1024)
                    remainder = remainder[row["written_chars"]:]
                else:
                    self.fail("stdin retry loop did not finish")
                self.assertFalse(row["stdin_buffer_full"])
                self.assertTrue(row["eof"])
            self.assertEqual(bytes(drained), big.encode())
            self.assertTrue((self.job_dir / "stdin-eof.json").exists())
        finally:
            os.close(reader)

    def test_stdin_unicode_write_reports_character_accounting(self) -> None:
        """written_chars is the exact character count of the accepted byte
        prefix; slicing the original string there never splits a character."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
        try:
            data = "中文日志\n" * 5000  # 15 bytes per repetition
            request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin"}
            pending = data
            drained = bytearray()
            with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
                for _ in range(1000):
                    row = self._control({**request, "data": pending, "eof": True})
                    self.assertTrue(row["accepted"])
                    self.assertGreater(row["written"], 0)
                    accepted = pending[: row["written_chars"]]
                    self.assertEqual(len(accepted.encode("utf-8")), row["written"])
                    drained += os.read(reader, 2 * 1024 * 1024)
                    pending = pending[row["written_chars"]:]
                    if not row["stdin_buffer_full"]:
                        break
                else:
                    self.fail("stdin retry loop did not finish")
                self.assertTrue(row["eof"])
            self.assertEqual(bytes(drained), data.encode("utf-8"))
        finally:
            os.close(reader)

    def test_stdin_write_without_fifo_reader_is_retryable_not_crash(self) -> None:
        """Readiness race: a running interactive job whose FIFO has no reader
        yet must return a truthful retryable refusal — no crash, no accepted
        or dropped bytes, and eof is not armed. Attaching the reader makes the
        identical retry succeed."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        os.mkfifo(self.job_dir / "stdin.pipe", 0o600)
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "stdin"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            reply = self._control({**request, "data": "hello\n", "eof": True})
            self.assertFalse(reply["accepted"])
            self.assertEqual(reply["written"], 0)
            self.assertTrue(reply["retryable"])
            self.assertIn("not ready", reply["reason"])
            self.assertFalse((self.job_dir / "stdin-eof.json").exists())
            reader = os.open(self.job_dir / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
            try:
                retry = self._control({**request, "data": "hello\n", "eof": True})
                self.assertTrue(retry["accepted"])
                self.assertEqual(retry["written"], 6)
                self.assertTrue(retry["eof"])
                self.assertEqual(os.read(reader, 64), b"hello\n")
            finally:
                os.close(reader)
            self.assertTrue((self.job_dir / "stdin-eof.json").exists())

    def test_tail_offset_mode_is_incremental(self) -> None:
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        log = self.job_dir / "stdout.log"
        log.write_bytes(b"first\n")
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        row = self._control({**request, "stdout_offset": 0, "max_bytes": 4096})
        self.assertEqual(row["stdout"], "first\n")
        self.assertEqual(row["stdout_offset"], 6)
        self.assertEqual(row["stdout_bytes_remaining"], 0)
        log.write_bytes(b"first\nsecond\n")
        row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 4096})
        self.assertEqual(row["stdout"], "second\n")
        row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 4096})
        self.assertEqual(row["stdout"], "")

    def test_tail_incremental_preserves_split_utf8_characters(self) -> None:
        """A character split by the byte budget is held back for the next page
        instead of corrupting into U+FFFD; paged reads reassemble exactly."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        original = "中文日志" * 100
        (self.job_dir / "stdout.log").write_text(original, encoding="utf-8")
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            combined = ""
            cursor = 0
            for _ in range(20):
                row = self._control({**request, "stdout_offset": cursor, "max_bytes": 256})
                combined += row["stdout"]
                cursor = row["stdout_offset"]
                if row["stdout_bytes_remaining"] == 0:
                    break
            else:
                self.fail("paged incremental read did not finish")
        self.assertEqual(combined, original)
        self.assertNotIn("\ufffd", combined)
        self.assertEqual(cursor, len(original.encode("utf-8")))

    def test_tail_incremental_flushes_invalid_bytes_without_stalling(self) -> None:
        """Genuinely invalid bytes are not an incomplete sequence: they surface
        as U+FFFD and the cursor still advances past them."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        log = self.job_dir / "stdout.log"
        log.write_bytes("ok\n".encode() + b"\xff\xfe" + "end\n".encode())
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 4})
            self.assertEqual(row["stdout"], "ok\n\ufffd")
            self.assertEqual(row["stdout_offset"], 4)
            row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 256})
            self.assertEqual(row["stdout"], "\ufffdend\n")
            self.assertEqual(row["stdout_bytes_remaining"], 0)

    def test_tail_incremental_flushes_unfinished_tail_on_terminal_job(self) -> None:
        """A terminal job at end of file can never complete a trailing partial
        sequence, so it flushes as U+FFFD instead of staying held back."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        (self.job_dir / "stdout.log").write_bytes("tail\n".encode() + "中".encode("utf-8")[:2])
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "succeeded", "quiet": True}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 256})
        self.assertEqual(row["stdout"], "tail\n\ufffd")
        self.assertEqual(row["stdout_offset"], 7)
        self.assertEqual(row["stdout_bytes_remaining"], 0)

    def test_tail_incremental_tiny_budget_still_advances(self) -> None:
        """A budget smaller than one UTF-8 character must still move the cursor
        (progress guarantee); the partial bytes flush as U+FFFD."""
        import remote_dev.processes.worker as worker_mod

        (self.job_dir).mkdir(parents=True)
        (self.job_dir / "stdout.log").write_bytes("中".encode("utf-8"))  # 3 bytes
        request = {"root": str(self.tree), "job_id": "job-stdin-local", "action": "tail"}
        with mock.patch.object(worker_mod, "job_status", return_value={"state": "running", "quiet": False}):
            row = self._control({**request, "stdout_offset": 0, "max_bytes": 2})
            self.assertEqual(row["stdout"], "\ufffd")
            self.assertEqual(row["stdout_offset"], 2)
            row = self._control({**request, "stdout_offset": row["stdout_offset"], "max_bytes": 2})
            self.assertEqual(row["stdout_offset"], 3)
            self.assertEqual(row["stdout_bytes_remaining"], 0)


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
        sample.write_text("".join(f"line_{i}\n" for i in range(1, 21)), encoding="utf-8")

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

        # ContentOffset byte offset
        byte_offset = len("line_1\nline_2\n".encode("utf-8"))
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
