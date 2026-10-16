"""Opt-in OpenSSH loopback acceptance; no external hosts or user SSH files.

The CI job runs an unprivileged sshd with generated keys and a temporary HOME.
This supplements Linux process tests; it does not establish NPU acceptance.
"""
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest

from remote_dev.core.endpoint import Endpoint
from remote_dev.core import rpc_transport, ssh_transport, artifact_ops, job_ops

pytestmark = pytest.mark.skipif(
    os.environ.get('REMOTE_DEV_REAL_SSH') != '1', reason='explicit isolated OpenSSH acceptance only')


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    sshd = shutil.which('sshd') or '/usr/sbin/sshd'
    assert Path(sshd).is_file(), 'OpenSSH server is required for the acceptance job'
    for name in ('host', 'client'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(tmp_path / name)], check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = tmp_path / 'sshd_config'
    config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {tmp_path / 'host'}
PidFile {tmp_path / 'sshd.pid'}
AuthorizedKeysFile {tmp_path / 'client.pub'}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
AllowUsers {getpass.getuser()}
LogLevel ERROR
''')
    # ssh always uses passwd's HOME. A test-only executable wrapper supplies
    # only isolated client config/known_hosts; the real transport and ssh run.
    real_ssh = shutil.which('ssh')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    wrapper = bindir / 'ssh'
    import shlex
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(real_ssh) + ' -F /dev/null -o UserKnownHostsFile=' +
                       shlex.quote(str(tmp_path / 'known_hosts')) + ' "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path / 'client-state'))
    workspace = tmp_path / 'remote-work'
    workspace.mkdir()
    log = (tmp_path / 'sshd.log').open('wb')
    server = subprocess.Popen([sshd, '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 10
        while True:
            assert server.poll() is None, (tmp_path / 'sshd.log').read_text()
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, 'sshd did not listen'
                time.sleep(.02)
        yield Endpoint('127.0.0.1', port, user=getpass.getuser(), root=str(workspace), cwd=str(workspace),
                       identity_file=str(tmp_path / 'client'), ssh_mux=False, runtime_env=False)
    finally:
        rpc_transport.close_connections()
        server.terminate()
        server.wait(timeout=5)
        log.close()


def test_real_rpc_and_binary_artifact_roundtrip(endpoint, tmp_path):
    result = ssh_transport.run_remote_python(endpoint,
        'import json; print(json.dumps({"status":"ok","value":"真实 SSH"}))', {})
    assert result == {'status': 'ok', 'value': '真实 SSH'}
    raw = bytes(range(256)) * 8192
    source = tmp_path / 'binary.bin'
    source.write_bytes(raw)
    pushed = artifact_ops.remote_artifact_push(endpoint, local_path=str(source), remote_path='binary.bin')['result']
    assert pushed['outcome'] == 'success', pushed
    target = tmp_path / 'pulled'
    pulled = artifact_ops.remote_artifact_pull(endpoint, remote_path='binary.bin', local_dir=str(target))['result']
    assert pulled['outcome'] == 'success', pulled
    copied = [path for path in target.rglob('*') if path.is_file() and path.read_bytes() == raw]
    assert len(copied) == 1
    assert hashlib.sha256(copied[0].read_bytes()).digest() == hashlib.sha256(raw).digest()


def test_durable_job_survives_rpc_disconnect_and_drains_descendants(endpoint):
    job_id = 'job-real-ssh-001'
    command = "sleep 600 & child=$!; printf '%s' \"$child\" > child.pid; wait"
    started = job_ops.start_remote_job(endpoint, command=command, job_id=job_id, yield_time_ms=100)['result']
    assert started['status'] == 'running', started
    try:
        rpc_transport.close_connections()
        observed = job_ops.remote_job_status(endpoint, job_id=job_id)['result']
        assert observed['status'] == 'running', observed
        stopped = job_ops.remote_job_stop(endpoint, job_id=job_id, force=True)['result']
        assert stopped['outcome'] in {'success', 'cancelled'}, stopped
        pid = int((Path(endpoint.root) / 'child.pid').read_text())
        assert not Path('/proc', str(pid)).exists(), 'owned descendant remains after confirmed drain'
    finally:
        job_ops.remote_job_stop(endpoint, job_id=job_id, force=True)


def test_lost_ssh_after_write_is_unknown_and_never_replayed(endpoint):
    marker = Path(endpoint.root) / 'one-write'
    source = ('from pathlib import Path; import time,json; '
              f'p=Path({str(marker)!r}); p.open("a").write("once\\n"); '
              'time.sleep(600); print(json.dumps({"status":"ok"}))')
    observed = []
    def invoke():
        try:
            observed.append(ssh_transport.run_remote_python(endpoint, source, {'_mutation': True}))
        except Exception as exc:
            observed.append(exc)
    thread = threading.Thread(target=invoke)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert time.monotonic() < deadline, 'remote write did not start'
            time.sleep(.02)
        with rpc_transport._pool_lock:
            connections = [entry.connection for entry in rpc_transport._pool.values() if entry.connection]
        assert len(connections) == 1
        connections[0].proc.kill()
        thread.join(10)
        assert not thread.is_alive(), 'known channel loss remained an unbounded wait'
        assert len(observed) == 1 and isinstance(observed[0], dict), observed
        result = observed[0]
        assert result['status'] == 'failed', result
        assert result['remote_outcome'] == 'unknown', result
        assert result['error_details']['submission_state'] == 'uncertain', result
        assert result['error_details']['retryable'] is False, result
        assert marker.read_text() == 'once\n'
    finally:
        rpc_transport.close_connections()
        thread.join(10)
