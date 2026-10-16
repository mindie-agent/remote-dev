"""Container coordinates at every public boundary, without a Docker daemon."""
from dataclasses import asdict, replace
import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest import mock

import pytest

from remote_dev.core import container_endpoint as containers
from remote_dev.core import file_ops, job_ops, rpc_transport as rpc, ssh_transport as ssh, state_store
from remote_dev.core.endpoint import Endpoint, EndpointError, resolve_endpoint
from remote_dev.core.errors import RemoteExecutionError

A = 'a' * 64
B = 'b' * 64

# Public I/O and record boundaries: a named container must be resolved before
# body execution. Host-only public entries have a separate rejection matrix.
PINNED_ENTRIES = [
    ('core.file_ops', 'remote_read', (), {'file_path': 'x'}),
    ('core.file_ops', 'remote_ls', (), {}),
    ('core.file_ops', 'remote_write', (), {'file_path': 'x', 'content': 'x'}),
    ('core.file_ops', 'remote_edit', (), {'file_path': 'x', 'old_string': 'a', 'new_string': 'b'}),
    ('core.file_ops', 'remote_multi_edit', (), {'file_path': 'x', 'edits': []}),
    ('core.search_ops', 'remote_glob', (), {'pattern': '*'}),
    ('core.search_ops', 'remote_grep', (), {'pattern': 'x'}),
    ('core.patch_ops', 'remote_apply_patch', (), {'patch': 'invalid'}),
    ('core.shell_ops', 'remote_bash', (), {'command': 'true'}),
    ('core.job_ops', 'start_remote_job', (), {'command': 'true'}),
    ('core.job_ops', 'remote_job_status', (), {'job_id': 'job-existing'}),
    ('core.job_ops', 'remote_job_tail', (), {'job_id': 'job-existing'}),
    ('core.job_ops', 'remote_job_stop', (), {'job_id': 'job-existing'}),
    ('core.job_ops', 'remote_job_stdin', (), {'job_id': 'job-existing'}),
    ('core.artifact_ops', 'remote_artifact_manifest', (), {'remote_path': 'x'}),
    ('core.artifact_ops', 'remote_artifact_pull', (), {'remote_path': 'x'}),
    ('core.artifact_ops', 'remote_artifact_push', (), {'remote_path': 'x', 'local_path': 'x'}),
    ('core.context_snapshot', 'remote_probe', (), {}),
    ('core.context_snapshot', 'remote_context_snapshot', (), {'live_probe': False}),
    ('core.context_snapshot', 'write_context_snapshot', ({},), {}),
    ('core.ssh_transport', 'ssh_command', ('bash', '-s'), {}),
    ('core.ssh_transport', 'stream_ssh_command', ('true',), {}),
    ('core.ssh_transport', 'run_script', ('true',), {}),
    ('core.ssh_transport', 'run_stream', ('true',), {}),
    ('core.ssh_transport', 'run_bytes', ('true',), {}),
    ('core.ssh_transport', 'run_rpc_script', ('true',), {}),
    ('core.ssh_transport', 'run_remote_python', ('', {}), {}),
    ('core.rpc_transport', 'RpcConnection', (), {}),
    ('core.rpc_transport', 'request', ('python', '', {}), {}),
    ('core.artifact_transport', 'ArtifactStream', ('pull', 0, 12000), {}),
    ('processes.client', 'control', ('job-existing', 'stop'), {}),
    ('diagnostics', 'diagnose_ssh', (), {}),
    ('core.state_store', 'endpoint_state_dir', (), {}),
    ('core.state_store', 'ensure_endpoint_state', (), {}),
    ('core.state_store', 'new_log_dir', ('read',), {}),
    ('core.state_store', 'read_ledger_path', ('/x',), {}),
    ('core.state_store', 'write_read_ledger', ({},), {}),
    ('core.state_store', 'load_read_ledger', ('/x',), {}),
    ('core.state_store', 'load_write_ledger_guard', ('/x',), {}),
    ('core.state_store', 'job_record_path', ('job-existing',), {}),
    ('core.locking', 'mutation_lock', (), {}),
]


def endpoint(container=A, **kwargs):
    return Endpoint(host='host.example', port=22, root='/work', cwd='/work',
                    container=container, ssh_mux=False, **kwargs)


@pytest.mark.parametrize('module,name,args,kwargs', PINNED_ENTRIES,
                         ids=[row[0] + '.' + row[1] for row in PINNED_ENTRIES])
def test_public_container_entries_resolve_before_any_operation(module, name, args, kwargs, monkeypatch):
    entry = getattr(importlib.import_module('remote_dev.' + module), name)
    def reject(ep, **options):
        assert ep.container == 'missing-name'
        raise EndpointError('container resolution refused')
    monkeypatch.setattr(containers, 'pin_container_endpoint', reject)
    monkeypatch.setattr(ssh, 'pin_container_endpoint', reject)
    monkeypatch.setattr(rpc, 'pin_container_endpoint', reject)
    with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('no host fallback')), \
         mock.patch.object(subprocess, 'run', side_effect=AssertionError('no host fallback')):
        with pytest.raises(EndpointError, match='container resolution refused'):
            entry(endpoint('missing-name'), *args, **kwargs)


@pytest.mark.parametrize('selector', ['', '-x', '--privileged', 'x;y', 'x y', 'x\ny', '$(id)', '/', True, 3])
def test_bad_container_selector_is_rejected_without_transport(selector):
    with mock.patch.object(rpc, 'request', side_effect=AssertionError('must not connect')):
        with pytest.raises(EndpointError, match='container'):
            endpoint(selector)


def test_names_are_fresh_full_ids_skip_inspect_and_host_lookup_has_no_runtime_overlay():
    calls = []
    def inspect(ep, kind, code, payload, **kwargs):
        calls.append(ep)
        assert ep.container is None and ep.root == ep.cwd == '/'
        assert ep.runtime_env is False and ep.runtime_env_file is None
        assert kind == 'python' and "['docker', 'inspect'" in code
        return {'returncode': 0, 'stdout': json.dumps({'container': A if len(calls) == 1 else B})}
    with mock.patch.object(rpc, 'request', side_effect=inspect):
        original = endpoint('repro-case', runtime_env_file='/work/env.sh')
        first = containers.pin_container_endpoint(original)
        second = containers.pin_container_endpoint(original)
        assert (first.container, second.container) == (A, B)
        assert first.container_selector == 'repro-case'
        assert containers.pin_container_endpoint(first) is first
        plain = replace(first, container=None, container_selector=None)
        assert containers.pin_container_endpoint(plain) is plain
        assert len(calls) == 2
        assert first.runtime_env_file == '/work/env.sh'


@pytest.mark.parametrize('row', [
    {'returncode': 1, 'stderr': 'Error: No such container'},
    {'returncode': 1, 'stderr': 'Selected container is not running'},
    {'returncode': 0, 'stdout': '{}'},
    {'returncode': 0, 'stdout': '{"container":"short"}'},
    {'returncode': 0, 'stdout': '', 'timed_out': True},
])
def test_failed_resolution_never_executes_a_fallback(row):
    with mock.patch.object(rpc, 'request', return_value=row) as request, \
         mock.patch.object(subprocess, 'run', side_effect=AssertionError('no fallback')):
        with pytest.raises(EndpointError):
            containers.pin_container_endpoint(endpoint('missing'))
    assert request.call_count == 1


def test_inspect_failure_preserves_docker_stderr_and_does_not_replay():
    with mock.patch.object(rpc, 'request', return_value={'returncode': 1, 'stderr': 'daemon permission denied'}) as request:
        with pytest.raises(EndpointError, match='daemon permission denied'):
            ssh.ssh_command(endpoint('name'), 'bash', '-s')
        assert request.call_count == 1
    with mock.patch.object(rpc, 'request', side_effect=RemoteExecutionError('unknown submitted outcome')) as request:
        with pytest.raises(RemoteExecutionError, match='unknown submitted outcome'):
            containers.pin_container_endpoint(endpoint('name'))
        assert request.call_count == 1


def test_complete_argv_pins_container_and_preserves_bash_command_quoting():
    script = "printf '%s' 'spaces; $literal $(literal)'"
    cmd = ssh.ssh_command(endpoint(), 'bash', '-c', shlex.quote(script))
    assert shlex.split(cmd[-1]) == ['docker', 'exec', '-i', A, 'bash', '-c', script]
    assert '--user' not in cmd[-1] and '--privileged' not in cmd[-1]
    streamed = ssh.stream_ssh_command(endpoint(), script)
    assert shlex.split(streamed[-1]) == ['docker', 'exec', '-i', A, 'bash', '-c', script]
    assert shlex.split(ssh.stream_ssh_command(endpoint(), None)[-1]) == ['docker', 'exec', '-i', A, 'bash', '-s']
    assert ssh._as_long_stream(endpoint()).container == A


def test_complete_argv_cannot_expand_variables_or_execute_operators_on_host():
    command = ssh.ssh_command(endpoint(), "echo", shlex.quote('$(touch /host) $HOME ; value'))[-1]
    assert shlex.split(command) == ['docker', 'exec', '-i', A, 'echo', '$(touch /host) $HOME ; value']
    # Even unquoted shell metacharacters are literal argv here; shell programs
    # are supplied explicitly through bash -c, never interpreted by the host.
    command = ssh.ssh_command(endpoint(), 'echo ; touch /host')[-1]
    assert command.endswith("echo ';' touch /host")


@pytest.mark.parametrize('call', [
    lambda ep: ssh.ssh_base_cmd(ep),
    lambda ep: ssh.interactive_ssh_command(ep, ['echo', 'x']),
    lambda ep: ssh.run_interactive(ep, ['echo', 'x']),
    lambda ep: ssh.local_forward_ssh_command(ep, local_host='127.0.0.1', local_port=3333,
                                           remote_host='127.0.0.1', remote_port=4444),
    lambda ep: ssh.open_local_forward(ep, 4444),
])
def test_host_only_entries_reject_containers_before_any_process_or_lookup(call):
    with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('must not launch')), \
         mock.patch.object(subprocess, 'run', side_effect=AssertionError('must not launch')), \
         mock.patch.object(rpc, 'request', side_effect=AssertionError('must not inspect')):
        with pytest.raises(RemoteExecutionError, match='container'):
            call(endpoint('name'))


def test_script_and_binary_primitives_use_the_fixed_container():
    with mock.patch.object(ssh, '_capture_command', return_value=subprocess.CompletedProcess([], 0, b'out', b'err')) as run:
        assert ssh.run_script(endpoint(), 'printf out').stdout == 'out'
        assert shlex.split(run.call_args.args[0][-1]) == ['docker', 'exec', '-i', A, 'bash', '-s']
        assert run.call_args.kwargs['input'] == b'printf out'
        assert ssh.run_bytes(endpoint(), 'cat', stdin=b'\x00\xff').stdout == b'out'
        assert shlex.split(run.call_args.args[0][-1]) == ['docker', 'exec', '-i', A, 'bash', '-c', 'cat']
        assert run.call_args.kwargs['input'] == b'\x00\xff'


@pytest.mark.skipif(sys.platform != 'linux', reason='Remote scripts target Linux Bash')
def test_binary_and_stdin_scripts_keep_actual_bash_pipe_redirect_and_env_semantics(tmp_path):
    original = ssh._capture_command
    def launch(argv, **kwargs):
        command = shlex.split(argv[-1])
        assert command[:4] == ['docker', 'exec', '-i', A]
        assert command[4] == 'bash'
        return original(command[4:], **kwargs)
    output = tmp_path / 'output file'
    script = "value='original environment'; printf '%s\\n' \"$value\" | cat > " + shlex.quote(str(output)) + "; cat " + shlex.quote(str(output))
    with mock.patch.object(ssh, '_capture_command', side_effect=launch):
        result = ssh.run_bytes(endpoint(), script)
        assert result.returncode == 0 and result.stdout == b'original environment\n'
        assert output.read_text() == 'original environment\n'
        result = ssh.run_script(endpoint(), "local_value='unexported'; fn() { printf '%s' \"$local_value\"; }; fn | cat")
        assert result.returncode == 0 and result.stdout == 'unexported'


def test_read_pin_precedes_ledger_and_replacement_does_not_reuse_optional_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path))
    def read(ep, code, payload, **kwargs):
        assert ep.container == A
        return {'status': 'ok', 'file': {'path': '/work/code.py', 'sha256': 'c' * 64,
                                       'size': 4, 'mtime_ns': 7, 'content': 'code'}}
    with mock.patch.object(rpc, 'request', return_value={'returncode': 0, 'stdout': json.dumps({'container': A})}), \
         mock.patch.object(file_ops, 'run_remote_python', side_effect=read):
        result = file_ops.remote_read(endpoint('same-name'), file_path='code.py')['result']
    assert result['target']['container'] == A
    assert result['target']['container_selector'] == 'same-name'
    before = state_store.load_write_ledger_guard(endpoint(A), '/work/code.py')
    after = state_store.load_write_ledger_guard(endpoint(B), '/work/code.py')
    assert before.ledger['sha256'] == 'c' * 64
    assert after.ledger is None and after.read_required is False  # No new mandatory read gate.
    record = json.loads((state_store.endpoint_state_dir(endpoint(A)) / 'endpoint.json').read_text())
    assert record['container'] == A and record['container_selector'] == 'same-name'


def test_python_mutation_entry_pins_before_optional_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path))
    original = endpoint('name')
    seen = []
    def guard(ep, *args, **kwargs):
        seen.append(ep.container)
        return state_store.WriteLedgerGuard(None, False, 'default')
    with mock.patch.object(rpc, 'request', return_value={'returncode': 0, 'stdout': json.dumps({'container': B})}) as inspect, \
         mock.patch.object(file_ops, 'load_write_ledger_guard', side_effect=guard), \
         mock.patch.object(file_ops, 'run_remote_python', return_value={'status': 'ok'}):
        result = file_ops.remote_edit(original, file_path='code.py', old_string='a', new_string='b')['result']
    assert seen == [B] and inspect.call_count == 1
    assert result['target']['container'] == B


def test_job_restore_retains_old_id_and_rejects_name_based_records(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path))
    fixed = endpoint(A, container_selector='repro')
    seen = []
    def control(ep, identifier, action, **kwargs):
        seen.append((ep.container, action))
        return {'state': 'succeeded', 'quiet': True, 'result': {'exit_code': 0}}
    with mock.patch.object(job_ops, 'control', side_effect=control):
        result = job_ops.start_remote_job(fixed, command='true', job_id='job-old-container')['result']
        record = json.loads(Path(result['refs']['job_record']).read_text())
        assert record['connection']['container'] == A
        assert job_ops.remote_job_stop(None, job_id=result['job_id'])['result']['quiet']
    assert seen == [(A, 'launch'), (A, 'stop')]
    with pytest.raises(ValueError, match='fixed full container ID'):
        job_ops.endpoint_from_job_record({'connection': asdict(endpoint('repro'))})
    with pytest.raises(FileNotFoundError):
        job_ops.remote_job_stop(endpoint(B), job_id='job-old-container')


def test_lost_launch_reply_retains_id_without_replaying_or_resolving_new_name(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path))
    with mock.patch.object(rpc, 'request', return_value={'returncode': 0, 'stdout': json.dumps({'container': A})}) as inspect, \
         mock.patch.object(job_ops, 'control', side_effect=RemoteExecutionError('reply lost after submission')) as control:
        result = job_ops.start_remote_job(endpoint('mutable-name'), command='some-command', job_id='job-lost-reply')['result']
        assert result['outcome'] == 'failed'
        assert inspect.call_count == control.call_count == 1
    _, record = state_store.find_job_record('job-lost-reply')
    assert record['connection']['container'] == A
    with mock.patch.object(rpc, 'request', side_effect=AssertionError('must not re-resolve')), \
         mock.patch.object(job_ops, 'control', return_value={'state': 'running', 'quiet': False}) as control:
        job_ops.remote_job_status(None, job_id='job-lost-reply')
        assert control.call_args.args[0].container == A


def test_roots_share_rpc_only_within_the_same_container():
    from test_rpc_pool import Connection
    rpc.close_connections()
    try:
        with mock.patch.object(rpc, 'RpcConnection', side_effect=Connection) as factory:
            for root in ('/first', '/sibling'):
                for container in (A, B, None):
                    rpc.request(replace(endpoint(container), root=root), 'control', '', {})
            assert factory.call_count == 3
            assert {item.connection.endpoint.container for item in rpc._pool.values()} == {A, B, None}
    finally:
        rpc.close_connections()


def test_alias_resolver_cli_schema_and_result_keep_container(tmp_path, monkeypatch):
    from remote_dev import cli
    from remote_dev.core import endpoint as endpoints
    from remote_dev.mcp.schemas import ENDPOINT_PROPS
    assert 'container' in ENDPOINT_PROPS
    schema = json.loads((Path(__file__).parents[1] / 'remote_dev/schemas/endpoint.schema.json').read_text())
    assert 'container' in schema['properties']
    args = cli.build_parser('read').parse_args(['--host', 'host.example', '--port', '22', '--container', 'repro', '--file-path', '/code'])
    assert cli.endpoint_payload(args)['container'] == 'repro'
    aliases = tmp_path / 'endpoints.json'
    aliases.write_text(json.dumps({'repro': {'host': 'host.example', 'port': 22, 'container': 'first'}}))
    monkeypatch.setattr(endpoints, 'alias_files', lambda: [aliases])
    assert resolve_endpoint({'alias': 'repro', 'container': 'second'}).container == 'second'
    entry = endpoints.RegisteredResolver('fixture', lambda payload: {'host': 'host.example', 'port': 22,
                                                                    'container': A, 'container_selector': 'stale'})
    resolved = endpoints._endpoint_from_resolver(entry, {'container': B})
    assert resolved.container == B and resolved.container_selector is None
    entry = endpoints.RegisteredResolver('fixed', lambda payload: endpoint(A, container_selector='stale'))
    resolved = endpoints._endpoint_from_resolver(entry, {'container': B})
    assert resolved.container == B and resolved.container_selector is None


@pytest.mark.skipif(sys.platform != 'linux', reason='Container worker targets Linux /proc and Bash')
def test_real_worker_and_artifact_pipes_run_through_docker_exec_adapter(tmp_path, monkeypatch):
    """Execute shipped protocols locally after validating the complete Docker argv.

    This is not a container-isolation claim; live Docker coverage is separate.
    """
    from remote_dev.core import artifact_ops
    from remote_dev.core.shell_ops import remote_bash
    monkeypatch.setenv('REMOTE_DEV_STATE_DIR', str(tmp_path / 'state'))
    root = tmp_path / 'remote'
    root.mkdir()
    ep = replace(endpoint(), root=str(root), cwd=str(root))
    original = subprocess.Popen
    commands = []
    def rewrite(argv):
        # Keep the real caller-death supervisor; adapt only its target argv.
        if len(argv) >= 7 and Path(argv[2]).name == '_posix_owner.py':
            return [*argv[:6], *rewrite(argv[6:])]
        if argv[0] == 'container-test-ssh':
            command = shlex.split(argv[-1])
            assert command[:4] == ['docker', 'exec', '-i', A]
            assert command[4] == 'python3'
            commands.append(command)
            argv = [sys.executable, *command[5:]]
        return argv
    def launch(argv, **kwargs):
        return original(rewrite(argv), **kwargs)
    rpc.close_connections()
    try:
        with mock.patch.object(ssh, 'ssh_base_cmd', return_value=['container-test-ssh']), \
             mock.patch.object(subprocess, 'Popen', side_effect=launch):
            result = remote_bash(ep, command="printf 'out'; printf 'err' >&2; exit 7", wait=True)['result']
            assert result['quiet'] and result['exit_code'] == 7
            assert result['preview'] == {'stdout': 'out', 'stderr': 'err'}
            assert result['target']['container'] == A
            # The adapter retains the execution user's identity, just as Docker
            # exec without --user does. On the validated WSL run this is uid 1000.
            identity = remote_bash(ep, command='id -u', wait=True)['result']
            assert identity['exit_code'] == 0
            assert identity['preview']['stdout'].strip() == str(os.getuid())
            data = bytes(range(256)) * 8192
            binary = tmp_path / 'input.bin'
            binary.write_bytes(data)
            assert artifact_ops.remote_artifact_push(ep, local_path=str(binary), remote_path=str(root / 'data.bin'))['result']['outcome'] == 'success'
            pulled = artifact_ops.remote_artifact_pull(ep, remote_path=str(root / 'data.bin'), local_dir=str(tmp_path / 'pull'))['result']
            actual = Path(pulled['artifacts'][0]['pulled'][0]['local_path'])
            assert actual.read_bytes() == data
            assert len(commands) == 3  # One RPC and two independent artifact streams.
    finally:
        rpc.close_connections()
