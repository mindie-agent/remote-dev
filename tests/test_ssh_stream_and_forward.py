"""Live attached SSH streams, local port forwards, and interactive bootstrap.

Split from ``test_ssh_transport.py`` to keep the pre-PR module-size redline.
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.core.errors import RemoteExecutionError  # noqa: E402
import remote_dev.core.ssh_transport as ssh_transport  # noqa: E402
from _ssh_test_support import _patch_composed_ssh, _process_alive  # noqa: E402

SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"


def _option_map(cmd: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    index = 0
    while index < len(cmd):
        if cmd[index] == "-o" and index + 1 < len(cmd):
            key, _, value = cmd[index + 1].partition("=")
            values[key] = value
            index += 2
            continue
        index += 1
    return values


class LiveStreamTests(unittest.TestCase):
    """Gap 3: attached live stream with dual-sided silent-hang handling."""

    def setUp(self) -> None:
        self.endpoint = Endpoint.for_long_stream("192.0.2.10", 46000)

    def test_stream_large_script_uses_binary_stdin_and_drains_live_output(self) -> None:
        import hashlib
        script = ("# unicode \u4e2d\u6587\n" * 50000) + "echo done\n"
        expected = hashlib.sha256(script.encode("utf-8")).hexdigest()
        code = (
            "import hashlib,sys;sys.stdout.write('progress\\n');sys.stdout.flush();"
            "data=sys.stdin.buffer.read();print(hashlib.sha256(data).hexdigest())"
        )
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", code]) as command:
            result = ssh_transport.run_stream(self.endpoint, script, timeout_ms=5000, merge_stderr=False)
        self.assertIsNone(command.call_args.args[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["progress", expected])
        self.assertFalse(result.timed_out)

    def test_stream_upload_deadline_when_peer_never_reads_stdin(self) -> None:
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", "import time;time.sleep(30)"]):
            started = time.monotonic()
            result = ssh_transport.run_stream(self.endpoint, "#" * 1000000, timeout_ms=200, merge_stderr=False)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 2)

    def test_cancel_stops_live_child_even_after_pipe_eof(self) -> None:
        import threading
        from remote_dev.core.cancellation import request_context
        for close_pipes in (False, True):
            for timeout in (None, 10000):
                with self.subTest(close_pipes=close_pipes, timeout=timeout):
                    cancelled = threading.Event()
                    code = "import os,time;" + ("os.close(1);os.close(2);" if close_pipes else "") + "time.sleep(3)"
                    timer = threading.Timer(0.04, cancelled.set)
                    with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", code]):
                        started = time.monotonic()
                        timer.start()
                        try:
                            with request_context(cancelled), self.assertRaises(RemoteExecutionError) as failure:
                                ssh_transport.run_stream(self.endpoint, "", timeout_ms=timeout, merge_stderr=False)
                        finally:
                            timer.cancel()
                            timer.join()
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(failure.exception.category, "cancelled")
                    self.assertEqual(failure.exception.submission_state, "uncertain")

    def test_stream_cleanup_preserves_primary_failure_and_completed_result(self) -> None:
        from remote_dev.core.errors import error_details
        primary = RemoteExecutionError("cancel fixture", category="cancelled", submission_state="uncertain")
        completed = ssh_transport.RemoteCompleted(7, "recorded stdout", "recorded stderr")
        timed_out = ssh_transport.RemoteCompleted(None, "partial output", "timeout", timed_out=True)
        stop = ssh_transport.OwnedProcess.stop
        def failed_stop(owner, **kwargs):
            stop(owner, **kwargs)
            raise OSError("cleanup fixture")
        for result in (primary, completed, timed_out):
            with self.subTest(primary=isinstance(result, Exception)):
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", "pass"]), mock.patch.object(
                        ssh_transport, "_read_attached", **options), mock.patch.object(ssh_transport.OwnedProcess, "stop", failed_stop):
                    with self.assertRaises(RemoteExecutionError) as caught:
                        ssh_transport.run_stream(self.endpoint, "", merge_stderr=False)
                self.assertIn("cleanup fixture", error_details(caught.exception)["cleanup_error"])
                if result is primary:
                    self.assertIs(caught.exception, primary)
                    self.assertEqual(caught.exception.submission_state, "uncertain")
                elif result is completed:
                    self.assertTrue(caught.exception.operation_completed)
                    self.assertIs(caught.exception.completed, completed)
                    self.assertEqual(caught.exception.submission_state, "acknowledged")
                else:
                    self.assertFalse(caught.exception.operation_completed)
                    self.assertIs(caught.exception.completed, timed_out)
                    self.assertEqual(caught.exception.submission_state, "uncertain")

    def test_cancel_keeps_its_state_when_reader_cleanup_also_fails(self) -> None:
        import threading
        from remote_dev.core.cancellation import request_context
        from remote_dev.core.errors import error_details
        cancelled = threading.Event()
        cancelled.set()
        stop = ssh_transport.OwnedProcess.stop
        def failed_stop(owner, **kwargs):
            stop(owner, **kwargs)
            raise OSError("reader cleanup fixture")
        with request_context(cancelled), mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", "import time;time.sleep(3)"]), mock.patch.object(
                ssh_transport.OwnedProcess, "stop", failed_stop):
            with self.assertRaises(RemoteExecutionError) as caught:
                ssh_transport.run_stream(self.endpoint, "", merge_stderr=False)
        self.assertEqual(caught.exception.category, "cancelled")
        self.assertEqual(caught.exception.submission_state, "uncertain")
        self.assertIn("reader cleanup fixture", error_details(caught.exception)["cleanup_error"])

    def test_real_exit_result_survives_reader_cleanup_failure(self) -> None:
        from remote_dev.core.errors import error_details
        stop = ssh_transport.OwnedProcess.stop
        def failed_stop(owner, **kwargs):
            stop(owner, **kwargs)
            raise OSError("terminal cleanup fixture")
        command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'known output\\n');sys.exit(7)"]
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=command), mock.patch.object(
                ssh_transport.OwnedProcess, "stop", failed_stop):
            with self.assertRaises(RemoteExecutionError) as caught:
                ssh_transport.run_stream(self.endpoint, "", merge_stderr=False)
        self.assertTrue(caught.exception.operation_completed)
        self.assertEqual(caught.exception.completed.returncode, 7)
        self.assertEqual(caught.exception.completed.stdout, "known output\n")
        self.assertEqual(caught.exception.submission_state, "acknowledged")
        self.assertIn("terminal cleanup fixture", error_details(caught.exception)["cleanup_error"])

    def test_stream_stdin_command_preserves_remote_timeout_and_independence(self) -> None:
        argv = ssh_transport.stream_ssh_command(self.endpoint, None, timeout_ms=120000)
        self.assertEqual(argv[-1], "timeout --preserve-status 120s bash -ls")
        self.assertEqual(_option_map(argv)["ControlMaster"], "no")
        self.assertEqual(_option_map(argv)["ControlPath"], "none")
        self.assertEqual(_option_map(argv)["ServerAliveInterval"], "30")

    def test_run_stream_wraps_remote_timeout_and_forwards_live_output(self) -> None:
        # Would fail on main: run_stream / stream_ssh_command do not exist,
        # and run_script only returns after the command finishes.
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                argv = ssh_transport.stream_ssh_command(self.endpoint, "analyze", timeout_ms=120000)
        options = _option_map(argv)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["ServerAliveInterval"], "30")
        remote = argv[-1]
        self.assertEqual(argv[-3:-1], ["bash", "-c"])
        self.assertIn("timeout --preserve-status", remote)
        self.assertIn("120s", remote)
        self.assertIn("--preserve-status", remote)
        self.assertIn("bash -lc", remote)

        buf = io.StringIO()
        # stream_ssh_command quotes for OpenSSH's remote-argv join. A local
        # Popen list must receive the script unquoted, so the live-forward
        # check patches the composed argv rather than ssh_base_cmd.
        local_cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'stage-a\\nstage-b\\n')",
        ]
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
            result = ssh_transport.run_stream(self.endpoint, "unused", timeout_ms=None, output=buf)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIn("[remote] stage-a", buf.getvalue())
        self.assertIn("[remote] stage-b", buf.getvalue())
        self.assertEqual(result.stdout, "")

    def test_run_stream_separate_channels_capture_stdout_stderr_and_callback(self) -> None:
        seen: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def on_output(channel: str, text: str) -> None:
            seen[channel].append(text)

        local_cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'machine-json\\n'); sys.stderr.buffer.write(b'progress\\n')",
        ]
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
            result = ssh_transport.run_stream(
                self.endpoint,
                "unused",
                timeout_ms=None,
                merge_stderr=False,
                on_output=on_output,
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, "machine-json\n")
        self.assertEqual(result.stderr, "progress\n")
        self.assertEqual(seen["stdout"], ["machine-json\n"])
        self.assertEqual(seen["stderr"], ["progress\n"])

    def test_run_stream_separate_channels_preserve_nonzero_status(self) -> None:
        local_cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'out\\n'); sys.stderr.buffer.write(b'err\\n'); raise SystemExit(7)",
        ]
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
            result = ssh_transport.run_stream(self.endpoint, "unused", merge_stderr=False)
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")

    def test_run_stream_waits_for_exit_after_pipe_eof(self) -> None:
        local_cmd = [
            sys.executable,
            "-c",
            "import os,time;os.close(1);os.close(2);time.sleep(0.1)",
        ]
        with mock.patch.object(ssh_transport, "STREAM_SELECT_SLICE_SECONDS", 0.02):
            with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
                started = time.monotonic()
                result = ssh_transport.run_stream(
                    self.endpoint,
                    "unused",
                    timeout_ms=None,
                    merge_stderr=False,
                )
                elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 2.0)

        with mock.patch.object(ssh_transport, "STREAM_SELECT_SLICE_SECONDS", 0.02):
            with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
                started = time.monotonic()
                result = ssh_transport.run_stream(
                    self.endpoint,
                    "unused",
                    timeout_ms=2000,
                    merge_stderr=False,
                )
                elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 2.0)

    def test_run_stream_separate_channels_honours_deadline_on_partial_line(self) -> None:
        local_cmd = [
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.buffer.write(b'partial');sys.stdout.buffer.flush();time.sleep(1.2)",
        ]
        started = time.monotonic()
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
            result = ssh_transport.run_stream(
                self.endpoint,
                "unused",
                timeout_ms=100,
                merge_stderr=False,
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertIn("wall-clock", result.stderr)
        self.assertEqual(result.stdout, "partial")
        self.assertLess(elapsed, 0.8)

    def test_read_stream_honours_local_deadline_without_remote_host(self) -> None:
        local_cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        buf = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=local_cmd):
            result = ssh_transport.run_stream(
                self.endpoint,
                "unused",
                timeout_ms=150,
                forward_prefix="",
                output=buf,
            )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertIn("wall-clock", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertLess(elapsed, 2.0)

    def test_stream_remote_payload_preserves_explicit_limit(self) -> None:
        wrapped = ssh_transport.stream_remote_payload("do-work", 30_000)
        self.assertTrue(wrapped.startswith("timeout --preserve-status 30s bash -lc "))
        self.assertIn("do-work", wrapped)
        self.assertEqual(ssh_transport.stream_remote_payload("do-work", None), "do-work")
        with self.assertRaises(ValueError):
            ssh_transport.stream_remote_payload("do-work", 0)
        one_second = ssh_transport.stream_remote_payload("do-work", 1_000)
        self.assertIn("timeout --preserve-status 1s ", one_second)

    def test_run_stream_is_not_a_result_v1_document(self) -> None:
        from remote_dev.result import RESULT_SCHEMA_VERSION, make_result

        self.assertEqual(RESULT_SCHEMA_VERSION, "remote-dev.result.v1")
        buf = io.StringIO()
        with mock.patch.object(ssh_transport, "stream_ssh_command", return_value=[sys.executable, "-c", "pass"]):
            completed = ssh_transport.run_stream(self.endpoint, "unused", output=buf)
        self.assertIsInstance(completed, ssh_transport.RemoteCompleted)
        self.assertNotIn("schema_version", completed.__dict__)
        wrapped = make_result(
            tool="remote.bash",
            target=self.endpoint.to_result_target(),
            outcome="success" if completed.returncode == 0 else "failed",
            status="ok",
            summary="stream finished",
        )
        self.assertEqual(wrapped["schema_version"], RESULT_SCHEMA_VERSION)

    def test_for_long_stream_is_the_natural_spelling_and_is_independent(self) -> None:
        endpoint = Endpoint.for_long_stream("192.0.2.10", 46000, identity_file="/keys/a")
        self.assertIs(endpoint.ssh_mux, False)
        self.assertTrue(endpoint.keepalive)
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                os.environ[SSH_MUX_ENV] = "1"
                cmd = ssh_transport.ssh_base_cmd(endpoint)
                argv = ssh_transport.stream_ssh_command(endpoint, "analyze", timeout_ms=120000)
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["ServerAliveInterval"], "30")
        self.assertEqual(options["ServerAliveCountMax"], "10")
        self.assertEqual(_option_map(argv)["ControlMaster"], "no")
        self.assertIn("timeout --preserve-status 120s", argv[-1])

    @unittest.skipIf(os.name == "nt", "native Windows default is already independent; see NativeWindowsMuxTests")
    def test_stream_refuses_a_muxed_endpoint(self) -> None:
        cases = (
            Endpoint(host="192.0.2.10", port=46000),
            Endpoint(host="192.0.2.10", port=46000, ssh_mux=True),
            Endpoint(host="192.0.2.10", port=46000, ssh_mux=True, keepalive=True),
            Endpoint(host="192.0.2.10", port=46000, keepalive=True),
        )
        for endpoint in cases:
            with self.subTest(ssh_mux=endpoint.ssh_mux, keepalive=endpoint.keepalive):
                with mock.patch.object(ssh_transport, "_MUX_READY", True):
                    with mock.patch.dict(os.environ):
                        os.environ.pop(SSH_MUX_ENV, None)
                        with self.assertRaises(RemoteExecutionError) as raised:
                            ssh_transport.stream_ssh_command(endpoint, "analyze")
                message = str(raised.exception)
                self.assertIn("rc=0", message)
                self.assertIn("first-option-wins", message)
                self.assertIn("ControlPath", message)
                self.assertIn("for_long_stream", message)


FAKE_SSH_PY = r"""
import os
import socket
import subprocess
import sys
import time

argv_path = os.environ.get("FAKE_SSH_ARGV")
if argv_path:
    with open(argv_path, "w", encoding="utf-8") as fh:
        fh.write("\0".join(sys.argv))

mode = os.environ.get("FAKE_SSH_MODE", "listen")
spec = None
args = sys.argv[1:]
index = 0
while index < len(args):
    if args[index] == "-L" and index + 1 < len(args):
        spec = args[index + 1]
        index += 2
        continue
    if args[index] == "-o" and index + 1 < len(args):
        index += 2
        continue
    index += 1

if mode == "exit0":
    raise SystemExit(0)
if mode == "exit1":
    sys.stderr.write("forward failed\n")
    raise SystemExit(1)
if mode == "interactive":
    raise SystemExit(int(os.environ.get("FAKE_SSH_RC", "0")))

if spec is None or spec.count(":") < 3:
    sys.stderr.write("missing -L spec\n")
    raise SystemExit(2)
local_host, local_port_s, _remote_host, _remote_port = spec.split(":", 3)
local_port = int(local_port_s)

if mode == "child":
    child_pid_path = os.environ["FAKE_SSH_CHILD_PID"]
    child = subprocess.Popen([sys.executable, "-c", "import time\nwhile True:\n    time.sleep(30)"])
    with open(child_pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(child.pid))

if mode == "hang":
    while True:
        time.sleep(30)

family = socket.AF_INET6 if ":" in local_host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((local_host, local_port))
sock.listen(8)
while True:
    sock.settimeout(1.0)
    try:
        conn, _addr = sock.accept()
    except socket.timeout:
        continue
    conn.close()
"""


def _write_fake_ssh(bindir: Path) -> Path:
    script = bindir / "ssh.py"
    script.write_text(FAKE_SSH_PY, encoding="utf-8")
    return script


class LocalForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.bindir = Path(self.temp.name) / "bin"
        self.bindir.mkdir()
        self.fake_ssh = _write_fake_ssh(self.bindir)
        self._ssh_patch = _patch_composed_ssh(self.fake_ssh)
        self.addCleanup(self._ssh_patch.close)
        self.endpoint = Endpoint.for_long_stream("192.0.2.10", 46000)
        self._env = {
            "HOME": str(self.home),
        }

    def _env_with(self, **extra: str) -> dict[str, str]:
        env = {**os.environ, **self._env, **extra}
        return env

    def test_readiness_failure_preserved_when_close_also_fails(self) -> None:
        from remote_dev.core.errors import error_details
        primary = RemoteExecutionError("readiness failed")
        close = ssh_transport.LocalForward.close
        def failed_close(handle):
            close(handle)
            raise OSError("cleanup fixture")
        with mock.patch.object(ssh_transport, "local_forward_ssh_command", return_value=[sys.executable, "-c", "import time;time.sleep(30)"]), mock.patch.object(
                ssh_transport.LocalForward, "wait_ready", side_effect=primary), mock.patch.object(
                ssh_transport.LocalForward, "close", failed_close):
            with self.assertRaises(RemoteExecutionError) as caught:
                ssh_transport.open_local_forward(self.endpoint, 8123)
        self.assertIs(caught.exception, primary)
        self.assertIn("cleanup fixture", error_details(primary)["cleanup_error"])

    def test_context_exit_preserves_primary_error_when_close_fails(self) -> None:
        # `with <forward> as fwd:` that raises must keep the body error and
        # attach the cleanup failure to it, never mask it with a NameError.
        from remote_dev.core.errors import error_details
        forward = ssh_transport.LocalForward(
            owner=mock.Mock(),
            local_host="127.0.0.1",
            local_port=1,
            remote_host="127.0.0.1",
            remote_port=2,
        )
        primary = RuntimeError("tunnel consumer failed")
        with mock.patch.object(ssh_transport.LocalForward, "close", side_effect=OSError("close fixture")):
            forward.__exit__(type(primary), primary, primary.__traceback__)
        self.assertIn("close fixture", error_details(primary)["cleanup_error"])

    def test_forward_argv_is_long_stream_plus_exit_on_forward_failure(self) -> None:
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                argv = ssh_transport.local_forward_ssh_command(
                    self.endpoint,
                    local_host="127.0.0.1",
                    local_port=47001,
                    remote_host="127.0.0.1",
                    remote_port=8000,
                )
        options = _option_map(argv)
        self.assertEqual(argv[0], ssh_transport._ssh_executable())
        self.assertEqual(options["BatchMode"], "yes")
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["ServerAliveInterval"], "30")
        self.assertEqual(options["ServerAliveCountMax"], "10")
        self.assertEqual(options["ExitOnForwardFailure"], "yes")
        self.assertIn("-N", argv)
        self.assertEqual(argv[argv.index("-L") + 1], "127.0.0.1:47001:127.0.0.1:8000")
        self.assertEqual(argv[argv.index("-l") + 1], "root")
        self.assertEqual(argv[argv.index("--") + 1], "192.0.2.10")
        self.assertNotIn("ControlMaster=auto", argv)
        destination = argv.index("--")
        self.assertLess(argv.index("ExitOnForwardFailure=yes"), destination)
        self.assertLess(argv.index("-N"), destination)
        self.assertLess(argv.index("-L"), destination)
        self.assertEqual(argv[destination + 1 :], ["192.0.2.10"])

    def test_forward_upgrades_independent_endpoint_to_keepalive(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=46000, ssh_mux=False, keepalive=False)
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            argv = ssh_transport.local_forward_ssh_command(
                endpoint,
                local_host="127.0.0.1",
                local_port=47002,
                remote_host="127.0.0.1",
                remote_port=9000,
            )
        options = _option_map(argv)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ServerAliveInterval"], "30")
        self.assertEqual(options["ExitOnForwardFailure"], "yes")

    @unittest.skipIf(os.name == "nt", "native Windows default is already independent; see NativeWindowsMuxTests")
    def test_forward_refuses_a_muxed_endpoint(self) -> None:
        cases = (
            Endpoint(host="192.0.2.10", port=46000),
            Endpoint(host="192.0.2.10", port=46000, ssh_mux=True),
            Endpoint(host="192.0.2.10", port=46000, ssh_mux=True, keepalive=True),
            Endpoint(host="192.0.2.10", port=46000, keepalive=True),
        )
        for endpoint in cases:
            with self.subTest(ssh_mux=endpoint.ssh_mux, keepalive=endpoint.keepalive):
                with mock.patch.object(ssh_transport, "_MUX_READY", True):
                    with mock.patch.dict(os.environ):
                        os.environ.pop(SSH_MUX_ENV, None)
                        with self.assertRaises(RemoteExecutionError) as raised:
                            ssh_transport.local_forward_ssh_command(
                                endpoint,
                                local_host="127.0.0.1",
                                local_port=47003,
                                remote_host="127.0.0.1",
                                remote_port=8000,
                            )
                message = str(raised.exception)
                self.assertIn("rc=0", message)
                self.assertIn("first-option-wins", message)
                self.assertIn("ControlPath", message)

    def test_open_local_forward_waits_until_port_accepts(self) -> None:
        argv_path = Path(self.temp.name) / "argv"
        with mock.patch.dict(os.environ, self._env_with(FAKE_SSH_ARGV=str(argv_path), FAKE_SSH_MODE="listen")):
            fwd = ssh_transport.open_local_forward(self.endpoint, 8123, ready_timeout_s=5.0)
        try:
            self.assertGreaterEqual(fwd.local_port, 1)
            self.assertIsNone(fwd.poll())
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", fwd.local_port))
            recorded = argv_path.read_text(encoding="utf-8").split("\0")
            composed = ssh_transport.local_forward_ssh_command(
                self.endpoint,
                local_host="127.0.0.1",
                local_port=fwd.local_port,
                remote_host="127.0.0.1",
                remote_port=8123,
            )
            self.assertEqual(composed[0], ssh_transport._ssh_executable())
            self.assertEqual(Path(recorded[0]).name, "ssh.py")
            self.assertEqual(recorded[1:], composed[1:])
            self.assertIn("-N", recorded)
            self.assertIn("ExitOnForwardFailure=yes", recorded)
        finally:
            result = fwd.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(fwd.poll())
        self.assertNotEqual(fwd.poll(), 0)

    def test_dead_forward_is_never_rc_zero(self) -> None:
        with mock.patch.dict(os.environ, self._env_with(FAKE_SSH_MODE="exit0")):
            with self.assertRaises(RemoteExecutionError) as raised:
                ssh_transport.open_local_forward(self.endpoint, 8123, ready_timeout_s=2.0)
        message = str(raised.exception)
        self.assertIn(f"rc={ssh_transport.FORWARD_DEAD_EXIT_CODE}", message)
        self.assertIn("ssh rc=0", message)
        self.assertNotIn("rc=0)", message.replace("ssh rc=0", ""))

    def test_close_kills_child_process(self) -> None:
        child_pid_path = Path(self.temp.name) / "child.pid"
        with mock.patch.dict(os.environ, self._env_with(FAKE_SSH_MODE="child", FAKE_SSH_CHILD_PID=str(child_pid_path))):
            fwd = ssh_transport.open_local_forward(self.endpoint, 8123, ready_timeout_s=5.0)
        deadline = time.time() + 2
        while time.time() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        self.assertTrue(_process_alive(child_pid), f"descendant {child_pid} was not started")
        parent = fwd._proc
        parent_pid = parent.pid
        result = fwd.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(parent.poll(), f"forward parent {parent_pid} still running after close()")
        self.assertFalse(_process_alive(parent_pid), f"forward parent {parent_pid} still alive after close()")
        deadline = time.time() + 5
        while time.time() < deadline:
            if not _process_alive(child_pid):
                break
            time.sleep(0.05)
        else:
            self.fail(f"forward descendant {child_pid} still alive after close()")

    def test_wait_ready_times_out_and_close_reaps_hanging_ssh(self) -> None:
        with mock.patch.dict(os.environ, self._env_with(FAKE_SSH_MODE="hang")):
            with self.assertRaises(RemoteExecutionError) as raised:
                ssh_transport.open_local_forward(self.endpoint, 8123, ready_timeout_s=0.15)
        self.assertIn("timed out", str(raised.exception))


class InteractiveBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.bindir = Path(self.temp.name) / "bin"
        self.bindir.mkdir()
        self.fake_ssh = _write_fake_ssh(self.bindir)
        self._ssh_patch = _patch_composed_ssh(self.fake_ssh)
        self.addCleanup(self._ssh_patch.close)
        self.endpoint = Endpoint(host="192.0.2.10", port=22, user="ubuntu", ssh_mux=False)

    def test_interactive_argv_is_password_bootstrap_off_the_mux(self) -> None:
        argv = ssh_transport.interactive_ssh_command(
            self.endpoint,
            ["sh", "-c", "umask 077; mkdir -p ~/.ssh"],
        )
        options = _option_map(argv)
        self.assertEqual(argv[0], ssh_transport._ssh_executable())
        self.assertEqual(options["BatchMode"], "no")
        self.assertEqual(options["StrictHostKeyChecking"], "accept-new")
        self.assertEqual(options["LogLevel"], "ERROR")
        self.assertNotIn("ConnectTimeout", options)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["PreferredAuthentications"], "password,keyboard-interactive")
        self.assertEqual(options["PubkeyAuthentication"], "no")
        self.assertEqual(options["NumberOfPasswordPrompts"], "1")
        self.assertNotIn("BatchMode=yes", argv)
        self.assertNotIn("ControlMaster=auto", argv)
        self.assertNotIn("ServerAliveInterval", options)
        self.assertNotIn("-i", argv)
        self.assertEqual(argv[argv.index("-l") + 1], "ubuntu")
        self.assertEqual(argv[argv.index("-p") + 1], "22")
        self.assertEqual(argv[argv.index("--") + 1], "192.0.2.10")
        self.assertEqual(argv[-3:], ["sh", "-c", "umask 077; mkdir -p ~/.ssh"])

    @unittest.skipIf(os.name == "nt", "native Windows default is already independent; see NativeWindowsMuxTests")
    def test_interactive_refuses_a_muxed_endpoint(self) -> None:
        cases = (
            Endpoint(host="192.0.2.10", port=22),
            Endpoint(host="192.0.2.10", port=22, ssh_mux=True),
            Endpoint(host="192.0.2.10", port=22, keepalive=True),
        )
        for endpoint in cases:
            with self.subTest(ssh_mux=endpoint.ssh_mux, keepalive=endpoint.keepalive):
                with mock.patch.object(ssh_transport, "_MUX_READY", True):
                    with mock.patch.dict(os.environ):
                        os.environ.pop(SSH_MUX_ENV, None)
                        with self.assertRaises(RemoteExecutionError) as raised:
                            ssh_transport.interactive_ssh_command(endpoint, ["true"])
                message = str(raised.exception)
                self.assertIn("password prompt", message)
                self.assertIn("impossible", message.lower())

    def test_run_interactive_inherits_and_returns_ssh_rc(self) -> None:
        argv_path = Path(self.temp.name) / "argv"
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FAKE_SSH_MODE": "interactive",
            "FAKE_SSH_RC": "7",
            "FAKE_SSH_ARGV": str(argv_path),
        }
        with mock.patch.dict(os.environ, env):
            rc = ssh_transport.run_interactive(self.endpoint, ["sh", "-c", "printf ok"])
        self.assertEqual(rc, 7)
        recorded = argv_path.read_text(encoding="utf-8").split("\0")
        composed = ssh_transport.interactive_ssh_command(self.endpoint, ["sh", "-c", "printf ok"])
        self.assertEqual(composed[0], ssh_transport._ssh_executable())
        self.assertEqual(Path(recorded[0]).name, "ssh.py")
        self.assertEqual(recorded[1:], composed[1:])
        self.assertEqual(recorded[-3:], ["sh", "-c", "printf ok"])
        self.assertIn("BatchMode=no", recorded)
        self.assertIn("PubkeyAuthentication=no", recorded)


if __name__ == "__main__":
    unittest.main()
