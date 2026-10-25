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


@unittest.skipIf(os.name == "nt", "POSIX remote worker simulation; Windows client normalization is covered separately")
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


@unittest.skipIf(os.name == "nt", "POSIX remote worker simulation; Windows client normalization is covered separately")
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


if __name__ == "__main__":
    unittest.main()
