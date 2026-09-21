from __future__ import annotations

import unittest
from unittest.mock import patch

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.search_ops import remote_grep
from remote_dev.mcp import server


class McpErrorTextTests(unittest.TestCase):
    def test_sparse_failure_reaches_text_only_client_with_recovery_detail(self) -> None:
        detail = "grep fallback cannot honor --type py; install ripgrep (rg) or use --glob"
        payload = {"text": "\n", "result": {"outcome": "failed", "status": "rg_required", "error": detail}}
        with patch.object(server, "call_tool", return_value=payload), \
                patch.object(server, "runtime_status", return_value={}), patch.object(server, "send") as send:
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "remote_grep"}})
        result = send.call_args.args[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("rg_required", result["content"][0]["text"])
        self.assertIn(detail, result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["error"], detail)

    def test_failed_search_is_not_reported_as_zero_matches(self) -> None:
        detail = "grep fallback cannot honor --type py; install ripgrep (rg) or use --glob"
        with patch("remote_dev.core.search_ops.run_remote_python", return_value={"status": "rg_required", "error": detail}):
            payload = remote_grep(Endpoint(host="example.invalid", port=22), pattern="timeout", type="py")
        self.assertNotIn("found 0 matches", payload["result"]["summary"])
        self.assertIn(detail, payload["text"])
        self.assertEqual(server.tool_text(payload).count(detail), 1)

    def test_success_and_cancelled_text_remain_unchanged(self) -> None:
        for outcome in ("success", "cancelled"):
            self.assertEqual(server.tool_text({"text": "bounded output\n", "result": {"outcome": outcome}}), "bounded output\n")

    def test_failure_text_includes_job_id_category_and_diagnostic_without_private_fields(self) -> None:
        from remote_dev.result import tool_text as result_tool_text

        payload = {
            "text": "failed\n",
            "result": {
                "outcome": "failed",
                "status": "failed",
                "summary": "Remote job job-abc status failed.",
                "error": "ssh: connect to host example port 22: Connection refused",
                "job_id": "job-abc",
                "error_details": {"category": "remote_execution", "submission_state": "not_sent"},
                "diagnostics": {"operation_id": "op-123", "phases": [{"name": "status"}]},
                "job": {"job_id": "job-abc", "error": "ssh: connect to host example port 22: Connection refused"},
            },
        }
        text = server.tool_text(payload)
        self.assertEqual(text, result_tool_text(payload))
        self.assertIn("job-abc", text)
        self.assertIn("not_sent", text)
        self.assertIn("Connection refused", text)
        self.assertIn("operation_id=op-123", text)
        self.assertIn("phase=status", text)
        self.assertEqual(text.count("Remote job job-abc status failed."), 1)
        self.assertNotIn("authorization", text)
        again = server.tool_text({"text": text, "result": payload["result"]})
        self.assertEqual(again, text)

    def test_successful_command_output_is_not_reformatted(self) -> None:
        body = "Remote command running.\n__STDOUT__\n" + ("ok" * 2000)
        self.assertEqual(server.tool_text({"text": body, "result": {"outcome": "success", "summary": "Remote command running."}}), body)

    def test_nonzero_command_output_keeps_its_existing_cursor_budget(self) -> None:
        body = "Remote command failed. Exit code: 2.\n__STDERR__\n" + "x" * 16000
        for tool in ("remote.bash", "remote.job_stdin"):
            self.assertEqual(server.tool_text({"text": body, "result": {
                "tool": tool, "outcome": "failed", "state": "failed", "exit_code": 2,
            }}), body)

    def test_preformatted_failure_is_bounded_and_accepts_late_phase(self) -> None:
        from remote_dev.result import MAX_FAILURE_TEXT_CHARS
        body = "Remote tool failed (failed).\n" + "x" * 12000
        details = {"outcome": "failed", "diagnostics": {
            "operation_id": "op-123", "phases": [{"phase": "rpc.control", "status": "error"}],
        }}
        text = server.tool_text({"text": body, "result": details})
        self.assertLessEqual(len(text), MAX_FAILURE_TEXT_CHARS)
        self.assertIn("operation_id=op-123", text)
        self.assertIn("phase=rpc.control", text)
        self.assertEqual(server.tool_text({"text": text, "result": details}), text)
