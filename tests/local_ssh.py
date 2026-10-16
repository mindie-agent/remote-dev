"""Test-only argv adapter for the shipped stdio Python protocols."""
import contextlib
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock
from remote_dev.core import ssh_transport


@contextlib.contextmanager
def local_python_ssh():
    original = subprocess.Popen
    def rewrite(argv):
        if len(argv) >= 7 and Path(argv[2]).name == '_posix_owner.py':
            return [*argv[:6], *rewrite(argv[6:])]
        if argv[0] == "test-ssh-python":
            command = shlex.split(argv[-1])
            if command[0] != "python3":
                raise AssertionError("test adapter requires python3 command")
            argv = [sys.executable, *command[1:]]
        return argv
    def launch(argv, **kwargs):
        argv = rewrite(argv)
        return original(argv, **kwargs)
    with mock.patch.object(ssh_transport, "ssh_base_cmd", return_value=["test-ssh-python"]), \
         mock.patch.object(subprocess, "Popen", side_effect=launch):
        yield
