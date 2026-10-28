from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core import ssh_transport


class WindowsSshSelectionTests(unittest.TestCase):
    def test_known_git_layouts_select_bundled_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Git with spaces"
            ssh = root / "usr" / "bin" / "ssh.exe"
            ssh.parent.mkdir(parents=True)
            ssh.touch()
            for layout in (
                "cmd", "bin", "mingw64/bin", "mingw32/bin", "ucrt64/bin", "usr/bin", "CMD",
            ):
                with self.subTest(layout=layout):
                    git = root / layout / "git.exe"
                    git.parent.mkdir(parents=True, exist_ok=True)
                    git.touch()
                    with mock.patch.object(ssh_transport.shutil, "which", return_value=str(git)) as find:
                        self.assertEqual(ssh_transport._windows_ssh_executable(), str(ssh.resolve()))
                    find.assert_called_once_with("git")

    def test_missing_or_unsupported_git_fails_before_owned_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_ssh = root / "Git" / "cmd" / "git.exe"
            missing_ssh.parent.mkdir(parents=True)
            missing_ssh.touch()
            unsupported = root / "wrapper" / "git.exe"
            unsupported.parent.mkdir()
            unsupported.touch()
            for git in (None, str(missing_ssh), str(unsupported), str(root / "absent.exe")):
                with self.subTest(git=git), mock.patch.object(
                    ssh_transport, "_native_windows_ssh_client", return_value=True
                ), mock.patch.object(
                    ssh_transport.shutil, "which", return_value=git
                ), mock.patch.object(ssh_transport, "OwnedProcess") as spawn:
                    with self.assertRaises(RemoteExecutionError) as caught:
                        ssh_transport.run_script(
                            Endpoint("example.invalid", 22, ssh_mux=False), "printf never"
                        )
                    self.assertEqual(caught.exception.category, "configuration")
                    self.assertEqual(caught.exception.submission_state, "not_sent")
                    self.assertFalse(caught.exception.retryable)
                    self.assertIn("Git for Windows", str(caught.exception))
                    spawn.assert_not_called()

    def test_resolution_error_is_a_configuration_error(self):
        with mock.patch.object(ssh_transport.shutil, "which", return_value="git.exe"), mock.patch.object(
            ssh_transport.Path, "resolve", side_effect=RuntimeError("symlink loop")
        ):
            with self.assertRaises(RemoteExecutionError) as caught:
                ssh_transport._windows_ssh_executable()
        self.assertEqual(caught.exception.category, "configuration")
        self.assertEqual(caught.exception.submission_state, "not_sent")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_posix_preserves_path_ssh_without_git_discovery(self):
        with mock.patch.object(
            ssh_transport, "_native_windows_ssh_client", return_value=False
        ), mock.patch.object(ssh_transport.shutil, "which") as find:
            self.assertEqual(ssh_transport._ssh_executable(), "ssh")
        find.assert_not_called()

    def test_all_command_builders_use_selected_windows_ssh(self):
        selected = "C:/Git with spaces/usr/bin/ssh.exe"
        endpoint = Endpoint("example.invalid", 22, ssh_mux=False, keepalive=True)
        with mock.patch.object(
            ssh_transport, "_native_windows_ssh_client", return_value=True
        ), mock.patch.object(ssh_transport, "_windows_ssh_executable", return_value=selected):
            commands = [
                ssh_transport.ssh_base_cmd(endpoint),
                ssh_transport.ssh_command(endpoint, "bash", "-s"),
                ssh_transport.interactive_ssh_command(endpoint, ["true"]),
                ssh_transport.local_forward_ssh_command(
                    endpoint, local_host="127.0.0.1", local_port=47001,
                    remote_host="127.0.0.1", remote_port=8000,
                ),
            ]
        for command in commands:
            self.assertEqual(command[0], selected)
            self.assertIn("ControlMaster=no", command)


if __name__ == "__main__":
    unittest.main()
