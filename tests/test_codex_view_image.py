"""codex view_image must report a missing media key as a tool failure."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from remote_dev.mcp.native_handlers_codex import codex_view_image


def _spec():
    return SimpleNamespace(wire="remote_view_image")


def _unused(*_args, **_kwargs):
    raise AssertionError("view_image must not call the canonical tool")


class CodexViewImageTests(unittest.TestCase):
    def test_flat_result_without_extra_sets_image_url(self) -> None:
        payload = {
            "text": "remote_view_image chart.png (image/jpeg, 8x8).\n",
            "result": {
                "outcome": "success",
                "status": "ok",
                "mime_type": "image/jpeg",
                "summary": "remote_view_image chart.png (image/jpeg, 8x8).",
            },
            "content": [{"type": "image", "data": "QkM=", "mimeType": "image/jpeg"}],
        }
        with mock.patch(
            "remote_dev.mcp.native_handlers_codex._read_media_payload",
            return_value=payload,
        ) as read:
            out = codex_view_image("codex", _spec(), {"detail": "original"}, {"file_path": "chart.png"}, _unused)
        read.assert_called_once()
        self.assertIsNone(read.call_args.kwargs["max_edge"])
        self.assertEqual(out["result"]["outcome"], "success")
        self.assertEqual(out["result"]["image_url"], "data:image/jpeg;base64,QkM=")
        self.assertEqual(out["result"]["detail"], "original")
        self.assertNotIn("extra", out["result"])

    def test_missing_result_is_tool_error(self) -> None:
        payload = {"text": "partial\n", "content": [{"type": "image", "data": "AAAA"}]}
        with mock.patch("remote_dev.mcp.native_handlers_codex._read_media_payload", return_value=payload):
            out = codex_view_image("codex", _spec(), {}, {"file_path": "chart.png"}, _unused)
        self.assertEqual(out["result"]["outcome"], "failed")
        self.assertEqual(out["result"]["status"], "invalid_media")
        self.assertIn("missing 'result'", out["text"])
        self.assertIn("missing 'result'", out["result"]["error"])
        self.assertNotIn("image_url", out["result"])

    def test_missing_data_is_tool_error(self) -> None:
        payload = {
            "text": "partial\n",
            "result": {"outcome": "success", "status": "ok", "mime_type": "image/png"},
            "content": [{"type": "image", "mimeType": "image/png"}],
        }
        with mock.patch("remote_dev.mcp.native_handlers_codex._read_media_payload", return_value=payload):
            out = codex_view_image("codex", _spec(), {"detail": "low"}, {"file_path": "chart.png"}, _unused)
        self.assertEqual(out["result"]["outcome"], "failed")
        self.assertEqual(out["result"]["status"], "invalid_media")
        self.assertIn("missing 'data'", out["text"])
        self.assertIn("missing 'data'", out["result"]["error"])
        self.assertEqual(out["result"]["tool"], "remote_view_image")

    def test_read_failure_without_content_is_unchanged(self) -> None:
        payload = {
            "text": "remote_view_image: not_found: missing file\n",
            "result": {"outcome": "failed", "status": "not_found", "error": "missing file"},
        }
        with mock.patch("remote_dev.mcp.native_handlers_codex._read_media_payload", return_value=payload):
            out = codex_view_image("codex", _spec(), {}, {"file_path": "missing.png"}, _unused)
        self.assertIs(out, payload)


if __name__ == "__main__":
    unittest.main()
