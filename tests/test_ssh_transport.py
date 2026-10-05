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
    path = shutil.which("ssh")
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
    eval_cmd = [binary, "-G", "-F", config_file or os.devnull, *argv[1:]]
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
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
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

    def test_mux_zero_bypasses_helper_and_does_not_touch_mux_state(self) -> None:
        real_helper = ssh_transport._control_master_options
        for ready in (None, True, False):
            with self.subTest(mux_ready=ready):
                with mock.patch.object(ssh_transport, "_MUX_READY", ready):
                    with mock.patch.object(Path, "mkdir") as mkdir:
                        with mock.patch.object(Path, "rmdir") as rmdir:
                            with mock.patch.object(Path, "unlink") as unlink:
                                with mock.patch.object(Path, "chmod") as path_chmod:
                                    with mock.patch.object(ssh_transport.os, "chmod") as chmod:
                                        with mock.patch.object(
                                            ssh_transport,
                                            "_control_master_options",
                                            wraps=real_helper,
                                        ) as helper:
                                            with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
                                                cmd = ssh_transport.ssh_base_cmd(self.endpoint)
                                        self.assertEqual(ssh_transport._MUX_READY, ready)
                                        helper.assert_not_called()
                                        mkdir.assert_not_called()
                                        rmdir.assert_not_called()
                                        unlink.assert_not_called()
                                        path_chmod.assert_not_called()
                                        chmod.assert_not_called()
                                        options = _option_map(cmd)
                                        self.assertEqual(options["ControlMaster"], "no")
                                        self.assertEqual(options["ControlPath"], "none")
                                        self.assertEqual(options["ControlPersist"], "no")

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
            self.assertEqual(args[0], "ssh")
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


    def test_subprocess_copies_keep_independent_argv_and_parent_env(self) -> None:
        parent_before = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.environ.copy()
            shared["REMOTE_DEV_SSH_MUX_DIR"] = tmp
            zero_env = dict(shared)
            zero_env[SSH_MUX_ENV] = "0"
            one_env = dict(shared)
            one_env[SSH_MUX_ENV] = "1"
            unset_env = dict(shared)
            unset_env.pop(SSH_MUX_ENV, None)

            zero = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV],
                capture_output=True,
                text=True,
                check=False,
                env=zero_env,
            )
            one = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV],
                capture_output=True,
                text=True,
                check=False,
                env=one_env,
            )
            unset = subprocess.run(
                [sys.executable, "-c", _CHILD_SSH_ARGV],
                capture_output=True,
                text=True,
                check=False,
                env=unset_env,
            )

        self.assertEqual(dict(os.environ), parent_before)
        self.assertEqual(zero.returncode, 0, zero.stderr)
        self.assertEqual(unset.returncode, 0, unset.stderr)
        zero_options = _option_map(json.loads(zero.stdout))
        unset_options = _option_map(json.loads(unset.stdout))
        self.assertEqual(zero_options["ControlMaster"], "no")
        self.assertEqual(zero_options["ControlPath"], "none")
        self.assertEqual(zero_options["ControlPersist"], "no")
        if os.name == "nt":
            self.assertNotEqual(one.returncode, 0, one.stderr)
            self.assertIn("ControlMaster is not supported", one.stderr)
            self.assertEqual(unset_options["ControlMaster"], "no")
            self.assertEqual(unset_options["ControlPath"], "none")
            self.assertEqual(unset_options["ControlPersist"], "no")
            self.assertEqual(unset_options, zero_options)
        else:
            self.assertEqual(one.returncode, 0, one.stderr)
            one_options = _option_map(json.loads(one.stdout))
            self.assertEqual(one_options["ControlMaster"], "auto")
            self.assertEqual(one_options["ControlPersist"], "120")
            self.assertIn("%C-", one_options["ControlPath"])
            self.assertEqual(unset_options, one_options)
            self.assertNotEqual(zero_options["ControlMaster"], one_options["ControlMaster"])
        self.assertEqual(parent_before.get(SSH_MUX_ENV), os.environ.get(SSH_MUX_ENV))

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

    def test_mux_override_does_not_write_environ(self) -> None:
        before = os.environ.copy()
        with mock.patch.object(ssh_transport, "_MUX_READY", True):
            with mock.patch.dict(os.environ, {SSH_MUX_ENV: "0"}):
                ssh_transport.ssh_base_cmd(self.endpoint)
                self.assertEqual(os.environ.get(SSH_MUX_ENV), "0")
            with mock.patch.dict(os.environ):
                os.environ.pop(SSH_MUX_ENV, None)
                ssh_transport.ssh_base_cmd(self.endpoint)
                self.assertNotIn(SSH_MUX_ENV, os.environ)
        self.assertEqual(dict(os.environ), before)


class NativeWindowsMuxTests(unittest.TestCase):
    """Win32-OpenSSH has no Client ControlMaster. Ordinary argv must not ask for it."""

    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, identity_file="/keys/a")

    def _windows_cmd(self, endpoint: Endpoint, env_value: str | None = None) -> list[str]:
        with mock.patch.object(ssh_transport.os, "name", "nt"):
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
        self.assertIn("Win32-OpenSSH", str(raised_endpoint.exception))
        with self.assertRaises(RemoteExecutionError) as raised_env:
            self._windows_cmd(self.endpoint, "1")
        self.assertIn("ControlMaster is not supported", str(raised_env.exception))
        self.assertNotIn("ControlMaster=auto", str(raised_endpoint.exception))

    def test_windows_default_stream_and_interactive_stay_independent(self) -> None:
        default = Endpoint(host="192.0.2.10", port=46000)
        with mock.patch.object(ssh_transport.os, "name", "nt"):
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
                    timer = threading.Timer(0.15, cancelled.set)
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
            "import os,time;os.close(1);os.close(2);time.sleep(0.3)",
        ]
        with mock.patch.object(ssh_transport, "STREAM_SELECT_SLICE_SECONDS", 0.05):
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
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 2.0)

        with mock.patch.object(ssh_transport, "STREAM_SELECT_SLICE_SECONDS", 0.05):
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
        self.assertGreaterEqual(elapsed, 0.2)
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
                timeout_ms=200,
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
                timeout_ms=800,
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


def _rewrite_ssh_argv(args: object, fake_script: Path) -> object:
    """Keep composed ssh argv; run it through the fake as a Python process.

    Production still launches ``ssh``. Tests replace only argv[0] so Windows
    does not have to find ``ssh.exe`` or a ``.cmd`` wrapper, while ``ssh -G``
    parser tests keep using a real OpenSSH binary.
    """
    if not isinstance(args, (list, tuple)) or not args:
        return args
    argv = [str(item) for item in args]
    if len(argv) >= 7 and Path(argv[2]).name == "_posix_owner.py":
        return [*argv[:6], *_rewrite_ssh_argv(argv[6:], fake_script)]
    if Path(argv[0]).name.lower() not in {"ssh", "ssh.exe"}:
        return args
    return [sys.executable, str(fake_script), *argv[1:]]


class _ComposedSshSubprocess:
    """Test-only argv hook on ``ssh_transport.subprocess``, not a PATH wrapper."""

    def __init__(self, fake_script: Path) -> None:
        self._fake_script = Path(fake_script)

    def __getattr__(self, name: str):
        return getattr(subprocess, name)

    def Popen(self, args, **kwargs):  # noqa: N802 - match subprocess.Popen
        return subprocess.Popen(_rewrite_ssh_argv(args, self._fake_script), **kwargs)

    def run(self, args, **kwargs):
        return subprocess.run(_rewrite_ssh_argv(args, self._fake_script), **kwargs)


def _patch_composed_ssh(fake_script: Path):
    stack = ExitStack()
    adapter = _ComposedSshSubprocess(fake_script)
    stack.enter_context(mock.patch.object(ssh_transport, "subprocess", adapter))
    stack.enter_context(mock.patch.object(local_process, "subprocess", adapter))
    return stack


def _windows_pid_alive(pid: int) -> bool:
    """Owned-process liveness on native Windows (``os.kill(pid, 0)`` is not)."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    wait_timeout = 258
    wait_failed = 0xFFFFFFFF

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information | synchronize, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        if int(code.value) != still_active:
            return False
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited == wait_failed:
            return True
        return waited == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    """True while this PID is still a live process we can observe."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
        self.assertEqual(argv[0], "ssh")
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
            self.assertEqual(composed[0], "ssh")
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
                ssh_transport.open_local_forward(self.endpoint, 8123, ready_timeout_s=0.6)
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
        self.assertEqual(argv[0], "ssh")
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
        self.assertEqual(composed[0], "ssh")
        self.assertEqual(Path(recorded[0]).name, "ssh.py")
        self.assertEqual(recorded[1:], composed[1:])
        self.assertEqual(recorded[-3:], ["sh", "-c", "printf ok"])
        self.assertIn("BatchMode=no", recorded)
        self.assertIn("PubkeyAuthentication=no", recorded)


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
