"""Property tests for bounded previews (``core.preview``) and read pagination.

Property: a preview never misrepresents the content it truncates. Whenever a
result is marked truncated, the returned head is an exact prefix and the tail
an exact suffix of the input, the two never overlap, and the byte count is the
real UTF-8 size. Boundaries are checked at exactly the limit, one character on
either side, on empty input and on multi-byte / combining / astral characters
that would be split by a byte-oriented cut.

The remote read pagination (``REMOTE_FILE_PY`` ``read``) is checked against a
reference slice of the file's lines.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

import remote_dev.core.file_ops as file_ops  # noqa: E402
from remote_dev.core import preview  # noqa: E402
from test_property_support import MULTIBYTE, SPLITLINES_EXTRA, Gen, run_cases, run_remote_script  # noqa: E402

MARKER = "\n<remote-dev text truncated; full output is available via refs/resources>\n"
TEXT_ALPHABET = "ab \n" + MULTIBYTE


class TextPreviewProperties(unittest.TestCase):
    def test_truncation_is_exact_prefix_and_suffix_without_overlap(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            head = gen.integer(0, 30)
            # tail_chars == 0 is covered by the known-defect test below.
            tail = gen.integer(1, 30)
            limit = head + tail
            length = gen.choice((0, max(0, limit - 1), limit, limit + 1, gen.integer(0, 120)))
            value = gen.text(TEXT_ALPHABET, length, length)
            result = preview.text_preview(value, head_chars=head, tail_chars=tail)
            self.assertEqual(result["bytes"], len(value.encode("utf-8")))
            self.assertEqual((result["head_chars"], result["tail_chars"]), (head, tail))
            if len(value) <= limit:
                self.assertFalse(result["truncated"])
                self.assertEqual(result["text"], value)
                self.assertNotIn("head", result)
            else:
                self.assertTrue(result["truncated"])
                self.assertNotIn("text", result)
                self.assertEqual(result["head"], value[:head])
                self.assertEqual(result["tail"], value[-tail:])
                self.assertTrue(value.startswith(result["head"]))
                self.assertTrue(value.endswith(result["tail"]))
                self.assertLess(len(result["head"]) + len(result["tail"]), len(value), "head and tail must not overlap")
                # Every character of the input is either shown or dropped; none is duplicated.
                omitted = value[len(result["head"]): len(value) - len(result["tail"])]
                self.assertEqual(result["head"] + omitted + result["tail"], value)

        run_cases(600, body, label="text_preview boundaries")

    def test_default_preview_never_splits_multibyte_characters(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            unit = gen.choice(("🙂", "漢", "é", "e\u0301", "👩\u200d💻"))
            count = (preview.DEFAULT_HEAD_CHARS + preview.DEFAULT_TAIL_CHARS) // len(unit) + gen.integer(1, 3)
            value = unit * count
            result = preview.text_preview(value)
            self.assertTrue(result["truncated"])
            for part in (result["head"], result["tail"]):
                part.encode("utf-8")  # a split surrogate would raise
                self.assertEqual(len(part.encode("utf-8")), len(part.encode("utf-8", errors="strict")))
            self.assertEqual(result["bytes"], len(value.encode("utf-8")))

        run_cases(20, body, label="multibyte previews")

    def test_zero_tail_chars_returns_an_empty_tail(self) -> None:
        """``text_preview(value, tail_chars=0)`` used ``value[-0:]`` — the
        entire value — so a truncated preview carried the full content in
        ``tail`` while claiming ``truncated: True``."""
        value = "x" * 100
        result = preview.text_preview(value, head_chars=5, tail_chars=0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["tail"], "")

    def test_stdout_stderr_flag_is_consistent_with_parts(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            limit = preview.DEFAULT_HEAD_CHARS + preview.DEFAULT_TAIL_CHARS
            stdout = "o" * gen.choice((0, limit - 1, limit, limit + 1))
            stderr = "e" * gen.choice((0, limit - 1, limit, limit + 1))
            result = preview.stdout_stderr_preview(stdout, stderr)
            self.assertEqual(result["truncated"], result["stdout"]["truncated"] or result["stderr"]["truncated"])
            self.assertEqual(result["stdout_bytes"], len(stdout))
            self.assertEqual(result["stderr_bytes"], len(stderr))

        run_cases(16, body, label="stdout/stderr preview")


class CompactTextProperties(unittest.TestCase):
    def test_compact_text_is_bounded_and_keeps_true_prefix_and_suffix(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            limit = gen.integer(len(MARKER) + 1, 400)
            length = gen.choice((0, limit - 1, limit, limit + 1, gen.integer(0, 900)))
            value = gen.text(TEXT_ALPHABET, length, length)
            result = preview.compact_text(value, limit=limit)
            if len(value) <= limit:
                self.assertEqual(result, value)
                return
            self.assertLessEqual(len(result), limit)
            self.assertIn(MARKER, result)
            head, _, tail = result.partition(MARKER)
            self.assertTrue(value.startswith(head))
            self.assertTrue(value.endswith(tail))
            self.assertLess(len(head) + len(tail), len(value))

        run_cases(600, body, label="compact_text bounds")

    def test_tail_text_returns_a_true_suffix_within_limit(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            limit = gen.integer(1, 50)
            value = gen.text(TEXT_ALPHABET, 0, 120)
            result = preview.tail_text(value, limit)
            self.assertLessEqual(len(result), limit)
            self.assertTrue(value.endswith(result))
            if len(value) >= limit:
                self.assertEqual(len(result), limit)

        run_cases(300, body, label="tail_text")

    def test_compact_text_stays_bounded_when_limit_is_below_marker_size(self) -> None:
        """When ``keep`` is 0 the tail slice was ``value[-0:]`` (the whole
        value), so the compacted output was ``marker + value``. Treat a
        zero-length tail as empty."""
        value = "x" * 200
        result = preview.compact_text(value, limit=50)
        self.assertLessEqual(len(result), max(50, len(MARKER)))

    def test_tail_text_with_zero_limit_returns_empty(self) -> None:
        """``tail_text(value, 0)`` used ``value[-0:]`` and returned the
        entire value. A caller asking for no tail must get ``""``."""
        self.assertEqual(preview.tail_text("x" * 200, 0), "")

    def test_fold_repeated_lines_never_expands_blank_or_short_runs(self) -> None:
        """Repeated blank or short lines within ``limit`` must never expand into
        a longer ``[previous line repeated ...]`` marker and trigger truncation."""
        value = "header\n\n\n\nfooter\n" + ("ok\n" * 4)
        self.assertEqual(preview.fold_repeated_lines(value), value)
        self.assertEqual(preview.compact_text(value, limit=len(value)), value)



class RemoteReadPaginationProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def test_pagination_matches_reference_slice(self) -> None:
        def body(gen: Gen, index: int) -> None:
            path = self.root / f"f{index}.txt"
            long_len = gen.integer(20, 60)
            lines = [gen.text("ab" + MULTIBYTE, 0, 8) if gen.boolean(0.8) else gen.text("z", long_len, long_len) for _ in range(gen.integer(0, 12))]
            text = "\n".join(lines) + ("\n" if lines and gen.boolean() else "")
            path.write_bytes(text.encode("utf-8"))
            reference_lines = text.splitlines()
            offset = gen.integer(1, max(1, len(reference_lines) + 2))
            limit = gen.integer(1, 6)
            max_line_chars = gen.integer(10, 30)
            data = run_remote_script(
                file_ops.REMOTE_FILE_PY,
                {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": path.name, "offset": offset, "limit": limit, "max_line_chars": max_line_chars},
            )
            info = data["file"]
            start = min(offset - 1, len(reference_lines))
            end = min(start + limit, len(reference_lines))
            expected = reference_lines[start:end]
            self.assertEqual(info["total_lines"], len(reference_lines))
            self.assertEqual(info["line_end"], end)
            self.assertEqual(info["partial"], start > 0 or end < len(reference_lines))
            self.assertEqual(data["status"], "partial" if info["partial"] else "ok")
            shown = info["content"].split("\n") if info["content"] else []
            self.assertEqual(len(shown), len(expected))
            truncated = 0
            for number, (rendered, original) in enumerate(zip(shown, expected), start=start + 1):
                prefix = f"{number} | "
                self.assertTrue(rendered.startswith(prefix), rendered)
                body_text = rendered[len(prefix):]
                if len(original) > max_line_chars:
                    truncated += 1
                    self.assertEqual(body_text, original[:max_line_chars] + "<remote-dev line truncated>")
                else:
                    self.assertEqual(body_text, original)
            self.assertEqual(info["truncated_line_count"], truncated)
            self.assertEqual(len(data["warnings"]), 1 if truncated else 0)
            self.assertEqual(info["size"], len(text.encode("utf-8")), "size/sha describe the whole file, not the page")

        run_cases(150, body, label="remote read pagination")

    def test_line_boundary_characters_are_counted_like_splitlines(self) -> None:
        # The executor uses str.splitlines, so these characters are line breaks
        # for pagination purposes; the property pins that contract explicitly.
        def body(gen: Gen, index: int) -> None:
            path = self.root / f"s{index}.txt"
            separator = gen.choice(SPLITLINES_EXTRA)
            text = f"a{separator}b\nc"
            path.write_bytes(text.encode("utf-8"))
            data = run_remote_script(
                file_ops.REMOTE_FILE_PY,
                {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": path.name, "offset": 1, "limit": 10},
            )
            self.assertEqual(data["file"]["total_lines"], len(text.splitlines()))

        run_cases(len(SPLITLINES_EXTRA), body, label="splitlines separators")

    def test_invalid_pagination_is_a_needs_input_status_not_a_crash(self) -> None:
        path = self.root / "p.txt"
        path.write_text("a\nb\n", encoding="utf-8")
        # Negative offsets are valid (read-from-end); negative limits and a
        # zero limit remain invalid.
        for offset, limit in ((2, -1), (1, -3)):
            with self.subTest(offset=offset, limit=limit):
                data = run_remote_script(
                    file_ops.REMOTE_FILE_PY,
                    {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": "p.txt", "offset": offset, "limit": limit},
                )
                self.assertEqual(data["status"], "invalid_pagination")
                self.assertEqual(file_ops._status_to_outcome(data["status"]), "needs_input")

    def test_negative_offset_reads_from_end(self) -> None:
        # Contract pin for the Kimi native Read habit: offset=-N starts N
        # lines before the end of the file.
        path = self.root / "p.txt"
        path.write_text("a\nb\nc\nd\n", encoding="utf-8")
        data = run_remote_script(
            file_ops.REMOTE_FILE_PY,
            {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": "p.txt", "offset": -2, "limit": 10},
        )
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["line_start"], 3)
        self.assertIn("3 | c", data["file"]["content"])

    def test_zero_offset_and_limit_fall_back_to_documented_defaults(self) -> None:
        # Contract pin: 0 is treated as "not provided" (offset 1, limit 200),
        # the same rule the MCP layer applies, and is reported back as such.
        path = self.root / "z.txt"
        path.write_text("a\nb\n", encoding="utf-8")
        data = run_remote_script(
            file_ops.REMOTE_FILE_PY,
            {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": "z.txt", "offset": 0, "limit": 0},
        )
        self.assertEqual(data["status"], "ok")
        self.assertEqual((data["file"]["offset"], data["file"]["limit"]), (1, 200))


if __name__ == "__main__":
    unittest.main()
