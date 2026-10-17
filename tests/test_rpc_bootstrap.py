from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import string
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from remote_dev.core import rpc_transport, ssh_transport
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError


class RpcBootstrapTests(unittest.TestCase):
    def test_windows_bootstrap_decodes_exact_complete_worker(self):
        processes = Path(rpc_transport.__file__).parents[1] / "processes"
        source = (processes / "rpc_worker.py").read_text(encoding="utf-8").replace(
            "# REMOTE_DEV_MUTATION_LOCK", (processes / "mutation.py").read_text(encoding="utf-8")
        )
        source += "\n# UTF-8: \u4e2d\u6587; quotes ' \" and slash \\\\\n"
        with mock.patch.object(ssh_transport, "_native_windows_ssh_client", return_value=True):
            command = rpc_transport._worker_command(source)
        self.assertTrue(command.isascii())
        self.assertLessEqual(len(command), 8191)
        argv = shlex.split(command)
        self.assertEqual(argv[:4], ["python3", "-I", "-u", "-c"])
        captured = []
        exec(argv[4], {"exec": captured.append})
        self.assertEqual(captured, [source.encode("utf-8")])
        self.assertEqual(
            hashlib.sha256(captured[0]).hexdigest(),
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    def test_oversized_windows_bootstrap_fails_before_spawn(self):
        source = "".join(random.Random(1).choices(string.ascii_letters + string.digits, k=20000))
        with mock.patch.object(
            ssh_transport, "_native_windows_ssh_client", return_value=True
        ), mock.patch.object(Path, "read_text", return_value=source), mock.patch.object(
            rpc_transport, "OwnedProcess"
        ) as spawn:
            with self.assertRaises(RemoteExecutionError) as caught:
                rpc_transport.RpcConnection(Endpoint("example.invalid", 22, ssh_mux=False))
        self.assertEqual(caught.exception.category, "configuration")
        self.assertEqual(caught.exception.submission_state, "not_sent")
        self.assertFalse(caught.exception.retryable)
        spawn.assert_not_called()

    def test_posix_bootstrap_keeps_existing_source_argument(self):
        source = "print('quote \" and unicode \\u4e2d')\n"
        with mock.patch.object(ssh_transport, "_native_windows_ssh_client", return_value=False):
            self.assertEqual(rpc_transport._worker_command(source), "python3 -I -u -c " + shlex.quote(source))

    def test_rpc_bootstrap_and_package_python_ignore_shadowing_working_directory(self):
        processes = Path(rpc_transport.__file__).parents[1] / "processes"
        source = (processes / "rpc_worker.py").read_text().replace(
            "# REMOTE_DEV_MUTATION_LOCK", (processes / "mutation.py").read_text()
        )
        payload = (
            "import json,platform,uuid\n"
            "print(json.dumps({'system':platform.system(),'uuid_valid':bool(uuid.uuid4())}))\n"
        )
        request = dict(
            id=1, kind="python", code=payload,
            code_key=hashlib.sha256(payload.encode()).hexdigest(),
            payload={}, timeout_ms=3000,
        )
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "platform.py").write_text("raise RuntimeError('SHADOWED_STDLIB')\n")
            with mock.patch.object(ssh_transport, "_native_windows_ssh_client", return_value=False):
                argv = shlex.split(rpc_transport._worker_command(source))
            child = subprocess.Popen(
                [sys.executable, *argv[1:]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=directory, env={**os.environ, "PYTHONPATH": directory},
            )
            deadline = threading.Timer(5, child.kill)
            deadline.start()
            try:
                ready = child.stdout.readline()
                child.stdin.write(json.dumps(request) + "\n")
                child.stdin.flush()
                reply = child.stdout.readline()
                _, stderr = child.communicate(timeout=5)
            finally:
                deadline.cancel()
                if child.poll() is None:
                    child.kill()
                child.wait()
        self.assertEqual(child.returncode, 0, stderr)
        self.assertTrue(json.loads(ready)["ready"])
        result = json.loads(reply)["result"]
        self.assertEqual(result["returncode"], 0, result["stderr"])
        value = json.loads(result["stdout"])
        self.assertTrue(value["system"])
        self.assertTrue(value["uuid_valid"])


if __name__ == "__main__":
    unittest.main()
