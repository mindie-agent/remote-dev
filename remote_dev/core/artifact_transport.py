"""Binary artifact streaming over one independent, cancellable SSH channel."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import replace

from .cancellation import current_event
from .atomic import publish_file
from .errors import RemoteExecutionError, record_cleanup_failure
from .locking import path_lock
from .execution import timeout_value

CHUNK_SIZE = 1024 * 1024


class ArtifactTransferError(RemoteExecutionError):
    def __init__(self, message, expected_sha256=None, observed_sha256=None, **details):
        super().__init__(message, **details)
        self.expected_sha256 = expected_sha256
        self.observed_sha256 = observed_sha256


class ArtifactStream:
    def __init__(self, endpoint, operation, count, timeout_ms=None):
        timeout_value(timeout_ms)
        from .container_endpoint import pin_container_endpoint
        from .ssh_transport import ssh_command
        endpoint = pin_container_endpoint(endpoint, timeout_ms=timeout_ms)
        source = (Path(__file__).parents[1] / "processes" / "artifact_worker.py").read_text(encoding="utf-8")
        helper = (Path(__file__).parents[1] / "processes" / "mutation.py").read_text(encoding="utf-8")
        source = source.replace("# REMOTE_DEV_MUTATION_LOCK", helper)
        self.proc = subprocess.Popen(
            ssh_command(replace(endpoint, ssh_mux=False, keepalive=True), "python3 -u -c " + shlex.quote(source)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.done = threading.Event()
        self.cancelled = current_event()
        self.deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000
        self.operation = operation
        self.commit_possible = False
        self.phase = "prepare"
        self.reason = ""
        self.expected_sha256 = None
        self.stderr = bytearray()
        self.guard = threading.Thread(target=self._watch, daemon=True)
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.guard.start()
        self.drain.start()
        try:
            self.send({"root": endpoint.root, "operation": operation, "count": count, "timeout_ms": timeout_ms})
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                record_cleanup_failure(error, cleanup_error)
            raise

    def _watch(self):
        while not self.done.wait(0.05):
            if self.cancelled is not None and self.cancelled.is_set():
                self.reason = "artifact transfer cancelled"
            elif self.deadline is not None and time.monotonic() >= self.deadline:
                self.reason = "artifact transfer timed out"
            elif self.proc.poll() is not None:
                return  # EOF/error is reported by the stream reader.
            if self.reason:
                with contextlib.suppress(OSError):
                    self.proc.kill()
                return

    def _drain(self):
        while True:
            chunk = self.proc.stderr.read(1024)
            if not chunk:
                return
            self.stderr.extend(chunk)
            del self.stderr[:-4000]

    def send(self, data):
        self.proc.stdin.write((json.dumps(data) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def receive(self):
        line = self.proc.stdout.readline(65536)
        if not line.endswith(b"\n"):
            raise RemoteExecutionError(
                self.reason or "artifact stream disconnected: " + self.stderr.decode("utf-8", "replace"),
                submission_state="uncertain" if self.commit_possible else "not_sent",
            )
        result = json.loads(line)
        if result.get("status") not in {"ready", "ok"}:
            raise ArtifactTransferError("artifact " + str(result.get("status", "failed")) + ": " + str(result.get("error", "")), self.expected_sha256, result.get("sha256"), submission_state="acknowledged")
        return result

    def pull(self, item, destination, *, overwrite=False):
        self.phase = "transfer"
        with path_lock(destination):
            self.expected_sha256 = item["sha256"]
            self.send(item)
            ready = self.receive()
            if ready.get("size") != item["size"]:
                raise RemoteExecutionError("artifact size changed")
            fd, temporary = tempfile.mkstemp(prefix=".remote-dev-", dir=str(destination.parent))
            try:
                digest = hashlib.sha256()
                size = item["size"]
                with os.fdopen(fd, "wb") as output:
                    while size:
                        chunk = self.proc.stdout.read(min(CHUNK_SIZE, size))
                        if not chunk:
                            raise RemoteExecutionError(self.reason or "incomplete artifact stream")
                        output.write(chunk)
                        digest.update(chunk)
                        size -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                reply = self.receive()
                if digest.hexdigest() != item["sha256"] or reply.get("sha256") != item["sha256"]:
                    raise ArtifactTransferError("artifact hash_mismatch", item["sha256"], digest.hexdigest())
                if destination.is_symlink():
                    raise RemoteExecutionError("refusing to replace a local symlink")
                publish_file(temporary, destination, overwrite=overwrite)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
        self.phase = "completed"
        return item["sha256"]

    def push(self, item, source):
        self.commit_possible = False
        self.phase = "transfer"
        self.expected_sha256 = item["sha256"]
        # An empty push needs no payload: receipt of the header alone can
        # publish it, even if the ready frame or final ACK is lost.
        if item["size"] == 0:
            self.commit_possible = True
            self.phase = "commit_ack"
        self.send(item)
        self.receive()
        remaining = item["size"]
        with source.open("rb") as stream:
            while remaining:
                chunk = stream.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise RemoteExecutionError("local artifact changed since manifest")
                if len(chunk) == remaining:
                    # Once all file bytes may reach the worker it can commit.
                    # A lost ACK is not proof that the destination is unchanged.
                    self.commit_possible = True
                    self.phase = "commit_ack"
                self.proc.stdin.write(chunk)
                remaining -= len(chunk)
        self.proc.stdin.flush()
        reply = self.receive()
        if reply.get("sha256") != item["sha256"]:
            raise ArtifactTransferError("artifact hash_mismatch", item["sha256"], reply.get("sha256"))
        self.commit_possible = False
        self.phase = "completed"
        return item["sha256"]

    def close(self):
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.done.set()
        self.guard.join(timeout=1)
        self.drain.join(timeout=1)
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, _type, error, _traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if error is not None:
                record_cleanup_failure(error, cleanup_error)
            else:
                failure = ArtifactTransferError("artifact stream cleanup failed", submission_state="acknowledged")
                failure.operation_completed = self.phase == "completed"
                failure.cleanup_error = type(cleanup_error).__name__
                raise failure from cleanup_error
