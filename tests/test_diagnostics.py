import os
import json
import subprocess
import sys
import time
import pytest
from unittest.mock import patch
import socket
import urllib.error
import urllib.request

from remote_dev.core.endpoint import resolve_endpoint
from remote_dev.core.ssh_transport import RemoteCompleted
from remote_dev.diagnostics import CONNECTION_PROBE_SCRIPT, diagnose_ssh, http_connection, http_failure, open_http, ssh_details
from remote_dev.runtime import process_identity, runtime_status


@pytest.mark.skipif(os.name == "nt", reason="ControlMaster is unsupported by Win32 OpenSSH")
def test_mux_probe_never_replays_caller_command():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "root": "/", "ssh_mux": True})
    with patch("remote_dev.diagnostics.run_script", side_effect=[
        RemoteCompleted(255, "", "timeout", timed_out=True),
        RemoteCompleted(0, json.dumps({"marker": "remote-dev-connection-ok"}), "", timed_out=False),
    ]) as run:
        result = diagnose_ssh(endpoint)
    assert result["status"] == "independent_connection_works"
    assert result["business_command_replayed"] is False
    assert run.call_args_list[1].args[0].ssh_mux is False
    assert all(call.args[1] == CONNECTION_PROBE_SCRIPT for call in run.call_args_list)


def test_successful_probe_needs_no_second_connection():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22})
    with patch("remote_dev.diagnostics.run_script", return_value=RemoteCompleted(0, json.dumps({"marker": "remote-dev-connection-ok"}), "", False)) as run:
        assert diagnose_ssh(endpoint)["status"] == "ok"
    assert run.call_count == 1
    assert run.call_args.kwargs["timeout_ms"] is None
    assert ssh_details(endpoint)["connect_timeout_ms"] is None


@pytest.mark.skipif(os.name == "nt", reason="ControlMaster is unsupported by Win32 OpenSSH")
def test_explicit_probe_deadline_splits_stage_budgets_and_preserves_remaining_time():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "ssh_mux": True,
                                 "connect_timeout_ms": 2500})
    with patch("remote_dev.diagnostics.run_script", side_effect=[
        RemoteCompleted(255, "", "connection failed"),
        RemoteCompleted(0, json.dumps({"marker": "remote-dev-connection-ok"}), ""),
    ]) as run, patch("remote_dev.diagnostics.time.monotonic", side_effect=[10.0, 10.25]):
        result = diagnose_ssh(endpoint, timeout_ms=7200000)
    assert [call.kwargs["timeout_ms"] for call in run.call_args_list] == [4320000, 7199750]
    assert result["status"] == "independent_connection_works"
    assert result["summary"] == "SSH succeeded only after bypassing ControlMaster."
    assert result["next"] == {"ssh_mux": False}
    assert result["probes"][0]["connect_timeout_ms"] == 2000


@pytest.mark.skipif(os.name == "nt", reason="ControlMaster is unsupported by Win32 OpenSSH")
def test_stage1_mux_timeout_still_runs_stage2_with_reserved_budget(tmp_path, monkeypatch):
    from remote_dev.core.context_snapshot import remote_probe

    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path))
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "ssh_mux": True})
    with patch("remote_dev.diagnostics.run_script", side_effect=[
        RemoteCompleted(None, "", "ControlMaster socket hung", timed_out=True),
        RemoteCompleted(0, json.dumps({"marker": "remote-dev-connection-ok"}), "", timed_out=False),
    ]) as run, patch("remote_dev.diagnostics.time.monotonic", side_effect=[10.0, 13.0]):
        diag = diagnose_ssh(endpoint, timeout_ms=5000)
    assert run.call_count == 2
    assert [call.kwargs["timeout_ms"] for call in run.call_args_list] == [3000, 2000]
    assert run.call_args_list[0].args[0].ssh_mux is True
    assert run.call_args_list[1].args[0].ssh_mux is False
    assert diag["status"] == "independent_connection_works"
    assert diag["summary"] == "SSH succeeded only after bypassing ControlMaster."
    assert diag["next"] == {"ssh_mux": False}
    assert diag["probes"][0]["timed_out"] is True
    assert diag["probes"][1]["ok"] is True

    with patch("remote_dev.diagnostics.run_script", side_effect=[
        RemoteCompleted(None, "", "ControlMaster socket hung", timed_out=True),
        RemoteCompleted(0, json.dumps({"marker": "remote-dev-connection-ok"}), "", timed_out=False),
    ]):
        payload = remote_probe(endpoint, timeout_ms=5000, diagnose_connection=True)
    result = payload["result"]
    assert result["outcome"] == "success"
    assert result["status"] == "independent_connection_works"
    assert result["summary"] == "SSH succeeded only after bypassing ControlMaster."
    assert result["next"] == {"ssh_mux": False}


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1000", float("inf"), float("nan")])
def test_invalid_probe_deadline_fails_before_dispatch(value):
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22})
    with patch("remote_dev.diagnostics.run_script") as run, pytest.raises(ValueError) as error:
        diagnose_ssh(endpoint, timeout_ms=value)
    assert getattr(error.value, "category") == "caller"
    run.assert_not_called()


def test_probe_reports_observed_time_and_leaves_connection_transfer_unknown():
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "ssh_mux": False})
    payload = {"marker": "remote-dev-connection-ok", "remote_execution_ms": 2.5,
               "facts": {"system": "Linux", "python": "3.12", "cwd": "/"}}
    completed = RemoteCompleted(0, json.dumps(payload), "", timings={
        "ssh_process_ms": 120.0, "connection_ms": None, "transfer_ms": None})
    with patch("remote_dev.diagnostics.run_script", return_value=completed):
        result = diagnose_ssh(endpoint)
    probe = result["probes"][0]
    assert probe["facts"] == payload["facts"]
    assert probe["timings"]["remote_execution_ms"] == 2.5
    assert probe["timings"]["unattributed_ssh_ms"] == 117.5
    assert probe["timings"]["connection_ms"] is None
    assert probe["timings"]["transfer_ms"] is None


@pytest.mark.parametrize("timeout", [False, True])
def test_script_transport_times_success_and_timeout_without_changing_output(timeout):
    from remote_dev.core.ssh_transport import run_script, RemoteBytesCompleted
    endpoint = resolve_endpoint({"host": "192.0.2.1", "port": 22, "ssh_mux": False})
    row = RemoteBytesCompleted([], None if timeout else 7, b"partial", b"detail", timed_out=timeout)
    with patch("remote_dev.core.ssh_transport._capture_command", return_value=row):
        result = run_script(endpoint, "printf test", timeout_ms=1000)
    assert (result.stdout, result.stderr, result.timed_out) == ("partial", "detail", timeout)
    assert result.returncode == (None if timeout else 7)
    assert all(result.timings[key] >= 0 for key in ("prepare_ms", "ssh_process_ms", "decode_ms", "total_ms"))
    assert result.timings["connection_ms"] is None
    assert result.timings["remote_execution_ms"] is None


def test_trace_milestones_use_received_events_and_preserve_ordinary_error():
    from remote_dev.core.ssh_transport import _run_traced_script
    script = """import sys, time
sys.stdin.buffer.read()
print('debug1: Connection established.', file=sys.stderr, flush=True)
time.sleep(.03)
print('Authenticated to example.test using publickey.', file=sys.stderr, flush=True)
time.sleep(.03)
print('probe result', flush=True)
print('ordinary error', file=sys.stderr, flush=True)
time.sleep(.03)
"""
    started = time.perf_counter()
    result = _run_traced_script([sys.executable, "-c", script], b"probe", 5000, started, started)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["probe result"]
    assert result.stderr == "ordinary error"
    assert 0 <= result.timings["tcp_connect_ms"] <= result.timings["connection_ms"] <= result.timings["ssh_process_ms"]
    assert result.timings["drain_and_exit_ms"] >= 0
    assert result.timings["transfer_ms"] is None


def test_http_direct_is_explicit_and_errors_are_not_model_attribution():
    with patch("urllib.request.build_opener") as build:
        open_http("http://example.test/health", timeout=5)
        assert build.call_args.args[0].proxies == {}
        build.return_value.open.assert_called_once_with("http://example.test/health", timeout=5)
    assert http_connection("http://u:password@example.test/health?token=secret")["target"] == "http://example.test/health"
    assert http_failure(urllib.error.HTTPError("http://example.test", 502, "bad gateway", {}, None)) == {"kind": "http_status", "status_code": 502}
    assert http_failure(urllib.error.URLError(socket.gaierror("lookup")))["kind"] == "dns"


def test_loaded_identity_does_not_change_when_installation_changes():
    first = {"package": "pkg", "version": "1", "commit": "a", "location": "/env"}
    with patch("remote_dev.runtime.installed_identity", return_value=first):
        loaded = process_identity("pkg")
    with patch("remote_dev.runtime.installed_identity", return_value={**first, "commit": "b"}):
        status = runtime_status(loaded)
    assert status["status"] == "restart_required"
    assert status["loaded"]["commit"] == "a"
    assert status["installed"]["commit"] == "b"
