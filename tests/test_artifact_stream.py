from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest import mock

from local_ssh import local_python_ssh
from remote_dev.core.artifact_transport import ArtifactStream, ArtifactTransferError
from remote_dev.core.artifact_ops import remote_artifact_pull
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class ArtifactStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "remote"
        self.root.mkdir()
        self.endpoint = Endpoint(host="192.0.2.10", port=22, root=str(self.root))
        patcher = mock.patch.dict(os.environ, REMOTE_DEV_STATE_DIR=str(self.base / "state"))
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = local_python_ssh()
        adapter.__enter__()
        self.addCleanup(adapter.__exit__, None, None, None)

    def test_multiple_binary_files_share_one_connection_and_roundtrip_unicode_paths(self):
        data = bytes(range(256)) * 16384 + b"\r\nUnix\n\x00"
        source = self.base / "源 数据.bin"
        source.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        items = [{"path": str(self.root / name), "size": len(data), "sha256": expected}
                 for name in ("子目录/数据.bin", "second.bin")]
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file buffering forbidden")):
            with ArtifactStream(self.endpoint, "push", 2, 10000) as stream:
                pid = stream.proc.pid
                for item in items:
                    self.assertEqual(stream.push(item, source), expected)
                    self.assertEqual(stream.proc.pid, pid)
            with ArtifactStream(self.endpoint, "pull", 2, 10000) as stream:
                for index, item in enumerate(items):
                    self.assertEqual(stream.pull(item, self.base / f"download-{index}"), expected)
        for index in range(2):
            self.assertEqual((self.base / f"download-{index}").read_bytes(), data)

    def test_empty_push_lost_ready_or_ack_reports_uncertain(self):
        import io
        from types import SimpleNamespace
        source = self.base / "empty"
        source.write_bytes(b"")
        expected = digest(source)
        for output in (b"", b'{"status":"ready"}\n'):
            with self.subTest(output=output):
                stream = object.__new__(ArtifactStream)
                stream.proc = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(output))
                stream.reason = ""
                stream.stderr = bytearray()
                with self.assertRaises(RemoteExecutionError) as failure:
                    stream.push({"path": str(self.root / "empty"), "size": 0, "sha256": expected}, source)
                self.assertEqual(failure.exception.submission_state, "uncertain")

    def test_default_has_no_deadline_and_explicit_cancel_stops_stream(self):
        import threading
        import time
        from remote_dev.core.cancellation import request_context
        cancelled = threading.Event()
        with request_context(cancelled), ArtifactStream(self.endpoint, "push", 1) as stream:
            self.assertIsNone(stream.deadline)
            self.assertIsNone(stream.proc.poll())
            cancelled.set()
            started = time.monotonic()
            with self.assertRaisesRegex(RemoteExecutionError, "cancelled"):
                stream.receive()
            self.assertLess(time.monotonic() - started, 2)

    def test_initial_send_failure_preserved_when_close_also_fails(self):
        from remote_dev.core.errors import error_details
        primary = BrokenPipeError("initial send fixture")
        close = ArtifactStream.close
        def failed_close(stream):
            close(stream)
            raise OSError("cleanup fixture")
        with mock.patch.object(ArtifactStream, "send", side_effect=primary), mock.patch.object(ArtifactStream, "close", failed_close):
            with self.assertRaises(BrokenPipeError) as caught:
                ArtifactStream(self.endpoint, "push", 1)
        self.assertIs(caught.exception, primary)
        self.assertIn("cleanup fixture", error_details(primary)["cleanup_error"])

    def test_negative_ack_is_acknowledged_and_preserves_hash_evidence(self):
        from remote_dev.core import artifact_ops
        source = self.base / "source"
        source.write_bytes(b"new bytes")
        destination = self.root / "destination"
        destination.write_bytes(b"existing bytes")
        manifest = artifact_ops._local_manifest(source)
        manifest["files"][0]["sha256"] = "0" * 64
        # The shipped remote worker targets POSIX paths. This client test
        # runs its byte protocol on the local OS, including Windows, so keep
        # path translation separate from the negative-ACK assertion.
        with mock.patch.object(artifact_ops, "_local_manifest", return_value=manifest), mock.patch.object(
                artifact_ops, "join_under_root", return_value=str(destination)):
            result = artifact_ops.remote_artifact_push(self.endpoint, local_path=str(source), remote_path=str(destination))["result"]
        self.assertEqual(result["status"], "hash_mismatch")
        self.assertEqual(result["error_details"]["submission_state"], "acknowledged")
        self.assertEqual(result["expected_sha256"], "0" * 64)
        self.assertEqual(result["observed_sha256"], digest(source))
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(destination.read_bytes(), b"existing bytes")

    def test_push_hash_failure_preserves_existing_file_and_cleans_temporary(self):
        source = self.base / "source"
        source.write_bytes(b"new bytes")
        destination = self.root / "destination"
        destination.write_bytes(b"existing bytes")
        item = {"path": str(destination), "size": source.stat().st_size, "sha256": "0" * 64}
        with ArtifactStream(self.endpoint, "push", 1, 5000) as stream:
            with self.assertRaises(ArtifactTransferError):
                stream.push(item, source)
        self.assertEqual(destination.read_bytes(), b"existing bytes")
        self.assertEqual(list(self.root.glob(".remote-dev-*")), [])

    def test_pull_hash_failure_preserves_existing_file_and_cleans_temporary(self):
        source = self.root / "source"
        source.write_bytes(b"server bytes")
        destination = self.base / "destination"
        destination.write_bytes(b"existing bytes")
        item = {"path": str(source), "size": source.stat().st_size, "sha256": "0" * 64}
        with ArtifactStream(self.endpoint, "pull", 1, 5000) as stream:
            with self.assertRaises(ArtifactTransferError):
                stream.pull(item, destination, overwrite=True)
        self.assertEqual(destination.read_bytes(), b"existing bytes")
        self.assertEqual(list(self.base.glob(".remote-dev-*")), [])

    def test_source_size_change_and_path_escape_never_replace_local_target(self):
        source = self.root / "source"
        source.write_bytes(b"server bytes")
        destination = self.base / "destination"
        destination.write_bytes(b"existing")
        for path, size in ((source, 999), (destination, 8)):
            with self.subTest(path=path), ArtifactStream(self.endpoint, "pull", 1, 5000) as stream:
                with self.assertRaises(RemoteExecutionError):
                    stream.pull({"path": str(path), "size": size, "sha256": "0" * 64}, destination, overwrite=True)
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_pull_commit_refuses_a_destination_created_after_preflight(self):
        source = self.root / "source"
        source.write_bytes(b"server bytes")
        destination = self.base / "destination"
        destination.write_bytes(b"another writer")
        item = {"path": str(source), "size": source.stat().st_size, "sha256": digest(source)}
        with ArtifactStream(self.endpoint, "pull", 1, 5000) as stream:
            with self.assertRaises(FileExistsError):
                stream.pull(item, destination)
        self.assertEqual(destination.read_bytes(), b"another writer")
        self.assertEqual(list(self.base.glob(".remote-dev-*")), [])

    @unittest.skipIf(os.name == "nt", "local SSH adapter cannot emulate a POSIX remote root on Windows")
    def test_pull_conflict_requires_explicit_overwrite(self):
        source = self.root / "source.bin"
        source.write_bytes(b"first")
        local = self.base / "download"
        first = remote_artifact_pull(self.endpoint, remote_path=str(source), local_dir=str(local))["result"]
        self.assertEqual(first["status"], "ok")
        self.assertEqual((local / "artifact").read_bytes(), b"first")

        source.write_bytes(b"second")
        blocked = remote_artifact_pull(self.endpoint, remote_path=str(source), local_dir=str(local))["result"]
        self.assertEqual((blocked["outcome"], blocked["status"]), ("blocked", "destination_exists"))
        self.assertEqual(blocked["conflicts"], [str(local / "artifact")])
        self.assertEqual((local / "artifact").read_bytes(), b"first")

        replaced = remote_artifact_pull(self.endpoint, remote_path=str(source), local_dir=str(local), overwrite=True)["result"]
        self.assertEqual(replaced["status"], "ok")
        self.assertEqual((local / "artifact").read_bytes(), b"second")
        self.assertTrue(Path(replaced["refs"]["local_manifest"]).exists())
        self.assertFalse((local / "manifest.json").exists())

    @unittest.skipIf(os.name == "nt", "local SSH adapter cannot emulate a POSIX remote root on Windows")
    def test_pull_preflights_every_file_and_preserves_remote_manifest_json(self):
        source = self.root / "tree"
        source.mkdir()
        (source / "a.txt").write_bytes(b"remote a")
        (source / "b.txt").write_bytes(b"remote b")
        (source / "manifest.json").write_bytes(b"remote metadata")
        local = self.base / "download"
        local.mkdir()
        (local / "b.txt").write_bytes(b"local b")

        blocked = remote_artifact_pull(self.endpoint, remote_path=str(source), local_dir=str(local))["result"]
        self.assertEqual(blocked["status"], "destination_exists")
        self.assertFalse((local / "a.txt").exists())
        self.assertFalse((local / "manifest.json").exists())
        self.assertEqual((local / "b.txt").read_bytes(), b"local b")

        changed = remote_artifact_pull(self.endpoint, remote_path=str(source), local_dir=str(local), overwrite=True)["result"]
        self.assertEqual(changed["status"], "ok")
        self.assertEqual((local / "a.txt").read_bytes(), b"remote a")
        self.assertEqual((local / "b.txt").read_bytes(), b"remote b")
        self.assertEqual((local / "manifest.json").read_bytes(), b"remote metadata")
        self.assertTrue(Path(changed["refs"]["local_manifest"]).exists())

    def test_client_allocations_are_bounded_for_a_large_transfer(self):
        source = self.root / "large.bin"
        with source.open("wb") as stream:
            chunk = b"a" * (1024 * 1024)
            for _ in range(32):
                stream.write(chunk)
        item = {"path": str(source), "size": source.stat().st_size, "sha256": digest(source)}
        tracemalloc.start()
        try:
            with ArtifactStream(self.endpoint, "pull", 1, 10000) as stream:
                stream.pull(item, self.base / "download")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 12 * 1024 * 1024)
        self.assertEqual(digest(self.base / "download"), item["sha256"])
