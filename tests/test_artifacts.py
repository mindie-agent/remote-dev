from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from remote_dev.core.endpoint import Endpoint  # noqa: E402
import remote_dev.core.artifact_ops as artifact_ops  # noqa: E402
import remote_dev.core.state_store as state_store  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def test_artifact_manifest_path_escape_returns_blocked_result(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        payload = artifact_ops.remote_artifact_manifest(endpoint, remote_path="/etc/passwd")
        self.assertEqual(payload["result"]["outcome"], "blocked")
        self.assertEqual(payload["result"]["status"], "path_outside_root")

    def test_artifact_manifest_persists_local_manifest_ref(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_state_root = state_store.substrate_root
        original_runner = artifact_ops.run_remote_python
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_store.substrate_root = lambda: Path(tmp)  # type: ignore[assignment]
                artifact_ops.run_remote_python = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                    "schema_version": "remote-dev.artifact_manifest.v1",
                    "status": "ok",
                    "root": "/vllm-workspace/out",
                    "is_dir": False,
                    "file_count": 1,
                    "total_bytes": 7,
                    "files": [],
                }
                payload = artifact_ops.remote_artifact_manifest(endpoint, remote_path="/vllm-workspace/out")
                manifest_ref = payload["result"]["refs"]["local_manifest"]
                self.assertTrue(Path(manifest_ref).exists())
                self.assertEqual(payload["result"]["artifacts"][0]["endpoint_id"], endpoint.endpoint_id)
                self.assertIn("artifact_id", payload["result"]["artifacts"][0])
        finally:
            state_store.substrate_root = original_state_root  # type: ignore[assignment]
            artifact_ops.run_remote_python = original_runner  # type: ignore[assignment]

    def test_artifact_push_rejects_local_symlink(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.txt"
            (target).write_bytes(("secret\n").encode("utf-8"))
            link = Path(tmp) / "link.txt"
            link.symlink_to(target)
            payload = artifact_ops.remote_artifact_push(
                endpoint,
                local_path=str(link),
                remote_path="/vllm-workspace/out/link.txt",
            )
            self.assertEqual(payload["result"]["outcome"], "blocked")
            self.assertEqual(payload["result"]["status"], "symlink_not_allowed")

    def test_artifact_push_passes_paths_and_hashes_without_loading_file_bytes(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=22)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "artifact.txt"
            local.write_bytes(b"payload\n")
            expected = artifact_ops._sha256_file(local)
            with mock.patch.object(artifact_ops, "ArtifactStream") as factory:
                stream = factory.return_value.__enter__.return_value
                stream.push.return_value = expected
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file reads forbidden")):
                    payload = artifact_ops.remote_artifact_push(endpoint, local_path=str(local), remote_path="/srv/artifact.txt")
            self.assertEqual(payload["result"]["outcome"], "success")
            item, path = stream.push.call_args.args
            self.assertEqual(path, local.resolve())
            self.assertEqual(item["sha256"], expected)
            self.assertEqual(item["path"], "/srv/artifact.txt")
            factory.assert_called_once_with(endpoint, "push", 1, None)

    def test_artifact_pull_blocks_malicious_relpath(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        original_manifest = artifact_ops.remote_artifact_manifest
        try:
            artifact_ops.remote_artifact_manifest = lambda *_args, **_kwargs: {  # type: ignore[assignment]
                "text": "",
                "result": {
                    "manifest": {
                        "status": "ok",
                        "files": [{
                            "relpath": "../escape.txt",
                            "path": "/vllm-workspace/out/file.txt",
                            "sha256": "0" * 64,
                            "size": 1,
                        }],
                    }
                },
            }
            with tempfile.TemporaryDirectory() as tmp:
                payload = artifact_ops.remote_artifact_pull(
                    endpoint,
                    remote_path="/vllm-workspace/out",
                    local_dir=tmp,
                )
            self.assertEqual(payload["result"]["outcome"], "blocked")
            self.assertEqual(payload["result"]["status"], "path_traversal")
        finally:
            artifact_ops.remote_artifact_manifest = original_manifest  # type: ignore[assignment]

    def test_artifact_pull_conflict_blocks_before_opening_a_stream(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=22)
        incoming = b"new content"
        manifest = {
            "status": "ok", "is_dir": False,
            "files": [{"relpath": ".", "path": "/remote/new.txt", "size": len(incoming),
                       "sha256": hashlib.sha256(incoming).hexdigest()}],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, REMOTE_DEV_STATE_DIR=tmp):
            destination = Path(tmp) / "download"
            destination.mkdir()
            (destination / "artifact").write_bytes(b"existing content")
            receipt = {"result": {"manifest": manifest, "refs": {"local_manifest": str(Path(tmp) / "receipt.json")}}}
            with mock.patch.object(artifact_ops, "remote_artifact_manifest", return_value=receipt), \
                 mock.patch.object(artifact_ops, "ArtifactStream") as stream:
                result = artifact_ops.remote_artifact_pull(endpoint, remote_path="/remote/new.txt", local_dir=str(destination))["result"]
            stream.assert_not_called()
            self.assertEqual((result["outcome"], result["status"]), ("blocked", "destination_exists"))
            self.assertEqual(result["conflicts"], [str(destination / "artifact")])
            self.assertEqual((destination / "artifact").read_bytes(), b"existing content")


if __name__ == "__main__":
    unittest.main()
