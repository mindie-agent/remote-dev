"""Python callers reject invalid deadlines before any endpoint or transport work."""
import math
from unittest import mock

import pytest

from remote_dev import diagnostics
from remote_dev.core import artifact_transport, container_endpoint, rpc_transport, ssh_transport
from remote_dev.core.endpoint import Endpoint


def sdk_calls(endpoint, timeout):
    return {
        "request": lambda: rpc_transport.request(endpoint, "python", "pass", {}, timeout_ms=timeout),
        "run_script": lambda: ssh_transport.run_script(endpoint, "true", timeout_ms=timeout),
        "run_stream": lambda: ssh_transport.run_stream(endpoint, "true", timeout_ms=timeout),
        "run_bytes": lambda: ssh_transport.run_bytes(endpoint, "true", timeout_ms=timeout),
        "run_rpc_script": lambda: ssh_transport.run_rpc_script(endpoint, "true", timeout_ms=timeout),
        "run_remote_python": lambda: ssh_transport.run_remote_python(endpoint, "pass", {}, timeout_ms=timeout),
        "stream_ssh_command": lambda: ssh_transport.stream_ssh_command(endpoint, "true", timeout_ms=timeout),
        "diagnose_ssh": lambda: diagnostics.diagnose_ssh(endpoint, timeout_ms=timeout),
        "ArtifactStream": lambda: artifact_transport.ArtifactStream(endpoint, "push", 1, timeout_ms=timeout),
    }


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "1000", math.nan, math.inf])
def test_invalid_sdk_deadline_precedes_pinning_and_process_creation(bad):
    endpoint = Endpoint(host="192.0.2.1", port=22, container="named-container", ssh_mux=False)
    with mock.patch.object(container_endpoint, "pin_container_endpoint") as pin, mock.patch.object(
            rpc_transport, "pin_container_endpoint") as rpc_pin, mock.patch.object(
            ssh_transport.subprocess, "Popen") as popen, mock.patch.object(
            ssh_transport.subprocess, "run") as run, mock.patch.object(rpc_transport, "_acquire") as acquire:
        for name, invoke in sdk_calls(endpoint, bad).items():
            with pytest.raises(ValueError) as error:
                invoke()
            assert error.value.category == "caller", name
        pin.assert_not_called()
        rpc_pin.assert_not_called()
        acquire.assert_not_called()
        popen.assert_not_called()
        run.assert_not_called()


@pytest.mark.parametrize("value", [None, 7200000])
def test_sdk_none_and_large_explicit_deadline_reach_pin_unchanged(value):
    endpoint = Endpoint(host="192.0.2.1", port=22, container="named-container", ssh_mux=False)
    class PinReached(Exception):
        pass
    def inspect(target, *, timeout_ms):
        assert target is endpoint
        assert timeout_ms == value
        raise PinReached
    with mock.patch.object(container_endpoint, "pin_container_endpoint", side_effect=inspect), mock.patch.object(
            rpc_transport, "pin_container_endpoint", side_effect=inspect):
        for invoke in sdk_calls(endpoint, value).values():
            with pytest.raises(PinReached):
                invoke()


@pytest.mark.parametrize("bad", [-1, True, math.nan])
def test_direct_pin_and_rpc_connection_reject_before_their_dependencies(bad):
    endpoint = Endpoint(host="192.0.2.1", port=22, container="named-container")
    with mock.patch.object(rpc_transport, "request") as request:
        with pytest.raises(ValueError) as error:
            container_endpoint.pin_container_endpoint(endpoint, timeout_ms=bad)
        assert error.value.category == "caller"
        request.assert_not_called()
    # No readiness event or process exists: validation must precede touching
    # either, even for a caller using the connection object directly.
    connection = object.__new__(rpc_transport.RpcConnection)
    with pytest.raises(ValueError) as error:
        connection.request("python", "pass", {}, bad)
    assert error.value.category == "caller"
