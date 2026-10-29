"""Bounded job-log previews and truthful full-log refs (core + native TaskOutput)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import remote_dev.core.job_ops as job_ops
import remote_dev.core.state_store as state_store
import remote_dev.mcp.tools as tools
from remote_dev.core import job_logs, job_results
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.shell_ops import remote_bash
from remote_dev.core.preview import MAX_TEXT_CHARS
from remote_dev.mcp import native_handlers_claude as claude_handlers
from remote_dev.mcp import native_handlers_kimi as kimi_handlers
from remote_dev.mcp.native_common import _tail_text


def _supervisor(**overrides):
    row = {
        "state": "running",
        "quiet": False,
        "receipt": {"pid": 42, "supervision": "subreaper"},
        "processes": [{"pid": 42}],
        "unknown": [],
        "result": None,
        "remote_dir": "/srv/app/.remote-dev/jobs/job-test123",
        "gate_open": True,
    }
    row.update(overrides)
    return row


class JobLogFormatterTests(unittest.TestCase):
    def test_bounded_head_tail_preserves_utf8_and_counts(self) -> None:
        value = "开" * 9000 + "尾" * 9000
        result = job_logs.preview_stream_text(value, head_chars=120, tail_chars=120)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["head"], value[:120])
        self.assertEqual(result["tail"], value[-120:])
        self.assertLessEqual(len(result["head"]) + len(result["tail"]), 240)
        self.assertEqual(result["omitted_chars"], len(value) - 240)
        self.assertEqual(result["omitted_bytes"], len((value[120:-120]).encode("utf-8")))
        self.assertIn("<remote-dev preview:", result["text"])
        self.assertIn(f"{result['omitted_chars']:,} characters", result["text"])
        self.assertIn(f"{result['omitted_bytes']:,} UTF-8 bytes", result["text"])
        result["head"].encode("utf-8")
        result["tail"].encode("utf-8")

    def test_zero_tail_never_leaks_the_source(self) -> None:
        result = job_logs.preview_stream_text("x" * 500, head_chars=5, tail_chars=0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["tail"], "")
        self.assertEqual(result["omitted_chars"], 495)
        self.assertNotIn("x" * 500, result["text"])

    def test_repeated_lines_fold_only_when_they_shrink(self) -> None:
        noisy = "header\n" + ("warning: unused parameter\n" * 100) + "error: real\n"
        result = job_logs.preview_stream_text(noisy)
        self.assertTrue(result["repeated_lines_folded"])
        self.assertIn("[previous line repeated 99 more times]", result["text"])
        self.assertFalse(result["truncated"])
        quiet = "header\n\n\n\nfooter\n" + ("ok\n" * 4)
        self.assertEqual(job_logs.preview_stream_text(quiet)["text"], quiet)

    def test_file_edges_report_exact_omitted_bytes_and_lower_bound_chars(self) -> None:
        result = job_logs.preview_stream_edges("HEAD" * 1000, "TAIL" * 1000, total_bytes=100_000)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["omitted_chars_exact"])
        self.assertGreater(result["omitted_bytes"], 0)
        self.assertGreater(result["omitted_chars"], 0)
        self.assertIn("at least", result["text"])

    def test_refs_render_remote_and_local_paths_without_calling_cache_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_path = Path(tmp) / "job-1.json"
            local = Path(tmp) / "job-1.stdout.log"
            local.write_bytes(b"cached only\n")
            refs = job_logs.job_log_refs(
                job_id="job-1",
                record_path=record_path,
                record={"job_id": "job-1", "remote_dir": "/srv/app/.remote-dev/jobs/job-1"},
                observed_offsets={"stdout": 11, "stderr": 0},
                unread_bytes={"stdout": 99, "stderr": 0},
            )
            self.assertEqual(refs["local_cache_status"], "partial")
            self.assertEqual(refs["local_cache_streams"]["stdout"]["missing_bytes"], 98)
            self.assertEqual(refs["remote_raw_stdout"], "/srv/app/.remote-dev/jobs/job-1/stdout.log")
            self.assertEqual(refs["local_cached_stdout"], str(local))
            text = job_logs.render_job_refs_text(refs)
            self.assertIn("remote raw stdout", text)
            self.assertIn("local cached stdout", text)
            self.assertIn("partial:", text)
            self.assertNotIn("local cached stdout: complete", text)


class SessionResultPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": str(Path(self.temp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app", cwd="/srv/app")
        self.job_id = "job-preview-001"
        self.record_path = state_store.job_record_path(self.endpoint, self.job_id)
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": self.job_id,
            "target": self.endpoint.to_result_target(),
            "remote_dir": f"{self.endpoint.root}/.remote-dev/jobs/{self.job_id}",
            "cwd": self.endpoint.cwd,
            "description": "preview demo",
            "started_at": "2026-10-25T00:00:00Z",
            "state": "running",
            "stdin_cursors": {"stdout_offset": 0, "stderr_offset": 0},
        }
        state_store.atomic_write_json(self.record_path, record)

    def _row(self, stdout: str, stderr: str, *, state="succeeded", exit_code=0, **extra):
        row = {
            "state": state,
            "quiet": True,
            "result": {"state": state, "exit_code": exit_code},
            "stdout": stdout,
            "stderr": stderr,
            "stdout_offset": len(stdout.encode("utf-8")),
            "stderr_offset": len(stderr.encode("utf-8")),
            "stdout_bytes_remaining": 0,
            "stderr_bytes_remaining": 0,
            "accepted": True,
        }
        row.update(extra)
        return row

    def test_large_stdout_stderr_are_bounded_with_refs_in_text_and_result(self) -> None:
        stdout = "HEAD-★\n" + ("中" * 30_000) + "\nTAIL-STDOUT\n"
        stderr = "HEAD-ERR\n" + ("e" * 30_000) + "\nTAIL-STDERR\n"
        row = self._row(stdout, stderr)
        job_id = "job-preview-large"
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops.start_remote_job(
                self.endpoint, command="python train.py", job_id=job_id, wait=True
            )
        result = payload["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["preview"]["stdout"].startswith("HEAD-★\n"))
        self.assertIn("TAIL-STDOUT", result["preview"]["stdout"])
        self.assertIn("TAIL-STDERR", result["preview"]["stderr"])
        self.assertLessEqual(len(result["preview"]["stdout"]), 9000)
        self.assertLessEqual(len(result["preview"]["stderr"]), 9000)
        self.assertTrue(result["output_preview"]["truncated"])
        self.assertFalse(result["output_preview"]["full_page"])
        self.assertGreater(result["output_preview"]["omitted_chars"], 0)
        self.assertGreater(result["output_preview"]["omitted_bytes"], 0)
        self.assertIn("<remote-dev preview:", payload["text"])
        self.assertIn("remote raw stdout", payload["text"])
        self.assertIn("local cached stdout", payload["text"])
        self.assertIn("remote.read", payload["text"])
        self.assertEqual(result["refs"]["remote_raw_stdout"], f"{self.endpoint.root}/.remote-dev/jobs/{job_id}/stdout.log")
        self.assertEqual(result["refs"]["remote_raw_stderr"], f"{self.endpoint.root}/.remote-dev/jobs/{job_id}/stderr.log")
        self.assertEqual(Path(result["refs"]["local_cached_stdout"]).read_text(encoding="utf-8"), stdout)
        self.assertEqual(result["refs"]["local_cache_status"], "complete")
        saved = json.loads(state_store.job_record_path(self.endpoint, job_id).read_text(encoding="utf-8"))
        self.assertEqual(saved["stdin_cursors"]["stdout_offset"], row["stdout_offset"])
        self.assertEqual(saved["stdin_cursors"]["stderr_offset"], row["stderr_offset"])

    def test_incremental_stdin_pages_reconstruct_exactly_across_head_tail_boundary(self) -> None:
        stdout = ("0123456789abcdef" * 1250)
        offset = 0

        def fake_control(_endpoint, _job_id, action, **params):
            nonlocal offset
            self.assertEqual(action, "exchange")
            start = int(params["stdout_offset"])
            budget = int(params["max_bytes"])
            chunk = stdout[start:start + budget]
            offset = start + len(chunk)
            done = offset >= len(stdout)
            return {
                "state": "succeeded" if done else "running",
                "quiet": done,
                "result": {"state": "succeeded", "exit_code": 0} if done else None,
                "stdout": chunk,
                "stderr": "",
                "stdout_offset": offset,
                "stderr_offset": 0,
                "stdout_bytes_remaining": len(stdout) - offset,
                "stderr_bytes_remaining": 0,
                "accepted": True,
            }

        pages: list[str] = []
        while True:
            with mock.patch.object(job_ops, "control", side_effect=fake_control):
                payload = job_ops.remote_job_stdin(
                    None, job_id=self.job_id, yield_time_ms=0, max_output_tokens=4096
                )
            result = payload["result"]
            page = result["preview"]["stdout"]
            pages.append(page)
            self.assertFalse(result["output_preview"]["truncated"])
            self.assertTrue(result["output_preview"]["full_page"])
            self.assertNotIn("<remote-dev preview:", page)
            if result["session_id"] is None:
                break
        self.assertGreater(len(pages[0]), 8000)
        self.assertEqual("".join(pages), stdout, "incremental pages must not gap or replay")
        self.assertEqual(offset, len(stdout))

    def test_incremental_output_returns_a_full_delta_page(self) -> None:
        stdout = "x" * 20_000
        offset = 0

        def fake_control(_endpoint, _job_id, action, **params):
            nonlocal offset
            self.assertEqual(action, "exchange")
            start = int(params["stdout_offset"])
            budget = int(params["max_bytes"])
            chunk = stdout[start:start + budget]
            offset = start + len(chunk)
            return {
                "state": "running",
                "quiet": False,
                "result": None,
                "stdout": chunk,
                "stderr": "",
                "stdout_offset": offset,
                "stderr_offset": 0,
                "stdout_bytes_remaining": len(stdout) - offset,
                "stderr_bytes_remaining": 0,
                "accepted": True,
            }

        with mock.patch.object(job_ops, "control", side_effect=fake_control):
            payload = job_ops.remote_job_output_incremental(None, job_id=self.job_id, max_bytes=8192)
        result = payload["result"]
        self.assertEqual(result["preview"]["stdout"], stdout[:8192])
        self.assertFalse(result["output_preview"]["truncated"])
        self.assertNotIn("<remote-dev preview:", result["preview"]["stdout"])

    def test_terminal_command_summary_reads_actual_cache_edges(self) -> None:
        stdout = "HEAD-\n" + ("progress line\n" * 5000) + "TAIL-STDOUT\n"
        stderr = "ERR-HEAD\n" + ("warning\n" * 3000) + "ERR-TAIL\n"
        Path(str(self.record_path.with_name(self.record_path.stem + ".stdout.log"))).write_text(
            stdout, encoding="utf-8"
        )
        Path(str(self.record_path.with_name(self.record_path.stem + ".stderr.log"))).write_text(
            stderr, encoding="utf-8"
        )
        row = self._row("HEAD-\n", "ERR-HEAD\n", state="failed", exit_code=7)
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops._session_result(
                self.endpoint, json.loads(self.record_path.read_text()), self.record_path,
                row, tool="remote.bash", started="2026-10-25T00:00:00Z",
                start=0.0, budget=10000, prefer_cache_edges=True,
            )
        self.assertIn("TAIL-STDOUT", payload["result"]["preview"]["stdout"])
        self.assertIn("ERR-TAIL", payload["result"]["preview"]["stderr"])
        self.assertIn("remote raw stdout", payload["text"])
        self.assertEqual(payload["result"]["exit_code"], 7)

    def test_cache_preview_deadline_is_not_reported_as_missing_output(self) -> None:
        row = self._row("HEAD\n", "", state="succeeded", exit_code=0)
        with mock.patch.object(job_results, "local_stream_preview",
                               side_effect=TimeoutError("cache preview deadline")):
            with self.assertRaisesRegex(TimeoutError, "cache preview deadline"):
                job_ops._session_result(
                    self.endpoint, json.loads(self.record_path.read_text()), self.record_path,
                    row, tool="remote.bash", started="2026-10-25T00:00:00Z",
                    start=0.0, budget=10000, prefer_cache_edges=True,
                )

    def test_preview_degradation_preserves_command_result_and_refs(self) -> None:
        row = self._row("HEAD\n", "ERR\n", state="failed", exit_code=7)
        row["stdout_bytes_remaining"] = 1000
        payload = job_ops._session_result(
            self.endpoint, json.loads(self.record_path.read_text()), self.record_path,
            row, tool="remote.bash", started="2026-10-25T00:00:00Z",
            start=0.0, budget=10000, preview_error="TimeoutError: preview deadline",
        )
        result = payload["result"]
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(result["preview_degraded"])
        self.assertTrue(result["refs"]["remote_raw_stdout"].endswith("/stdout.log"))
        self.assertIn("Preview degraded", payload["text"])
        self.assertIn("full raw log refs", payload["text"])

    def test_preview_timeout_keeps_completed_command_outcome(self) -> None:
        job_id = "job-preview-timeout"
        row = self._row("HEAD\n", "ERR\n", state="failed", exit_code=7,
                        stdout_bytes_remaining=900, stderr_bytes_remaining=50)

        def fake_control(_endpoint, _job_id, action, **params):
            if action == "launch":
                return row
            raise TimeoutError("preview tail deadline")

        with mock.patch.object(job_ops, "control", side_effect=fake_control):
            payload = remote_bash(
                self.endpoint, command="false", job_id=job_id, foreground=True,
                wait=False,
            )
        result = payload["result"]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["preview_degraded"])
        self.assertIn("Preview degraded", payload["text"])
        self.assertIn("full raw log refs", payload["text"])
        self.assertTrue(result["refs"]["remote_raw_stdout"].endswith("/stdout.log"))

    def test_partial_first_page_is_not_called_full_raw_log(self) -> None:
        stdout = "first page only\n"
        row = self._row(stdout, "", state="running", exit_code=None)
        row["quiet"] = False
        row["stdout_bytes_remaining"] = 50_000
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops.remote_job_stdin(None, job_id=self.job_id, yield_time_ms=0)
        result = payload["result"]
        self.assertEqual(result["refs"]["local_cache_status"], "partial")
        stream = result["refs"]["local_cache_streams"]["stdout"]
        self.assertEqual(stream["cached_bytes"], len(stdout.encode()))
        self.assertEqual(stream["missing_bytes"], 50_000)
        self.assertIn("local cached stdout", payload["text"])
        self.assertIn("partial:", payload["text"])
        self.assertIn("Continue: remote.job_output", payload["text"])
        self.assertIn("remote raw stdout", payload["text"])

    def test_nonzero_exit_keeps_preview_and_real_refs(self) -> None:
        stdout = "before failure\n" + ("x" * 40_000) + "\nafter failure\n"
        stderr = "error detail 中文\n"
        row = self._row(stdout, stderr, state="failed", exit_code=7)
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops.remote_job_stdin(None, job_id=self.job_id, yield_time_ms=0)
        result = payload["result"]
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("after failure", result["preview"]["stdout"])
        self.assertIn("error detail 中文", result["preview"]["stderr"])
        self.assertIn("remote raw stderr", payload["text"])
        self.assertTrue(result["refs"]["remote_raw_stderr"].endswith("/stderr.log"))

    def test_local_recording_failure_keeps_remote_refs_and_marks_cache_unreliable(self) -> None:
        stdout = "completed output"
        stderr = "warning"
        row = self._row(stdout, stderr)
        with mock.patch.object(job_ops, "control", return_value=row), \
                mock.patch.object(job_ops, "_save_output", side_effect=OSError("isolated disk failure")):
            payload = job_ops.start_remote_job(
                self.endpoint, command="test command", job_id="job-recording-preview", wait=True
            )
        result = payload["result"]
        self.assertEqual(result["status"], "local_recording_failed")
        self.assertEqual(result["execution_outcome"], "success")
        self.assertEqual(result["preview"], {"stdout": stdout, "stderr": stderr})
        self.assertEqual(result["refs"]["local_cache_status"], "unreliable")
        self.assertTrue(result["refs"]["remote_raw_stdout"].endswith("/stdout.log"))
        self.assertTrue(result["refs"]["remote_raw_stderr"].endswith("/stderr.log"))
        self.assertIn("remote raw stdout", payload["text"])
        self.assertIn("unreliable", payload["text"])
        self.assertIn("Do not relaunch", payload["text"])

    def test_job_tail_carries_refs_and_stays_text_bounded(self) -> None:
        row = _supervisor(stdout="x" * (MAX_TEXT_CHARS * 2), stderr="")
        with mock.patch.object(job_ops, "control", return_value=row):
            payload = job_ops.remote_job_tail(None, job_id=self.job_id, lines=500)
        self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        self.assertIn("remote raw stdout", payload["text"])
        self.assertIn("local cached stdout", payload["text"])
        self.assertTrue(payload["result"]["refs"]["remote_raw_stdout"].endswith("/stdout.log"))


class NativeTaskOutputPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"REMOTE_DEV_STATE_DIR": str(Path(self.temp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.endpoint = Endpoint(host="1.2.3.4", port=46000, root="/srv/app", cwd="/srv/app")
        self.job_id = "job-native-preview"
        self.record_path = state_store.job_record_path(self.endpoint, self.job_id)
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": self.job_id,
            "target": self.endpoint.to_result_target(),
            "remote_dir": f"{self.endpoint.root}/.remote-dev/jobs/{self.job_id}",
            "cwd": self.endpoint.cwd,
            "description": "native preview",
            "command_preview": "python train.py",
            "started_at": "2026-10-25T00:00:00Z",
            "state": "succeeded",
            "session_scope": "scope",
            "stdin_cursors": {"stdout_offset": 0, "stderr_offset": 0},
        }
        state_store.atomic_write_json(self.record_path, record)
        self.stdout = "HEAD-★\n" + ("甲" * 40_000) + "\nTAIL-STDOUT\n"
        self.stderr = "ERR-HEAD\n" + ("e" * 20_000) + "\nERR-TAIL\n"
        Path(str(self.record_path.with_name(self.record_path.stem + ".stdout.log"))).write_text(self.stdout, encoding="utf-8")
        Path(str(self.record_path.with_name(self.record_path.stem + ".stderr.log"))).write_text(self.stderr, encoding="utf-8")

    def _canonical(self, name, args):
        if name == "remote.job_status":
            return {
                "text": "Remote job succeeded.\n",
                "result": {
                    "outcome": "success",
                    "status": "succeeded",
                    "job": {
                        "state": "succeeded",
                        "remote_status": {"exit_code": 0, "result": {"exit_code": 0}},
                    },
                },
            }
        if name == "remote.job_stdin":
            return {
                "text": "Remote command succeeded. Exit code: 0.\n",
                "result": {
                    "tool": "remote.job_stdin",
                    "outcome": "success",
                    "status": "succeeded",
                    "state": "succeeded",
                    "exit_code": 0,
                    "bytes_remaining": {"stdout": 0, "stderr": 0},
                    "cursors": {
                        "stdout_offset": len(self.stdout.encode()),
                        "stderr_offset": len(self.stderr.encode()),
                    },
                },
            }
        raise AssertionError(name)

    def test_claude_task_output_is_bounded_and_has_refs(self) -> None:
        with mock.patch.object(claude_handlers, "_assert_visible", return_value=None), \
                mock.patch.object(tools, "_call_canonical", side_effect=self._canonical):
            payload = tools.call_tool(
                "remote_TaskOutput", {"task_id": self.job_id, "block": False}, profile="claude"
            )
        result = payload["result"]
        output = result["task"]["output"]
        self.assertEqual(result["retrieval_status"], "success")
        self.assertTrue(result["task"]["output_truncated"])
        self.assertIn("HEAD-★", output)
        self.assertIn("TAIL-STDOUT", output)
        self.assertIn("ERR-TAIL", output)
        self.assertIn("<remote-dev preview:", output)
        self.assertLess(len(output), len(self.stdout) + len(self.stderr))
        self.assertIn("remote raw stdout", payload["text"])
        self.assertIn("local cached stdout", payload["text"])
        self.assertTrue(result["refs"]["remote_raw_stdout"].endswith("/stdout.log"))
        self.assertEqual(Path(result["refs"]["local_cached_stdout"]).read_text(encoding="utf-8"), self.stdout)

    def test_kimi_task_output_is_bounded_and_has_refs(self) -> None:
        with mock.patch.object(kimi_handlers, "_assert_visible", return_value=None), \
                mock.patch.object(tools, "_call_canonical", side_effect=self._canonical):
            payload = tools.call_tool(
                "remote_TaskOutput", {"task_id": self.job_id}, profile="kimi"
            )
        result = payload["result"]
        self.assertIn("output_truncated: true", payload["text"])
        self.assertIn("HEAD-★", payload["text"])
        self.assertIn("TAIL-STDOUT", payload["text"])
        self.assertIn("ERR-TAIL", payload["text"])
        self.assertIn("<remote-dev preview:", payload["text"])
        self.assertIn("remote raw stdout", payload["text"])
        self.assertIn("local cached stdout", payload["text"])
        self.assertTrue(result["refs"]["remote_raw_stderr"].endswith("/stderr.log"))
        self.assertTrue(result["output_preview"]["truncated"])

    def test_native_output_does_not_load_large_cache_through_read_bytes(self) -> None:
        cache = Path(self.temp.name) / "bounded-cache.stdout.log"
        cache.write_bytes(("H" * 20_000 + "\nTAIL-STDOUT\n").encode("utf-8"))
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole cache read")):
            text, truncated = _tail_text(cache, 4096)
        self.assertTrue(truncated)
        self.assertTrue(text.endswith("TAIL-STDOUT\n"))
        self.assertLessEqual(len(text.encode()), 4096)


class NativeBashRefsTests(unittest.TestCase):
    def test_codex_rewritten_text_keeps_refs(self) -> None:
        from remote_dev.mcp.native_handlers_codex import codex_exec

        payload = {
            "text": "ignored",
            "result": {
                "outcome": "success",
                "state": "succeeded",
                "exit_code": 0,
                "duration_ms": 5,
                "session_id": "123",
                "preview": {"stdout": "out\n", "stderr": ""},
                "refs": {
                    "remote_raw_stdout": "/srv/app/.remote-dev/jobs/job-1/stdout.log",
                    "local_cached_stdout": "/tmp/job-1.stdout.log",
                    "local_cache_streams": {"stdout": {"status": "complete", "cached_bytes": 4}},
                },
            },
        }
        spec = SimpleNamespace(wire="exec_command")
        result = codex_exec("codex", spec, {"cmd": "true"}, {"cmd": "true"}, lambda name, args: payload)
        self.assertIn("Final output:", result["text"])
        self.assertIn("remote raw stdout", result["text"])
        self.assertIn("local cached stdout", result["text"])
        self.assertEqual(result["result"]["refs"]["remote_raw_stdout"], "/srv/app/.remote-dev/jobs/job-1/stdout.log")


if __name__ == "__main__":
    unittest.main()
