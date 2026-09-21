"""Conservative reporting classification without changing original tool results."""
import json
import subprocess
import sys
import types

import pytest
from mindie_diagnostics import configure
from remote_dev.core.errors import EndpointError, PathPolicyError, RemoteExecutionError, caller_error
from remote_dev.observability import confirmed_failure, observed_tool


@pytest.fixture
def reports(tmp_path, monkeypatch):
    configure('remote-dev', root=tmp_path / 'logs')
    calls = []
    module = types.ModuleType('mindie_diagnostics.integration')
    def record_failure(component, operation, **kwargs):
        calls.append({'component': component, 'operation': operation, **kwargs})
        return {'recorded': True, 'incident_id': 'a' * 32, 'logging_failed': False, 'private_extra': 'not_forwarded'}
    module.record_failure = record_failure
    monkeypatch.setitem(sys.modules, 'mindie_diagnostics.integration', module)
    return calls


@pytest.mark.parametrize('exception', [
    caller_error('private'), EndpointError('private'), PathPolicyError('private'),
    ValueError('private'), TypeError('private'), PermissionError('private'),
    FileNotFoundError('private'), ConnectionError('private'), KeyboardInterrupt(), SystemExit(3),
    RemoteExecutionError('private', category='cancelled'),
    RemoteExecutionError('private', category='connection_unavailable', submission_state='not_sent'),
    RemoteExecutionError('private', category='rpc_timeout', submission_state='uncertain'),
])
def test_expected_failure_preserves_identity_without_report(reports, exception):
    @observed_tool('remote.bash')
    def run():
        raise exception
    with pytest.raises(type(exception)) as caught:
        run()
    assert caught.value is exception and reports == []


@pytest.mark.parametrize('outcome,category', [('failed','command_exit'), ('timeout','command_timeout'), ('cancelled','cancelled'), ('failed','command_protocol'), ('failed','internal')])
def test_returned_failure_is_not_owner_evidence(reports, outcome, category):
    @observed_tool('remote.bash')
    def run():
        return {'outcome': outcome, 'error_details': {'category': category}, 'stdout': 'private_stdout'}
    result = run()
    assert result['outcome'] == outcome and result['stdout'] == 'private_stdout'
    assert reports == [] and 'diagnostic' not in result


def test_actual_unhandled_internal_exception_once_and_safe_reference(reports):
    failure = KeyError('private_error')
    @observed_tool('remote.read')
    def inner():
        raise failure
    @observed_tool('remote.read')
    def outer():
        return inner()
    with pytest.raises(KeyError) as caught:
        outer()
    assert caught.value is failure and len(reports) == 1
    assert reports[0]['operation'] == 'remote.read' and reports[0]['category'] == 'internal_exception'
    assert failure.mindie_diagnostic == {'incident_id': 'a' * 32, 'logging_failed': False}


def test_wrapped_network_and_malicious_category_do_not_report(reports):
    @observed_tool('remote.read')
    def wrapped():
        try:
            raise RemoteExecutionError('private', category='rpc_send', submission_state='uncertain')
        except RemoteExecutionError as cause:
            raise RuntimeError('wrapper') from cause
    with pytest.raises(RuntimeError):
        wrapped()
    marked = RuntimeError('private')
    marked.category = 'lower_case_private_secret'
    @observed_tool('remote.read')
    def marked_failure():
        raise marked
    with pytest.raises(RuntimeError):
        marked_failure()
    assert reports == []
    assert confirmed_failure('private', stage='lower_case_private_secret', category='internal_exception') is None
    assert reports == []


def test_recorder_failure_does_not_replace_original(reports, monkeypatch):
    module = sys.modules['mindie_diagnostics.integration']
    module.record_failure = lambda *a, **k: (_ for _ in ()).throw(ValueError('private_logger'))
    failure = AssertionError('actual_internal')
    @observed_tool('remote.read')
    def run():
        raise failure
    with pytest.raises(AssertionError) as caught:
        run()
    assert caught.value is failure and failure.mindie_diagnostic == {'logging_failed': True}


@pytest.mark.parametrize('body', ['not_json', '[1]'])
def test_actual_local_protocol_output_owner_records_once(reports, monkeypatch, body):
    from remote_dev.core.endpoint import resolve_endpoint
    from remote_dev.core import rpc_transport, ssh_transport
    def local_request(*a, **kw):
        completed = subprocess.run([sys.executable, '-c', f'print({body!r})'], capture_output=True, text=True, timeout=3)
        return {'returncode': completed.returncode, 'stdout': completed.stdout, 'stderr': completed.stderr}
    monkeypatch.setattr(rpc_transport, 'request', local_request)
    endpoint = resolve_endpoint({'host': 'example.invalid', 'port': 22, 'root': '/tmp', 'cwd': '/tmp'})
    @observed_tool('remote.read')
    def run():
        return {'result': ssh_transport.run_remote_python(endpoint, 'owned fixture', {})}
    result = run()['result']
    assert result['status'] == 'failed' and len(reports) == 1
    assert reports[0]['category'] == 'command_protocol'
    assert result['diagnostic'] == {'incident_id': 'a' * 32, 'logging_failed': False}
