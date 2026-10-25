from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.core.preview import MAX_TEXT_CHARS  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402
import remote_dev.mcp.tools as mcp_tools  # noqa: E402


class RemoteReadTests(unittest.TestCase):
    def test_remote_read_writes_ledger(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                file_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "status": "ok",
                    "file": {
                        "path": "/vllm-workspace/foo.py",
                        "sha256": "abc",
                        "size": 3,
                        "mtime_ns": 1,
                        "offset": 1,
                        "limit": 200,
                        "line_start": 1,
                        "line_end": 1,
                        "total_lines": 1,
                        "partial": False,
                        "content": "1 | abc",
                    },
                }
                payload = file_ops.remote_read(endpoint, file_path="/vllm-workspace/foo.py")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertTrue(Path(payload["result"]["refs"]["read_ledger"]).exists())
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_remote_read_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = file_ops.remote_read(endpoint, file_path="/etc/passwd")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_remote_read_clamps_excessive_limit_and_text(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        captured = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]

                def fake_run_remote_python(_endpoint, _script, payload, **_kwargs):
                    captured.update(payload)
                    return {
                        "status": "partial",
                        "file": {
                            "path": "/vllm-workspace/big.log",
                            "sha256": "abc",
                            "size": 20000,
                            "mtime_ns": 1,
                            "offset": 1,
                            "limit": payload["limit"],
                            "line_start": 1,
                            "line_end": payload["limit"],
                            "total_lines": 100000,
                            "partial": True,
                            "content": "1 | " + ("x" * 20000),
                        },
                    }

                file_ops.run_remote_python = fake_run_remote_python  # type: ignore[assignment]
                payload = file_ops.remote_read(endpoint, file_path="/vllm-workspace/big.log", limit=2000000)
                self.assertEqual(captured["limit"], file_ops.MAX_READ_LINES)
                self.assertIn("clamped", payload["result"]["warnings"][0])
                self.assertLessEqual(len(payload["text"]), MAX_TEXT_CHARS)
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]


sys.path.insert(0, str(Path(__file__).parent))
from _worker_runner import run_worker_script


class KimiReadPaginationTests(unittest.TestCase):
    """Business-level pagination through the real worker and formatter.

    The only substitution is SSH transport: each worker call is a real
    ``python3 -I`` execution of the production ``remote_file_worker.py``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self._state_root = state_store.substrate_root
        self._runner = file_ops.run_remote_python
        state_store.substrate_root = lambda: self.root / "state"
        self.addCleanup(setattr, state_store, "substrate_root", self._state_root)

        def run_worker(_endpoint, script, payload, **_kwargs):
            proc = run_worker_script(
                script, payload, cwd=self.root, isolated=True,
            )
            if proc.returncode != 0:
                raise AssertionError(f"worker failed: {proc.stderr}")
            return json.loads(proc.stdout.strip().splitlines()[-1])

        file_ops.run_remote_python = run_worker
        self.addCleanup(setattr, file_ops, "run_remote_python", self._runner)
        self.endpoint = Endpoint(host="local", port=22, root=str(self.root), cwd=str(self.root))

    @staticmethod
    def _js_units(text: str) -> int:
        return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)

    def _read(self, file_path: str, **extra) -> dict:
        return file_ops.remote_read(self.endpoint, file_path=file_path, **extra)

    def _page_body(self, text: str) -> str:
        body = text.split("\n\n", 1)[1] if "\n\n" in text else ""
        body = body.split("\nNext Read:", 1)[0]
        return body.rstrip("\n")

    def _follow(self, file_path: str, source_lines: list[str], max_chars: int, *, utf16_units: bool = True) -> int:
        parts: dict[int, str] = {}
        kwargs = {
            "offset": 1,
            "limit": 1_000_000,
            "max_chars": max_chars,
            "utf16_units": utf16_units,
        }
        pages = 0
        while True:
            payload = self._read(file_path, **kwargs)
            self.assertNotEqual(payload["result"].get("status"), "max_chars_too_small", payload.get("text"))
            self.assertLessEqual(self._js_units(payload["text"]), max_chars, payload["text"])
            self.assertFalse(any(0xD800 <= ord(ch) <= 0xDFFF for ch in payload["text"]))
            body = self._page_body(payload["text"])
            for raw in body.split("\n") if body else []:
                match = re.match(r"^(\d+) \| (.*)$", raw)
                self.assertIsNotNone(match, raw)
                line_no = int(match.group(1))
                parts[line_no] = parts.get(line_no, "") + match.group(2)
            next_args = payload["result"].get("next")
            footer = re.search(r"Next Read: (\{.*\})", payload["text"])
            if next_args is None:
                self.assertIsNone(footer)
                break
            self.assertIsNotNone(footer)
            self.assertEqual(json.loads(footer.group(1)), next_args)
            file_info = payload["result"]["file"]
            self.assertEqual(file_info.get("next_offset"), next_args["line_offset"])
            self.assertEqual(file_info.get("next_column_offset"), next_args["column_offset"])
            kwargs = {
                "offset": next_args["line_offset"],
                "column_offset": next_args["column_offset"],
                "limit": 1_000_000,
                "max_chars": max_chars,
                "utf16_units": utf16_units,
            }
            pages += 1
            self.assertLess(pages, 30, "pagination did not terminate")
            self.assertEqual(payload["result"]["status"], "partial")
        reconstructed = "\n".join(parts[index] for index in sorted(parts))
        self.assertEqual(reconstructed, "\n".join(source_lines))
        return pages + 1

    def test_long_first_line_reconstructs_and_terminates(self) -> None:
        source = "x" * 1000
        (self.root / "a.txt").write_text(source + "\n", encoding="utf-8")
        payload = self._read("a.txt", offset=1, limit=1, max_chars=500, utf16_units=True)
        self.assertLessEqual(self._js_units(payload["text"]), 500)
        self.assertEqual(payload["result"]["file"]["next_column_offset"], len(self._page_body(payload["text"]).split(" | ", 1)[1]))
        pages = self._follow("a.txt", [source], 500)
        self.assertGreaterEqual(pages, 3)

    def test_multiline_emoji_boundary_reconstructs_exactly(self) -> None:
        lines = [
            "A" + "😀" * 160 + "B",
            "second 😀😀 line",
            "third line",
            "fourth " + "y" * 60,
        ]
        (self.root / "emoji.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for budget in (360, 380, 400, 420):
            with self.subTest(max_chars=budget):
                pages = self._follow("emoji.txt", lines, budget)
                self.assertGreaterEqual(pages, 1)
                payload = self._read("emoji.txt", offset=1, limit=1_000_000, max_chars=budget, utf16_units=True)
                self.assertFalse(any(0xD800 <= ord(ch) <= 0xDFFF for ch in payload["text"]))

    def test_utf16_file_paginates_with_the_same_contract(self) -> None:
        lines = ["first 😀😀 line", "second line", "third " + "z" * 400]
        (self.root / "u.txt").write_text("\n".join(lines) + "\n", encoding="utf-16")
        self._follow("u.txt", lines, 420, utf16_units=True)

    def test_forward_explicit_n_lines_continuation_keeps_the_range(self) -> None:
        source = "x" * 1000
        (self.root / "bounded.txt").write_text(source + "\nOUTSIDE_REQUEST\n", encoding="utf-8")
        endpoint_args = {
            "host": "local", "port": 22, "root": str(self.root), "cwd": str(self.root),
        }
        args = {
            **endpoint_args, "path": "bounded.txt", "n_lines": 1, "max_chars": 500,
        }
        seen_lines: set[int] = set()
        first_chars = ""
        pages = 0
        while True:
            payload = mcp_tools.call_tool("remote_Read", args, profile="kimi")
            body = self._page_body(payload["text"])
            for raw in body.split("\n") if body else []:
                match = re.match(r"^(\d+) \| (.*)$", raw)
                self.assertIsNotNone(match, raw)
                line_no = int(match.group(1))
                seen_lines.add(line_no)
                if line_no == 1:
                    first_chars += match.group(2)
                else:
                    self.fail(f"continuation escaped the requested n_lines=1 range: {raw}")
            next_args = payload["result"].get("next")
            footer = re.search(r"Next Read: (\{.*\})", payload["text"])
            if next_args is None:
                self.assertIsNone(footer)
                break
            self.assertIsNotNone(footer)
            self.assertEqual(json.loads(footer.group(1)), next_args)
            self.assertEqual(next_args.get("n_lines"), 1)
            self.assertEqual(payload["result"]["file"].get("next_offset"), next_args["line_offset"])
            self.assertEqual(payload["result"]["file"].get("next_column_offset"), next_args["column_offset"])
            args = {
                **endpoint_args,
                "path": "bounded.txt",
                "n_lines": next_args["n_lines"],
                "line_offset": next_args["line_offset"],
                "column_offset": next_args["column_offset"],
                "max_chars": 500,
            }
            pages += 1
            self.assertLess(pages, 20)
        self.assertEqual(seen_lines, {1})
        self.assertEqual(first_chars, source)
        self.assertNotIn("OUTSIDE_REQUEST", payload["text"])

    def test_negative_offset_reads_the_tail(self) -> None:
        lines = [f"line-{index}" for index in range(1, 11)]
        (self.root / "tail.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = self._read("tail.txt", offset=-3, limit=1_000_000, max_chars=500, utf16_units=True)
        self.assertEqual(payload["result"]["file"]["line_start"], 8)
        self.assertEqual(payload["result"]["file"]["line_end"], 10)
        body = self._page_body(payload["text"])
        self.assertEqual(body, "8 | line-8\n9 | line-9\n10 | line-10")

    def test_empty_file_tail_is_ok_not_a_budget_error(self) -> None:
        (self.root / "empty.txt").write_text("", encoding="utf-8")
        payload = self._read("empty.txt", offset=-1, limit=10, max_chars=500, utf16_units=True)
        self.assertEqual(payload["result"]["status"], "ok")
        self.assertEqual(payload["result"]["file"]["line_start"], 0)
        self.assertEqual(payload["result"]["file"]["line_end"], 0)
        self.assertIsNone(payload["result"].get("next"))

    def test_negative_tail_budget_keeps_trailing_whole_lines(self) -> None:
        lines = [f"{index}-" + "a" * 295 for index in range(1, 6)]
        (self.root / "tailbudget.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = self._read("tailbudget.txt", offset=-3, limit=1_000_000, max_chars=800, utf16_units=True)
        self.assertEqual(payload["result"]["file"]["line_start"], 5)
        self.assertEqual(payload["result"]["file"]["line_end"], 5)
        body = self._page_body(payload["text"])
        self.assertTrue(body.startswith("5 | 5-"))
        self.assertNotIn("4 | 4-", body)
        next_args = payload["result"]["next"]
        self.assertEqual(next_args["line_offset"], 3)
        self.assertEqual(next_args["column_offset"], 0)
        self.assertEqual(next_args["n_lines"], 2)
        earlier = self._read(
            "tailbudget.txt", offset=3, limit=next_args["n_lines"],
            column_offset=0, max_chars=800, utf16_units=True,
        )
        self.assertEqual(earlier["result"]["file"]["line_start"], 3)
        self.assertEqual(earlier["result"]["file"]["line_end"], 4)
        self.assertIn("4 | 4-", earlier["text"])

    def test_small_max_chars_is_an_explicit_error(self) -> None:
        (self.root / "small.txt").write_text("hello\n", encoding="utf-8")
        payload = self._read("small.txt", offset=1, limit=1, max_chars=80, utf16_units=True)
        self.assertEqual(payload["result"]["outcome"], "needs_input")
        self.assertEqual(payload["result"]["status"], "max_chars_too_small")
        self.assertIn("too small", payload["text"])
        self.assertIsNone(payload["result"].get("next"))
        self.assertNotIn("next_offset", payload["result"]["file"])

    def test_large_line_output_is_bounded_and_continues(self) -> None:
        source = "z" * 1_200_000
        (self.root / "huge.txt").write_text(source + "\n", encoding="utf-8")
        pages = self._follow("huge.txt", [source], 500_000)
        self.assertGreaterEqual(pages, 2)

    def test_kimi_profile_call_tool_follows_the_real_continuation(self) -> None:
        source = "x" * 1000
        (self.root / "profile.txt").write_text(source + "\n", encoding="utf-8")
        endpoint_args = {
            "host": "local", "port": 22, "root": str(self.root), "cwd": str(self.root),
        }
        parts: list[str] = []
        args = {**endpoint_args, "path": "profile.txt", "max_chars": 500}
        for _ in range(10):
            payload = mcp_tools.call_tool("remote_Read", args, profile="kimi")
            self.assertLessEqual(self._js_units(payload["text"]), 500)
            body = self._page_body(payload["text"]).split(" | ", 1)[1]
            parts.append(body)
            next_args = payload["result"].get("next")
            if next_args is None:
                break
            args = {
                **endpoint_args,
                "path": "profile.txt",
                "max_chars": 500,
                "line_offset": next_args["line_offset"],
                "column_offset": next_args["column_offset"],
            }
        self.assertEqual("".join(parts), source)

    def test_native_read_caps_max_chars_at_the_runtime_limit(self) -> None:
        endpoint_args = {"host": "example.invalid", "port": 22, "path": "x.txt"}
        with mock.patch.object(mcp_tools, "remote_read", return_value={"text": "", "result": {}}) as execute:
            mcp_tools.call_tool("remote_Read", {**endpoint_args, "max_chars": 600000}, profile="kimi")
        self.assertEqual(execute.call_args.kwargs["max_chars"], 500000)
        with mock.patch.object(mcp_tools, "remote_read", return_value={"text": "", "result": {}}) as execute:
            mcp_tools.call_tool("remote_Read", endpoint_args, profile="kimi")
        self.assertEqual(execute.call_args.kwargs["max_chars"], 100000)


if __name__ == "__main__":
    unittest.main()
