from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from remote_dev.core.endpoint import Endpoint  # noqa: E402
from remote_dev.core.errors import RemoteExecutionError  # noqa: E402
import remote_dev.core.ssh_transport as ssh_transport  # noqa: E402
import remote_dev.core.local_process as local_process
from _ssh_test_support import _windows_pid_alive  # noqa: E402,F401 - compatibility import

SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"

_CHILD_SSH_ARGV = """
import json
from remote_dev.core.endpoint import Endpoint
import remote_dev.core.ssh_transport as t
t._MUX_READY = True
endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")
print(json.dumps(t.ssh_base_cmd(endpoint)))
"""


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


def _require_openssh() -> str:
    path = (ssh_transport._ssh_executable() if os.name == "nt"
            else shutil.which("ssh"))
    if path is None:
        raise AssertionError(
            "OpenSSH ssh is required to parse composed argv with ssh -G; "
            "a skipped parser test is how options-after-destination shipped"
        )
    return path


def _tokens_after_host(argv: list[str]) -> list[str]:
    return list(argv[argv.index("--") + 2 :])


def _openssh_G(
    argv: list[str],
    *,
    ssh: str | None = None,
    config_file: str | None = None,
    home: str | None = None,
) -> tuple[dict[str, str], str]:
    binary = ssh or _require_openssh()
    if argv[0] != "ssh" and not argv[0].endswith("/ssh") and not argv[0].endswith("\\ssh") and not argv[0].endswith("ssh.exe"):
        raise AssertionError(f"composed argv must start with ssh, got {argv[0]!r}")
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = home
    with ExitStack() as files:
        if config_file is None:
            # Git SSH does not interpret the Windows device name "nul".
            directory = Path(files.enter_context(tempfile.TemporaryDirectory()))
            empty_config = directory / "ssh_config"
            empty_config.write_text("", encoding="utf-8")
            config_file = str(empty_config)
        eval_cmd = [binary, "-G", "-F", config_file, *argv[1:]]
        proc = subprocess.run(eval_cmd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise AssertionError(
            f"ssh -G failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '')[:2000]}\n"
            f"argv={eval_cmd!r}"
        )
    parsed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        parsed[key.lower()] = value.strip()
    return parsed, proc.stdout


class SshTransportTests(unittest.TestCase):
    def test_known_hosts_with_spaces_remains_one_file_in_real_openssh_parser(self):
        with tempfile.TemporaryDirectory(prefix="ssh user home ") as home:
            known_hosts = str(Path(home) / "shared trust" / "known_hosts")
            endpoint = Endpoint("example.invalid", 22, ssh_mux=False, known_hosts_file=known_hosts)
            parsed, _ = _openssh_G(ssh_transport.ssh_command(endpoint, "true"))
            self.assertEqual(parsed["userknownhostsfile"], known_hosts)

    def test_run_remote_python_uses_rpc_and_parses_json(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=22)
        from remote_dev.core import rpc_transport
        row = {"returncode": 0, "stdout": '{"status":"ok"}', "stderr": ""}
        code = "import json\nprint(json.dumps({'status':'ok'}))"
        with mock.patch.object(rpc_transport, "request", return_value=row) as execute:
            payload = ssh_transport.run_remote_python(endpoint, code, {}, timeout_ms=500)
        self.assertEqual(payload["status"], "ok")
        execute.assert_called_once_with(endpoint, "python", code, {}, timeout_ms=500)

    def test_run_bytes_quotes_shell_command_as_one_remote_command(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000)
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(ssh_transport, "_capture_command", fake_run):
            ssh_transport.run_bytes(endpoint, "cat '/tmp/path with spaces'")

        args = observed["args"]
        self.assertIsInstance(args, list)
        self.assertEqual(args[-1].split(" ", 2)[:2], ["bash", "-c"])
        self.assertIn("path with spaces", args[-1])

    @staticmethod
    def _control_path(options: list[str]) -> str:
        return next(option for option in options if option.startswith("ControlPath="))

    def test_control_path_is_scoped_per_identity_file(self) -> None:
        # OpenSSH %C does not hash the identity file, so endpoints that differ
        # only by SSH key must get distinct ControlPath sockets (D9).
        with mock.patch.object(ssh_transport, "_MUX_READY", True), mock.patch.object(
                ssh_transport, "_MUX_DIR", "/test-mux"):
            plain = self._control_path(ssh_transport._control_master_options())
            first = self._control_path(ssh_transport._control_master_options("/keys/a"))
            second = self._control_path(ssh_transport._control_master_options("/keys/b"))
        self.assertTrue(plain.endswith("/%C"))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, plain)
        self.assertTrue(first.startswith(plain))

    def test_mux_dir_defaults_under_home_and_honours_env_override(self) -> None:
        # The mux directory is remote-dev's own by default; a consumer that
        # wants to share its existing OpenSSH mux dir sets REMOTE_DEV_SSH_MUX_DIR.
        self.assertEqual(ssh_transport._MUX_DIR, Path.home() / ".ssh" / "remote-dev-mux")
        code = "import remote_dev.core.ssh_transport as t; print(t._MUX_DIR)"
        mux = str(Path(tempfile.gettempdir()) / "remote-dev-shared-mux")
        env = {**os.environ, "REMOTE_DEV_SSH_MUX_DIR": mux}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), mux)

    def test_ssh_base_cmd_passes_identity_file_into_control_path(self) -> None:
        with_identity = Endpoint(host="1.2.3.4", port=46000, identity_file="/keys/a")
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            options = ssh_transport._control_master_options(with_identity.identity_file)
            expected = self._control_path(options)
            cmd = ssh_transport.ssh_base_cmd(with_identity)
        self.assertIn("/keys/a", cmd)
        mapped = _option_map(cmd)
        if os.name == "nt":
            # Native Windows ordinary connections are independent; identity
            # still selects the key, but ControlPath is none, not a mux socket.
            self.assertEqual(mapped["ControlMaster"], "no")
            self.assertEqual(mapped["ControlPath"], "none")
            self.assertEqual(mapped["ControlPersist"], "no")
            self.assertNotIn(expected, cmd)
        else:
            self.assertIn(expected, cmd)
            self.assertIn("%C-", mapped["ControlPath"])

    def test_ssh_base_cmd_cannot_turn_user_or_host_into_an_option(self) -> None:
        # A user or host that begins with `-` must stay an argument of `-l`
        # / the destination after `--`. OpenSSH would otherwise treat
        # `-oProxyCommand=...` as an option and run a local command.
        endpoint = Endpoint(host="-oProxyCommand=marker", port=22, user="-oProxyCommand=evil")
        with mock.patch.object(ssh_transport, "_MUX_READY", False):
            cmd = ssh_transport.ssh_base_cmd(endpoint)
        self.assertEqual(cmd[cmd.index("-l") + 1], "-oProxyCommand=evil")
        self.assertEqual(cmd[cmd.index("--") + 1], "-oProxyCommand=marker")
        self.assertLess(cmd.index("-l"), cmd.index("--"))
        self.assertNotIn("-oProxyCommand=evil@-oProxyCommand=marker", cmd)


class SshMuxIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")

    def _cmd_with_mux(self, value: str | None, *, mux_ready: bool = True) -> list[str]:
        with mock.patch.object(ssh_transport, "_MUX_READY", mux_ready):
            with mock.patch.dict(os.environ):
                if value is None:
                    os.environ.pop(SSH_MUX_ENV, None)
                else:
                    os.environ[SSH_MUX_ENV] = value
                return ssh_transport.ssh_base_cmd(self.endpoint)

    def test_mux_zero_emits_independent_connection_options(self) -> None:
        cmd = self._cmd_with_mux("0")
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertNotIn("ControlMaster=auto", cmd)
        self.assertFalse(any(item.startswith("ControlPath=") and item != "ControlPath=none" for item in cmd))

    @unittest.skipIf(os.name == "nt", "native Windows has no ControlMaster; see NativeWindowsMuxTests")
    def test_unset_and_explicit_one_keep_shared_mux_and_identity_suffix(self) -> None:
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            expected_path = next(
                item for item in ssh_transport._control_master_options("/keys/a") if item.startswith("ControlPath=")
            )
        for value in (None, "1"):
            with self.subTest(REMOTE_DEV_SSH_MUX=value):
                cmd = self._cmd_with_mux(value)
                options = _option_map(cmd)
                self.assertEqual(options["ControlMaster"], "auto")
                self.assertEqual(options["ControlPersist"], "120")
                self.assertEqual("ControlPath=" + options["ControlPath"], expected_path)
                self.assertIn("%C-", options["ControlPath"])
                self.assertIn(expected_path, cmd)

    def test_runners_honor_mux_zero_and_keep_quoting_and_results(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            if args[-2:] == ["bash", "-s"]:
                self.assertEqual(kwargs["input"], b"echo hi")
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=b'{"status":"ok"}', stderr=b"")
            if kwargs.get("text"):
                return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"status":"ok"}', stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"bytes-out", stderr=b"")

        with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
            with mock.patch.object(ssh_transport, "_capture_command", fake_run):
                script_result = ssh_transport.run_script(self.endpoint, "echo hi")
                script_args = list(observed["args"])
                bytes_result = ssh_transport.run_bytes(self.endpoint, "cat '/tmp/path with spaces'", stdin=b"abc")
                bytes_args = list(observed["args"])

        for args in (script_args, bytes_args):
            options = _option_map(args)
            self.assertEqual(options["ControlMaster"], "no")
            self.assertEqual(options["ControlPath"], "none")
            self.assertEqual(options["ControlPersist"], "no")
            self.assertEqual(args[0], ssh_transport._ssh_executable())
            self.assertEqual(args[args.index("-l") + 1], self.endpoint.user)
            self.assertEqual(args[args.index("--") + 1], self.endpoint.host)

        self.assertEqual(script_args[-2:], ["bash", "-s"])
        self.assertEqual(script_result.returncode, 0)
        self.assertEqual(script_result.stdout, '{"status":"ok"}')
        self.assertFalse(script_result.timed_out)

        self.assertEqual(bytes_args[-1].split(" ", 2)[:2], ["bash", "-c"])
        self.assertIn("path with spaces", bytes_args[-1])
        self.assertEqual(bytes_result.returncode, 0)
        self.assertEqual(bytes_result.stdout, b"bytes-out")

    def test_openssh_G_evaluates_independent_mux_options(self) -> None:
        ssh = _require_openssh()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "ssh_config"
            fake_path = str(Path(tmp) / "cm-%C")
            config_path.write_text(
                "Host *\n"
                "  ControlMaster auto\n"
                f"  ControlPath {fake_path}\n"
                "  ControlPersist 120\n"
            )
            cmd = self._cmd_with_mux("0")
            parsed, stdout = _openssh_G(cmd, config_file=str(config_path), ssh=ssh)
            self.assertIn("controlmaster", parsed, stdout[:400])
            self.assertIn("controlpersist", parsed, stdout[:400])
            path = parsed.get("controlpath", "none")
            self.assertIn(parsed["controlmaster"].lower(), {"false", "no"})
            self.assertEqual(path.lower(), "none")
            self.assertIn(parsed["controlpersist"].lower(), {"no", "0", "false"})
            self.assertNotIn(fake_path.lower(), path.lower())
            self.assertNotIn(fake_path, stdout)

    def test_unsupported_mux_values_are_configuration_errors(self) -> None:
        for value in ("", "2", "false", "true", "off", "yes", "no", "auto", "00"):
            with self.subTest(REMOTE_DEV_SSH_MUX=value):
                with self.assertRaises(RemoteExecutionError) as raised:
                    self._cmd_with_mux(value)
                message = str(raised.exception)
                self.assertIn(SSH_MUX_ENV, message)
                self.assertIn("0", message)
                self.assertIn("1", message)

    def test_mux_zero_preserves_identity_timeout_and_option_injection(self) -> None:
        dashed = Endpoint(
            host="-oProxyCommand=marker",
            port=22,
            user="-oProxyCommand=evil",
            identity_file="/keys/a",
            connect_timeout_ms=5000,
        )
        with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
            cmd = ssh_transport.ssh_base_cmd(dashed)
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertEqual(options["ConnectTimeout"], "5")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/keys/a")
        self.assertEqual(cmd[cmd.index("-l") + 1], "-oProxyCommand=evil")
        self.assertEqual(cmd[cmd.index("--") + 1], "-oProxyCommand=marker")
        self.assertLess(cmd.index("-l"), cmd.index("--"))
        self.assertNotIn("-oProxyCommand=evil@-oProxyCommand=marker", cmd)



class NativeWindowsMuxTests(unittest.TestCase):
    """Windows transport supports independent connections without an extra flag."""

    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")
        selected = mock.patch.object(ssh_transport, "_windows_ssh_executable",
                                     return_value="C:/Git/usr/bin/ssh.exe")
        selected.start()
        self.addCleanup(selected.stop)

    def _windows_cmd(self, endpoint: Endpoint, env_value: str | None = None) -> list[str]:
        with mock.patch.object(ssh_transport, "_native_windows_ssh_client", return_value=True):
            with mock.patch.object(ssh_transport, "_MUX_READY", True):
                with mock.patch.dict(os.environ):
                    if env_value is None:
                        os.environ.pop(SSH_MUX_ENV, None)
                    else:
                        os.environ[SSH_MUX_ENV] = env_value
                    return ssh_transport.ssh_base_cmd(endpoint)

    def test_ordinary_windows_argv_is_independent_without_a_flag(self) -> None:
        cmd = self._windows_cmd(self.endpoint, None)
        options = _option_map(cmd)
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")
        self.assertNotIn("ControlMaster=auto", cmd)
        self.assertFalse(any(item.startswith("ControlPath=") and item != "ControlPath=none" for item in cmd))
        env_zero = _option_map(self._windows_cmd(self.endpoint, "0"))
        self.assertEqual(env_zero, options)

    def test_explicit_windows_mux_request_is_a_capability_error(self) -> None:
        with self.assertRaises(RemoteExecutionError) as raised_endpoint:
            self._windows_cmd(Endpoint(host="192.0.2.10", port=46000, ssh_mux=True))
        self.assertIn("ControlMaster is not supported", str(raised_endpoint.exception))
        self.assertIn("native Windows", str(raised_endpoint.exception))
        with self.assertRaises(RemoteExecutionError) as raised_env:
            self._windows_cmd(self.endpoint, "1")
        self.assertIn("ControlMaster is not supported", str(raised_env.exception))
        self.assertNotIn("ControlMaster=auto", str(raised_endpoint.exception))

    def test_windows_default_stream_and_interactive_stay_independent(self) -> None:
        default = Endpoint(host="192.0.2.10", port=46000)
        with mock.patch.object(ssh_transport, "_native_windows_ssh_client", return_value=True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                stream = ssh_transport.stream_ssh_command(default, "analyze")
                interactive = ssh_transport.interactive_ssh_command(default, ["true"])
        self.assertEqual(_option_map(stream)["ControlMaster"], "no")
        self.assertEqual(_option_map(stream)["ControlPath"], "none")
        self.assertEqual(_option_map(interactive)["ControlMaster"], "no")
        self.assertEqual(_option_map(interactive)["ControlPath"], "none")


@unittest.skipIf(os.name == "nt", "native Windows has no ControlMaster; see NativeWindowsMuxTests")
class PerEndpointMuxTests(unittest.TestCase):
    """Gap 1: mux is an endpoint property. Shared and independent coexist."""

    def setUp(self) -> None:
        self.shared = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a", ssh_mux=True)
        self.independent = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a", ssh_mux=False)

    def _cmd(self, endpoint: Endpoint, env_value: str | None = None) -> list[str]:
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                if env_value is None:
                    os.environ.pop(SSH_MUX_ENV, None)
                else:
                    os.environ[SSH_MUX_ENV] = env_value
                return ssh_transport.ssh_base_cmd(endpoint)

    def test_per_endpoint_mux_coexists_in_one_process_either_order(self) -> None:
        # Would fail on main: mux is process-global, so the second call
        # inherits the first call's REMOTE_DEV_SSH_MUX decision. Both orders
        # must produce ControlMaster=auto on one endpoint and the full
        # independent triple on the other.
        observed: list[tuple[str, dict[str, str]]] = []
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                for label, first, second in (
                    ("shared-then-independent", self.shared, self.independent),
                    ("independent-then-shared", self.independent, self.shared),
                ):
                    first_cmd = ssh_transport.ssh_base_cmd(first)
                    second_cmd = ssh_transport.ssh_base_cmd(second)
                    first_opts = _option_map(first_cmd)
                    second_opts = _option_map(second_cmd)
                    observed.append((label, first_opts, second_opts, first_cmd, second_cmd))
                    if first is self.shared:
                        self.assertEqual(first_opts["ControlMaster"], "auto")
                        self.assertEqual(first_opts["ControlPersist"], "120")
                        self.assertIn("%C-", first_opts["ControlPath"])
                        self.assertNotEqual(first_opts["ControlPath"], "none")
                        self.assertEqual(second_opts["ControlMaster"], "no")
                        self.assertEqual(second_opts["ControlPath"], "none")
                        self.assertEqual(second_opts["ControlPersist"], "no")
                    else:
                        self.assertEqual(first_opts["ControlMaster"], "no")
                        self.assertEqual(first_opts["ControlPath"], "none")
                        self.assertEqual(first_opts["ControlPersist"], "no")
                        self.assertEqual(second_opts["ControlMaster"], "auto")
                        self.assertEqual(second_opts["ControlPersist"], "120")
                        self.assertIn("%C-", second_opts["ControlPath"])
                        self.assertNotEqual(second_opts["ControlPath"], "none")

        # Keep the constructed argv in the failure message so a reviewer can
        # read the actual options rather than a description.
        for label, first_opts, second_opts, first_cmd, second_cmd in observed:
            self.assertNotEqual(
                first_opts["ControlMaster"],
                second_opts["ControlMaster"],
                f"{label}: first={first_cmd!r} second={second_cmd!r}",
            )

    def test_explicit_ssh_mux_overrides_process_env_in_both_directions(self) -> None:
        env_zero_shared = _option_map(self._cmd(self.shared, "0"))
        env_one_independent = _option_map(self._cmd(self.independent, "1"))
        self.assertEqual(env_zero_shared["ControlMaster"], "auto")
        self.assertEqual(env_zero_shared["ControlPersist"], "120")
        self.assertIn("%C-", env_zero_shared["ControlPath"])
        self.assertEqual(env_one_independent["ControlMaster"], "no")
        self.assertEqual(env_one_independent["ControlPath"], "none")
        self.assertEqual(env_one_independent["ControlPersist"], "no")

    def test_unset_ssh_mux_still_follows_process_env_default(self) -> None:
        unset = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")
        unset_default = _option_map(self._cmd(unset, None))
        unset_zero = _option_map(self._cmd(unset, "0"))
        self.assertEqual(unset_default["ControlMaster"], "auto")
        self.assertEqual(unset_zero["ControlMaster"], "no")
        self.assertEqual(unset_zero["ControlPath"], "none")
        self.assertEqual(unset_zero["ControlPersist"], "no")


class KeepaliveMechanismTests(unittest.TestCase):
    """Gap 2: ServerAlive is the keepalive mechanism, applied only when asked."""

    def _cmd(self, endpoint: Endpoint) -> list[str]:
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                return ssh_transport.ssh_base_cmd(endpoint)

    def test_keepalive_flag_adds_server_alive(self) -> None:
        cmd = self._cmd(Endpoint(host="192.0.2.10", port=46000, ssh_mux=False, keepalive=True))
        options = _option_map(cmd)
        self.assertEqual(options["ServerAliveInterval"], "30")
        self.assertEqual(options["ServerAliveCountMax"], "10")
        self.assertEqual(options["ControlMaster"], "no")
        self.assertEqual(options["ControlPath"], "none")
        self.assertEqual(options["ControlPersist"], "no")

    def test_default_endpoint_has_no_server_alive(self) -> None:
        cmd = self._cmd(Endpoint(host="192.0.2.10", port=46000))
        options = _option_map(cmd)
        self.assertNotIn("ServerAliveInterval", options)
        self.assertNotIn("ServerAliveCountMax", options)
        self.assertFalse(any(item.startswith("ServerAlive") for item in cmd))

    @unittest.skipIf(os.name == "nt", "native Windows has no ControlMaster; see NativeWindowsMuxTests")
    def test_keepalive_mechanism_can_appear_on_ssh_base_cmd_for_either_mux(self) -> None:
        # keepalive is a TCP-probe flag and stays orthogonal on ssh_base_cmd.
        # Attached streams still refuse the muxed combination below.
        muxed = _option_map(self._cmd(Endpoint(host="192.0.2.10", port=46000, ssh_mux=True, keepalive=True)))
        self.assertEqual(muxed["ControlMaster"], "auto")
        self.assertEqual(muxed["ServerAliveInterval"], "30")
        independent = _option_map(self._cmd(Endpoint(host="192.0.2.10", port=46000, ssh_mux=False, keepalive=False)))
        self.assertEqual(independent["ControlMaster"], "no")
        self.assertNotIn("ServerAliveInterval", independent)


class OpensshConfigParseTests(unittest.TestCase):
    """Real ``ssh -G`` must see options; a fake ssh on PATH cannot catch D1."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ssh = _require_openssh()

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def parse(self, argv: list[str]) -> tuple[dict[str, str], str]:
        return _openssh_G(argv, ssh=self.ssh, home=self.home.name)

    def test_openssh_G_parses_forward_argv(self) -> None:
        endpoint = Endpoint.for_long_stream("192.0.2.10", 46000)
        argv = ssh_transport.local_forward_ssh_command(
            endpoint,
            local_host="127.0.0.1",
            local_port=47001,
            remote_host="127.0.0.1",
            remote_port=8000,
        )
        self.assertEqual(_tokens_after_host(argv), [])
        parsed, stdout = self.parse(argv)
        self.assertEqual(parsed.get("exitonforwardfailure", "").lower(), "yes", stdout)
        localforward = parsed.get("localforward", "")
        self.assertIn("47001", localforward, stdout)
        self.assertIn("8000", localforward, stdout)
        self.assertIn("127.0.0.1", localforward, stdout)
        self.assertEqual(parsed.get("sessiontype", "").lower(), "none", stdout)
        self.assertIn(parsed.get("controlmaster", "").lower(), {"false", "no"}, stdout)
        self.assertEqual(parsed.get("serveraliveinterval"), "30", stdout)
        self.assertEqual(parsed.get("serveralivecountmax"), "10", stdout)

    def test_openssh_G_parses_interactive_argv(self) -> None:
        endpoint = Endpoint(host="192.0.2.10", port=22, user="ubuntu", ssh_mux=False)
        argv = ssh_transport.interactive_ssh_command(endpoint, ["sh", "-c", "true"])
        self.assertEqual(_tokens_after_host(argv), ["sh", "-c", "true"])
        parsed, stdout = self.parse(argv)
        self.assertEqual(parsed.get("batchmode", "").lower(), "no", stdout)
        self.assertEqual(
            parsed.get("preferredauthentications"),
            "password,keyboard-interactive",
            stdout,
        )
        self.assertIn(parsed.get("pubkeyauthentication", "").lower(), {"false", "no"}, stdout)
        self.assertIn(parsed.get("controlmaster", "").lower(), {"false", "no"}, stdout)
        self.assertEqual(parsed.get("numberofpasswordprompts"), "1", stdout)

    def test_openssh_G_parses_for_long_stream_argv(self) -> None:
        endpoint = Endpoint.for_long_stream("192.0.2.10", 46000)
        argv = ssh_transport.ssh_base_cmd(endpoint)
        self.assertEqual(_tokens_after_host(argv), [])
        parsed, stdout = self.parse(argv)
        self.assertEqual(parsed.get("batchmode", "").lower(), "yes", stdout)
        self.assertIn(parsed.get("controlmaster", "").lower(), {"false", "no"}, stdout)
        self.assertEqual(parsed.get("controlpath", "none").lower(), "none", stdout)
        self.assertIn(parsed.get("controlpersist", "").lower(), {"no", "0", "false"}, stdout)
        self.assertEqual(parsed.get("serveraliveinterval"), "30", stdout)
        self.assertEqual(parsed.get("serveralivecountmax"), "10", stdout)
        self.assertNotEqual(parsed.get("sessiontype", "").lower(), "none", stdout)
        self.assertNotEqual(parsed.get("exitonforwardfailure", "").lower(), "yes", stdout)

    def test_openssh_G_ssh_base_cmd_callers_do_not_smuggle_options_past_destination(self) -> None:
        endpoint = Endpoint.for_long_stream("192.0.2.10", 46000)
        composers = {
            "ssh_base_cmd": ssh_transport.ssh_base_cmd(endpoint),
            "stream_ssh_command": ssh_transport.stream_ssh_command(endpoint, "analyze", timeout_ms=120000),
            "run_script": [*ssh_transport.ssh_base_cmd(endpoint), "bash", "-s"],
            "run_bytes": [*ssh_transport.ssh_base_cmd(endpoint), "bash -c true"],
            "run_remote_python": [*ssh_transport.ssh_base_cmd(endpoint), "python3 -c pass"],
            "local_forward": ssh_transport.local_forward_ssh_command(
                endpoint,
                local_host="127.0.0.1",
                local_port=47001,
                remote_host="127.0.0.1",
                remote_port=8000,
            ),
        }
        option_flags = {"-o", "-N", "-L", "-i", "-l", "-p", "-F", "-G"}
        for name, argv in composers.items():
            with self.subTest(composer=name):
                after = _tokens_after_host(argv)
                smuggled = [token for token in after if token in option_flags or token.startswith("-o")]
                self.assertEqual(smuggled, [], f"{name} put option tokens after the host: {argv!r}")
                parsed, stdout = self.parse(argv)
                self.assertIn(parsed.get("controlmaster", "").lower(), {"false", "no"}, stdout)
                self.assertEqual(parsed.get("serveraliveinterval"), "30", stdout)
                if name == "local_forward":
                    self.assertEqual(parsed.get("sessiontype", "").lower(), "none", stdout)
                    self.assertEqual(_tokens_after_host(argv), [])
                else:
                    self.assertNotEqual(parsed.get("sessiontype", "").lower(), "none", stdout)


if __name__ == "__main__":
    unittest.main()
