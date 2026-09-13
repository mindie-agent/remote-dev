"""Native OS process boundaries; the same tests execute on Windows/macOS/Linux."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.local_process import OwnedProcess
from remote_dev.core import local_process, ssh_transport
from test_ssh_transport import _windows_pid_alive


TREE_SCRIPT = '''
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
role, parent_exit = sys.argv[2:]
if role == 'leaf':
    sys.stderr.buffer.write('leaf ready 中文\\n'.encode('utf-8')); sys.stderr.flush()
    (root / 'leaf.pid').write_text(str(os.getpid()))
elif role == 'branch':
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, __file__, str(root), 'leaf', parent_exit])
    while not (root / 'leaf.pid').exists(): time.sleep(.01)
    (root / 'branch.pid').write_text(str(os.getpid()))
else:
    subprocess.Popen([sys.executable, __file__, str(root), 'branch', parent_exit])
    while not (root / 'branch.pid').exists(): time.sleep(.01)
    sys.stdout.buffer.write(b'parent ready\\n'); sys.stdout.flush()
    (root / 'ready').write_text('ready')
    if parent_exit == 'yes': sys.exit(0)
time.sleep(12)
'''


def tree_command(tmp_path: Path, parent_exit: bool) -> list[str]:
    script = tmp_path / 'process tree.py'
    script.write_text(TREE_SCRIPT, encoding='utf-8')
    return [sys.executable, str(script), str(tmp_path), 'root', 'yes' if parent_exit else 'no']


def wait_ready(tmp_path: Path) -> None:
    deadline = time.monotonic() + 5
    while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (tmp_path / 'ready').exists(), 'process tree did not start'


def live(pid: int) -> bool:
    if os.name == 'nt':
        return _windows_pid_alive(pid)
    # A killed orphan can remain a zombie until the host init reaps it.
    # ps is available on both macOS and Linux; a zombie is not a live process.
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                           capture_output=True, text=True, check=False).stdout.strip()
    return bool(state and not state.startswith('Z'))


def assert_tree_stopped(tmp_path: Path) -> None:
    ids = [int((tmp_path / (name + '.pid')).read_text()) for name in ('branch', 'leaf')]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(live(pid) for pid in ids):
            return
        time.sleep(.02)
    assert not any(live(pid) for pid in ids), 'owned descendant survived stop'


def test_literal_argv_unicode_cwd_env_and_binary_pipes(tmp_path):
    original_env = os.environ.get('LOCAL_PROCESS_VALUE')
    cwd = tmp_path / "中文 path ' $dollar"
    cwd.mkdir()
    arguments = ["literal ' quote", '$not_expanded', '中文', 'semi;colon', 'back\\slash']
    payload = bytes(range(256)) * 2048
    code = (
        "import json,os,sys; "
        "print(json.dumps([os.getcwd(),sys.argv[1:],os.environ['LOCAL_PROCESS_VALUE']])); "
        "sys.stdout.flush(); data=sys.stdin.buffer.read(); "
        "sys.stderr.buffer.write(data[::-1]);sys.stderr.flush(); "
        "sys.stdout.buffer.write(data)"
    )
    with OwnedProcess([sys.executable, '-c', code, *arguments], cwd=cwd,
                      env={'LOCAL_PROCESS_VALUE': '值 $literal'}, stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as owner:
        stdout, stderr = owner.process.communicate(payload, timeout=5)
        assert owner.process.returncode == 0
    metadata, binary = stdout.split(b'\n', 1)
    actual_cwd, actual_args, actual_env = json.loads(metadata)
    assert Path(actual_cwd) == cwd
    assert actual_args == arguments
    assert actual_env == '值 $literal'
    assert binary == payload
    assert stderr == payload[::-1]
    assert os.environ.get('LOCAL_PROCESS_VALUE') == original_env


@pytest.mark.parametrize('parent_exit', [False, True])
def test_owned_stop_ends_grandchildren_and_inherited_pipes(tmp_path, parent_exit):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(15)'])
    try:
        with OwnedProcess(tree_command(tmp_path, parent_exit), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as owner:
            wait_ready(tmp_path)
            if parent_exit:
                assert owner.process.wait(timeout=3) == 0
            started = time.monotonic()
            owner.stop(force=False, timeout=.3)
            stdout, stderr = owner.process.communicate(timeout=2)
            assert time.monotonic() - started < 3
            assert b'parent ready' in stdout
            assert 'leaf ready 中文'.encode('utf-8') in stderr
            assert_tree_stopped(tmp_path)
            assert unrelated.poll() is None
            owner.stop()  # A repeated close cannot target a reused group id.
    finally:
        unrelated.kill()
        unrelated.wait(timeout=3)


@pytest.mark.parametrize('parent_exit', [False, True])
def test_attached_stream_stops_tree_on_timeout_or_parent_exit(tmp_path, parent_exit):
    command = tree_command(tmp_path, parent_exit)
    started = time.monotonic()
    with mock.patch.object(ssh_transport, 'stream_ssh_command', return_value=command):
        result = ssh_transport.run_stream(Endpoint.for_long_stream('192.0.2.1', 22),
                                         '#' * 1000000, merge_stderr=False,
                                         timeout_ms=None if parent_exit else 1200)
    assert time.monotonic() - started < 4
    assert result.timed_out is (not parent_exit)
    assert result.returncode == (0 if parent_exit else None)
    assert result.stdout == 'parent ready\n'
    assert 'leaf ready 中文\n' in result.stderr
    assert_tree_stopped(tmp_path)


def test_forward_close_after_parent_exit_drains_child_pipe(tmp_path):
    with mock.patch.object(ssh_transport, 'local_forward_ssh_command',
                           return_value=tree_command(tmp_path, True)):
        forward = ssh_transport.open_local_forward(Endpoint.for_long_stream('192.0.2.1', 22),
                                                  8000, ready_timeout_s=None)
    try:
        wait_ready(tmp_path)
        assert forward._proc.wait(timeout=3) == 0
        started = time.monotonic()
        result = forward.close()
        assert time.monotonic() - started < 3
        assert result.returncode != 0
        assert 'leaf ready 中文\n' in result.stderr
        assert_tree_stopped(tmp_path)
    finally:
        forward.close()


def test_forward_startup_failure_stops_children_before_reading_stderr(tmp_path):
    with mock.patch.object(ssh_transport, 'local_forward_ssh_command',
                           return_value=tree_command(tmp_path, True)):
        started = time.monotonic()
        with pytest.raises(ssh_transport.RemoteExecutionError, match='exited early'):
            ssh_transport.open_local_forward(Endpoint.for_long_stream('192.0.2.1', 22),
                                             8000, ready_timeout_s=3)
    assert time.monotonic() - started < 4
    assert_tree_stopped(tmp_path)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group identity')
def test_reused_reaped_parent_pid_is_never_signalled():
    with OwnedProcess([sys.executable, '-c', 'pass'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL) as owner:
        assert owner.process.wait(timeout=3) == 0
        # Deterministically inject PID reuse; forcing real OS PID recycling
        # would be slow and could endanger unrelated processes on the runner.
        with mock.patch.object(os, 'getpgid', return_value=owner.process.pid), \
                mock.patch.object(os, 'killpg') as signal_group:
            assert owner.stop(force=False) == 0
        signal_group.assert_not_called()


@pytest.mark.parametrize('failure', ['job', 'wait'])
def test_windows_job_handle_closes_when_stop_or_wait_fails(failure):
    owner = OwnedProcess.__new__(OwnedProcess)
    owner._closed = False
    owner._job = mock.Mock()
    owner.process = mock.Mock(returncode=None)
    operation = owner._job.stop if failure == 'job' else owner.process.wait
    operation.side_effect = TimeoutError('injected local stop failure')
    with pytest.raises(TimeoutError, match='injected local stop failure'):
        owner.stop()
    owner._job.close.assert_called_once_with()
    # Finally/context cleanup cannot retry a closed handle and mask the error.
    owner.stop()
    owner._job.close.assert_called_once_with()


@pytest.mark.parametrize('listing,returncode', [('42 Z\n42 Z+\n', 0), ('', 1)])
def test_darwin_group_permission_error_accepts_only_observed_exited_group(listing, returncode):
    owner = OwnedProcess.__new__(OwnedProcess)
    owner.process = SimpleNamespace(pid=42, returncode=None)
    original = PermissionError(errno.EPERM, 'fixture group error')
    with mock.patch.object(local_process.sys, 'platform', 'darwin'), \
            mock.patch.object(local_process.os, 'killpg', side_effect=original, create=True), \
            mock.patch.object(local_process.subprocess, 'run', return_value=SimpleNamespace(returncode=returncode, stdout=listing, stderr='')) as observe:
        owner._signal_group(9)
    assert observe.call_args.args[0] == ['/bin/ps', '-x', '-g', '42', '-o', 'pgid=,stat=']
    assert observe.call_args.kwargs['timeout'] == 1.0
    assert observe.call_args.kwargs['env']['COMMAND_MODE'] == 'unix2003'


@pytest.mark.parametrize('listing,returncode,stderr', [
    ('42 Z\n42 S\n', 0, ''), ('42 T\n', 0, ''), ('42 ?\n', 0, ''),
    ('unreadable\n', 0, ''), ('42\n', 0, ''), ('42 Z\n', 1, ''),
    ('99 S\n', 0, ''), ('', 0, ''), ('', 0, 'sysctl failed'), ('', 1, 'error'),
    ('', 2, ''), pytest.param('42 Z\n' * 15000, 0, '', id='oversized-listing'),
])
def test_darwin_live_or_unknown_group_preserves_original_permission_error(listing, returncode, stderr):
    owner = OwnedProcess.__new__(OwnedProcess)
    owner.process = SimpleNamespace(pid=42, returncode=None)
    original = PermissionError(errno.EPERM, 'fixture group error')
    with mock.patch.object(local_process.sys, 'platform', 'darwin'), \
            mock.patch.object(local_process.os, 'killpg', side_effect=original, create=True), \
            mock.patch.object(local_process.subprocess, 'run', return_value=SimpleNamespace(returncode=returncode, stdout=listing, stderr=stderr)):
        with pytest.raises(PermissionError) as caught:
            owner._signal_group(9)
    assert caught.value is original


@pytest.mark.parametrize('observation_error', [OSError('ps unavailable'), subprocess.TimeoutExpired('ps', 1)])
def test_darwin_group_observation_failure_cannot_hide_permission_error(observation_error):
    owner = OwnedProcess.__new__(OwnedProcess)
    owner.process = SimpleNamespace(pid=42, returncode=None)
    original = PermissionError(errno.EPERM, 'fixture group error')
    with mock.patch.object(local_process.sys, 'platform', 'darwin'), \
            mock.patch.object(local_process.os, 'killpg', side_effect=original, create=True), \
            mock.patch.object(local_process.subprocess, 'run', side_effect=observation_error):
        with pytest.raises(PermissionError) as caught:
            owner._signal_group(9)
    assert caught.value is original


@pytest.mark.parametrize('platform,error_number', [('linux', errno.EPERM), ('darwin', errno.EACCES)])
def test_other_permission_errors_do_not_use_darwin_zombie_exception(platform, error_number):
    owner = OwnedProcess.__new__(OwnedProcess)
    owner.process = SimpleNamespace(pid=42, returncode=None)
    original = PermissionError(error_number, 'fixture group error')
    with mock.patch.object(local_process.sys, 'platform', platform), \
            mock.patch.object(local_process.os, 'killpg', side_effect=original, create=True), \
            mock.patch.object(local_process.subprocess, 'run') as observe:
        with pytest.raises(PermissionError) as caught:
            owner._signal_group(9)
    assert caught.value is original
    observe.assert_not_called()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin killpg zombie-group behavior')
def test_darwin_stop_reaps_a_group_leader_that_is_already_a_zombie():
    owner = OwnedProcess([sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # Do not poll/wait Popen here: keeping our exited child unreaped
            # makes the zombie-only group deterministic instead of a timing race.
            status = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(owner.process.pid)],
                                    capture_output=True, text=True, check=True, timeout=1).stdout.strip()
            if status.startswith('Z'):
                break
            time.sleep(.01)
        else:
            pytest.fail('owned child did not become an unreaped zombie')
        assert owner.process.returncode is None
        assert owner.stop(force=False, timeout=.3) == 0
        assert owner.process.returncode == 0
    finally:
        owner.process.kill()
        owner.process.wait(timeout=3)
