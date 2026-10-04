"""Execution receipts survive local faults; lost channels retain their evidence."""
import json
import sys
import threading
from unittest import mock

import pytest

from remote_dev.core import job_ops, rpc_transport as rpc, ssh_transport
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError, error_details
from remote_dev.core.state_store import job_record_path


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path / 'state'))
    return Endpoint(host='example.invalid', port=22, root='/srv/app', cwd='/srv/app')


def row(**changes):
    return {'state': 'succeeded', 'quiet': True, 'result': {'state': 'succeeded', 'exit_code': 0},
            'stdout': 'completed output', 'stderr': 'command warning', 'stdout_offset': 16,
            'stderr_offset': 15, 'accepted': True, 'written': 4, 'written_chars': 4, **changes}


@pytest.mark.parametrize('remote', [row(), row(state='failed', result={'state': 'failed', 'exit_code': 17})])
def test_completed_launch_keeps_receipt_when_local_output_commit_fails(endpoint, remote):
    with mock.patch.object(job_ops, 'control', return_value=remote) as control, mock.patch.object(
            job_ops, '_save_output', side_effect=OSError('isolated disk failure')):
        payload = job_ops.start_remote_job(endpoint, command='test command', job_id='job-receipt-001', wait=True)
    result = payload['result']
    assert control.call_count == 1
    assert result['status'] == 'local_recording_failed'
    assert result['exit_code'] == remote['result']['exit_code']
    assert result['preview'] == {'stdout': remote['stdout'], 'stderr': remote['stderr']}
    assert result['error_details']['operation_completed']
    assert result['error_details']['submission_state'] == 'acknowledged'
    assert 'Do not relaunch' in payload['text']
    assert json.loads(job_record_path(endpoint, 'job-receipt-001').read_text())['authorization']


def test_stdin_ack_is_not_discarded_or_replayed_when_local_save_fails(endpoint):
    with mock.patch.object(job_ops, 'control', return_value=row(state='running', quiet=False, result=None)):
        job_ops.start_remote_job(endpoint, command='test command', job_id='job-receipt-002')
    before = job_record_path(endpoint, 'job-receipt-002').read_bytes()
    with mock.patch.object(job_ops, 'control', return_value=row()) as control, mock.patch.object(
            job_ops, 'atomic_write_json', side_effect=OSError('isolated receipt failure')):
        result = job_ops.remote_job_stdin(endpoint, job_id='job-receipt-002', chars='data', yield_time_ms=0)['result']
    assert control.call_count == 1
    assert result['stdin']['written_chars'] == 4
    assert result['exit_code'] == 0
    assert result['local_recording']['status'] == 'failed'
    assert job_record_path(endpoint, 'job-receipt-002').read_bytes() == before


@pytest.mark.parametrize('state', ['uncertain', 'lost_outcome', 'unknown'])
def test_foreground_unknown_job_returns_once_with_original_identity(endpoint, state):
    with mock.patch.object(job_ops, 'control', return_value=row(state=state, quiet=False, result=None)) as control:
        result = job_ops.start_remote_job(endpoint, command='test', job_id='job-unknown-001', wait=True)['result']
    assert control.call_count == 1
    assert result['outcome'] == 'failed'
    assert result['error_details']['submission_state'] == 'uncertain'
    assert result['session_id'] == 'job-unknown-001'


def test_rpc_disconnect_retains_exit_and_stderr_without_replaying(endpoint):
    rpc.close_connections()
    source = "import sys; print('{\"id\":0,\"ready\":true}', flush=True); sys.stdin.readline(); sys.stderr.write('original SSH failure 中文\\n'); sys.stderr.flush(); raise SystemExit(27)"
    with mock.patch.object(ssh_transport, 'ssh_command', return_value=[sys.executable, '-u', '-c', source]):
        try:
            with pytest.raises(RemoteExecutionError) as error:
                rpc.request(endpoint, 'control', 'unused code', {})
            details = error_details(error.value)
            assert details['submission_state'] == 'uncertain'
            assert details['exit_code'] == 27
            assert details['stderr_tail'] == 'original SSH failure 中文\n'
            assert 'original SSH failure' in str(error.value)
        finally:
            rpc.close_connections()


def test_pool_close_attempts_every_owned_connection_and_aggregates_failures():
    rpc.close_connections()
    first, second = mock.Mock(), mock.Mock()
    first.close.side_effect = OSError('first close failure')
    second.close.side_effect = OSError('second close failure')
    with rpc._pool_lock:
        rpc._pool['one'] = rpc._Entry(connection=first)
        rpc._pool['two'] = rpc._Entry(connection=second)
    with pytest.raises(RemoteExecutionError) as error:
        rpc.close_connections()
    assert first.close.call_count == second.close.call_count == 1
    assert error.value.category == 'cleanup'
    assert 'first close failure' in error.value.cleanup_error
    assert 'second close failure' in error.value.cleanup_error
    assert not rpc._pool


def test_binary_capture_owns_descendants_after_ssh_parent_exit(tmp_path):
    from test_local_process import tree_command, assert_tree_stopped
    result = ssh_transport._capture_command(tree_command(tmp_path, True))
    assert result.returncode == 0
    assert b'parent ready' in result.stdout
    assert result.cleanup_error is None
    assert_tree_stopped(tmp_path)


def test_binary_capture_cancellation_ends_silent_process_without_business_deadline():
    import time
    from concurrent.futures import ThreadPoolExecutor
    from remote_dev.core.cancellation import request_context
    cancelled = threading.Event()
    entered = threading.Event()
    def call():
        with request_context(cancelled):
            entered.set()
            return ssh_transport._capture_command([sys.executable, '-c', 'import time; time.sleep(10)'])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call)
        assert entered.wait(1)
        time.sleep(.15)
        assert not future.done()
        cancelled.set()
        result = future.result(timeout=2)
    assert result.cancelled and not result.timed_out
    assert result.returncode is None  # Local kill cannot acknowledge remote exit.


def test_binary_capture_keeps_completed_result_when_cleanup_fails():
    from remote_dev.core.local_process import OwnedProcess
    stop = OwnedProcess.stop
    def fail_cleanup(self, **kwargs):
        stop(self, **kwargs)
        raise OSError('cleanup fixture')
    with mock.patch.object(OwnedProcess, 'stop', fail_cleanup):
        result = ssh_transport._capture_command([sys.executable, '-c', "print('completed'); raise SystemExit(9)"])
    assert result.returncode == 9 and result.stdout == b'completed\n'
    assert 'cleanup fixture' in result.cleanup_error


def test_write_result_and_hash_survive_local_ledger_failure(endpoint):
    from remote_dev.core import file_ops
    data = {'status': 'written', 'file': {'path': '/srv/app/result', 'sha256': 'a' * 64, 'size': 7}}
    with mock.patch.object(file_ops, 'run_remote_python', return_value=data) as execute, mock.patch.object(
            file_ops, 'write_read_ledger', side_effect=OSError('ledger disk failure')):
        result = file_ops.remote_write(endpoint, file_path='result', content='created')['result']
    assert execute.call_count == 1
    assert result['status'] == 'local_recording_failed'
    assert result['execution_status'] == 'written' and result['operation_completed']
    assert result['changed_files'][0]['after_sha256'] == 'a' * 64


def test_mutation_crash_keeps_remote_stderr_and_unknown_effect(endpoint):
    from remote_dev.core import file_ops
    from remote_dev.result import tool_text
    with mock.patch.object(rpc, 'request', return_value={'returncode': 7, 'stdout': '', 'stderr': 'failure after file replace'}):
        payload = file_ops.remote_write(endpoint, file_path='result', content='created')
    assert payload['result']['error_details']['submission_state'] == 'uncertain'
    assert payload['result']['error_details']['exit_code'] == 7
    assert 'failure after file replace' in tool_text(payload)


def test_patch_receipt_failure_preserves_changed_files_and_original_failure(endpoint):
    from remote_dev.core import patch_ops
    for status in ('applied', 'commit_failed'):
        data = {'status': status, 'changed_files': [{'path': '/srv/app/a'}], 'error': 'original remote failure' if status != 'applied' else None,
                'rollback_status': {'restored': [], 'failed': [{'path': '/srv/app/a', 'error': 'restore failed'}]}}
        with mock.patch.object(patch_ops, 'atomic_write_json', side_effect=OSError('receipt failure')):
            result = patch_ops._patch_result(endpoint, 'start', 0, '/srv/app', data)['result']
        assert result['execution_status'] == status
        assert result['error'] == data['error']
        assert result['rollback_status'] == data['rollback_status']
        assert result['changed_files'] == data['changed_files']


def test_bad_job_and_endpoint_records_are_not_omitted(endpoint):
    from remote_dev.core import state_store
    path = job_record_path(endpoint, 'job-bad-001')
    path.write_text('[]')
    with pytest.raises(ValueError, match='invalid job record'):
        state_store.list_job_records(endpoint.endpoint_id)
    with pytest.raises(ValueError, match='invalid job record'):
        state_store.find_job_record('job-bad-001')
    endpoint_path = path.parents[1] / 'endpoint.json'
    endpoint_path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        state_store.list_endpoint_records()
