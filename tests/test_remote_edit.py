from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
import remote_dev.core.file_ops as file_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402


class RemoteEditTests(unittest.TestCase):
    def test_remote_edit_without_read_ledger_executes_by_default(self) -> None:
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
                        "status": "edited",
                        "file": {"path": "/vllm-workspace/foo.py", "sha256": "after", "size": 3, "mtime_ns": 2},
                        "before_sha256": "before",
                        "after_sha256": "after",
                        "diff_preview": "@@\n-a\n+b\n",
                    }

                file_ops.run_remote_python = fake_run_remote_python  # type: ignore[assignment]
                payload = file_ops.remote_edit(endpoint, file_path="/vllm-workspace/foo.py", old_string="a", new_string="b")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertIsNone(captured["expected_sha256"])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_remote_edit_with_ledger_returns_changed_file(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                state_store.write_read_ledger(endpoint, {"path": "/vllm-workspace/foo.py", "sha256": "before", "size": 3, "mtime_ns": 1})
                file_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "status": "edited",
                    "file": {"path": "/vllm-workspace/foo.py", "sha256": "after", "size": 3, "mtime_ns": 2},
                    "before_sha256": "before",
                    "after_sha256": "after",
                    "diff_preview": "@@\n-a\n+b\n",
                }
                payload = file_ops.remote_edit(endpoint, file_path="/vllm-workspace/foo.py", old_string="a", new_string="b")
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertEqual(payload["result"]["changed_files"][0]["after_sha256"], "after")
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_remote_edit_ignores_read_ledger_from_different_client_context(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = file_ops.run_remote_python
        captured = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                state_store.write_read_ledger(
                    endpoint,
                    {"path": "/vllm-workspace/foo.py", "sha256": "before", "size": 3, "mtime_ns": 1},
                    client_context_id="agent-a",
                )

                def fake_run_remote_python(_endpoint, _script, payload, **_kwargs):
                    captured.update(payload)
                    return {
                        "status": "edited",
                        "file": {"path": "/vllm-workspace/foo.py", "sha256": "after", "size": 3, "mtime_ns": 2},
                        "before_sha256": "before",
                        "after_sha256": "after",
                        "diff_preview": "@@\n-a\n+b\n",
                    }

                file_ops.run_remote_python = fake_run_remote_python  # type: ignore[assignment]
                payload = file_ops.remote_edit(
                    endpoint,
                    file_path="/vllm-workspace/foo.py",
                    old_string="a",
                    new_string="b",
                    client_context_id="agent-b",
                )
                self.assertEqual(payload["result"]["outcome"], "success")
                self.assertIsNone(captured["expected_sha256"])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            file_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_call_tool_edit_allows_empty_new_string_across_all_harness_aliases(self) -> None:
        import json
        import subprocess
        from unittest import mock
        import remote_dev.mcp.tools as mcp_tools

        def local_remote_python(_endpoint, script, payload, **_kwargs):
            # The client always addresses a POSIX remote; only this transport
            # fixture maps that workspace onto the current OS's scratch directory.
            local_payload = {
                **payload,
                "root": str(root),
                "cwd": str(root),
                "file_path": str(root / PurePosixPath(payload["file_path"]).relative_to("/workspace")),
            }
            proc = subprocess.run(
                [sys.executable, "-I", "-c", script],
                input=json.dumps(local_payload),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise AssertionError(f"remote script failed: {proc.stderr}\n{proc.stdout}")
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            for key in ("path", "resolved_path"):
                relative = Path(data["file"][key]).relative_to(root)
                data["file"][key] = str(PurePosixPath("/workspace") / relative.as_posix())
            return data

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            endpoint_args = {"host": "192.0.2.10", "port": 22, "root": "/workspace", "cwd": "/workspace"}
            cases = [
                (
                    "remote.edit",
                    lambda p: {"file_path": str(p), "old_string": "delete_me = 1\n", "new_string": ""},
                ),
                (
                    "remote_Edit",
                    lambda p: {"file_path": str(p), "old_string": "delete_me = 1\n", "new_string": ""},
                ),
                (
                    "remote_StrReplaceFile",
                    lambda p: {"path": str(p), "old_str": "delete_me = 1\n", "new_str": ""},
                ),
                (
                    "remote_replace_file_content",
                    lambda p: {
                        "TargetFile": str(p),
                        "TargetContent": "delete_me = 1\n",
                        "ReplacementContent": "",
                        "StartLine": 1,
                        "EndLine": 3,
                    },
                ),
            ]
            with mock.patch.object(state_store, "substrate_root", return_value=root / "state"), mock.patch.object(
                file_ops, "run_remote_python", side_effect=local_remote_python
            ), mock.patch.object(
                file_ops, "remote_stat", return_value={"status": "missing", "exists": False}
            ):
                for idx, (tool_name, make_args) in enumerate(cases):
                    with self.subTest(tool_name=tool_name):
                        target = root / f"sample_{idx}.py"
                        target.write_bytes(b"keep_top = 0\ndelete_me = 1\nkeep_bottom = 2\n")
                        remote_target = PurePosixPath("/workspace") / target.name
                        profile = "claude" if tool_name == "remote_Edit" else None
                        res = mcp_tools.call_tool(
                            tool_name, {**endpoint_args, **make_args(remote_target)}, profile=profile
                        )
                        self.assertEqual(res["result"]["outcome"], "success", res)
                        self.assertEqual(res["result"]["status"], "edited", res)
                        self.assertEqual(res["result"]["file"]["path"], str(remote_target))
                        self.assertEqual(target.read_text(encoding="utf-8"), "keep_top = 0\nkeep_bottom = 2\n")

    def test_call_tool_edit_still_rejects_missing_or_non_string_new_string(self) -> None:
        import remote_dev.mcp.tools as mcp_tools

        endpoint_args = {"host": "192.0.2.10", "port": 22, "file_path": "/tmp/a.py", "old_string": "a"}
        with self.assertRaisesRegex((KeyError, ValueError), "new_string"):
            mcp_tools.call_tool("remote.edit", endpoint_args)
        with self.assertRaisesRegex(ValueError, "new_string"):
            mcp_tools.call_tool("remote.edit", {**endpoint_args, "new_string": None})
        with self.assertRaisesRegex(ValueError, "new_string"):
            mcp_tools.call_tool("remote.edit", {**endpoint_args, "new_string": 123})


if __name__ == "__main__":
    unittest.main()
