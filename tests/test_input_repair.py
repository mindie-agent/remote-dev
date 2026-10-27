"""Model-visible endpoint discovery and trusted local input repair."""
from __future__ import annotations

import pytest

from remote_dev.core import endpoint as endpoint_module
from remote_dev.core.endpoint import EndpointError, resolve_endpoint
from remote_dev.core.errors import error_details
from remote_dev.mcp import tools
from remote_dev.mcp.schemas import harness_catalog


@pytest.fixture(autouse=True)
def no_ambient_selectors(monkeypatch):
    monkeypatch.setattr(endpoint_module, "_RESOLVERS", [])
    monkeypatch.setattr(endpoint_module, "_load_env_resolvers", lambda: None)
    monkeypatch.setattr(endpoint_module, "_read_endpoint_aliases_and_default",
                        lambda: ({}, None))


def repair_of(error):
    details = error_details(error)
    assert details["category"] == "caller"
    assert details["submission_state"] == "not_sent"
    assert details["retryable"] is False
    return details["input_repair"]


@pytest.mark.parametrize(
    ("args", "missing"),
    [
        ({"host": "192.0.2.10"}, ["port"]),
        ({"port": 22}, ["host"]),
        ({}, ["host", "port"]),
    ],
)
def test_missing_direct_endpoint_fields_are_actionable(args, missing):
    with pytest.raises(EndpointError) as caught:
        resolve_endpoint(args)
    repair = repair_of(caught.value)
    assert repair["missing"] == missing
    assert "host and port" in repair["action"]
    assert "22" in repair["action"]
    assert "implicit default" in repair["action"]
    assert "omit endpoint selectors" in repair["action"]


def test_missing_endpoint_reaches_tool_validation_before_transport(monkeypatch):
    monkeypatch.setattr(tools, "remote_bash", pytest.fail)
    with pytest.raises(EndpointError) as caught:
        tools.call_tool("remote_bash", {"host": "192.0.2.10", "command": "true"})
    assert repair_of(caught.value)["missing"] == ["port"]


def test_native_exec_command_missing_port_is_actionable(monkeypatch):
    monkeypatch.setattr(tools, "remote_bash", pytest.fail)
    with pytest.raises(EndpointError) as caught:
        tools.call_tool("remote_exec_command",
                        {"host": "192.0.2.10", "cmd": "true"}, profile="codex")
    assert repair_of(caught.value)["missing"] == ["port"]


def test_missing_operation_argument_uses_the_same_repair_path(monkeypatch):
    monkeypatch.setattr(tools, "remote_bash", lambda *args, **kwargs: {
        "text": "unexpected", "result": {"outcome": "success"}})
    with pytest.raises(ValueError) as caught:
        tools.call_tool("remote_bash", {"host": "192.0.2.10", "port": 22})
    assert repair_of(caught.value)["missing"] == ["command"]


def test_corrected_endpoint_and_arguments_reach_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "remote_bash", lambda endpoint, **kwargs: seen.append(
        (endpoint, kwargs)) or {"text": "ok", "result": {"outcome": "success"}})
    value = tools.call_tool("remote_bash", {
        "host": "192.0.2.10", "port": 22, "command": "true"})
    assert value["result"]["outcome"] == "success"
    assert len(seen) == 1 and seen[0][0].port == 22


def test_alias_and_job_controls_remain_valid_without_explicit_host_port(monkeypatch):
    monkeypatch.setattr(endpoint_module, "_read_endpoint_aliases",
                        lambda: {"fixture": {"host": "192.0.2.10", "port": 22}})
    assert resolve_endpoint({"alias": "fixture"}).port == 22
    job = next(tool for tool in harness_catalog("canonical")
               if tool["name"] == "remote_job_status")
    assert "host" not in job["inputSchema"]["required"]
    assert "port" not in job["inputSchema"]["required"]
    assert job["description"] == (
        "Check a remote background job through the shared process supervisor.")
    write = next(tool for tool in harness_catalog("codex")
                 if tool["name"] == "remote_write_stdin")
    assert write["inputSchema"]["required"] == ["session_id"]
    assert "session_id" in write["inputSchema"]["properties"]
    assert "restored from job_id" not in write["description"]


@pytest.mark.parametrize(
    ("profile", "name"),
    [("canonical", "remote_bash"), ("codex", "remote_exec_command")],
)
def test_advertised_tool_and_property_descriptions_explain_endpoint(profile, name):
    tool = next(item for item in harness_catalog(profile) if item["name"] == name)
    assert "host and port together" in tool["description"]
    assert "22" in tool["description"]
    assert "no implicit default" in tool["description"].lower()
    assert "omit endpoint selectors" in tool["description"]
    host = tool["inputSchema"]["properties"]["host"]["description"]
    port = tool["inputSchema"]["properties"]["port"]["description"]
    assert "host and port" in host
    assert "22" in port and "no implicit default" in port
    assert "host" not in tool["inputSchema"]["required"]
    assert "port" not in tool["inputSchema"]["required"]


def test_mcp_error_data_carries_input_repair(monkeypatch):
    from remote_dev.mcp import server

    messages = []
    monkeypatch.setattr(server, "send", lambda value, **kwargs: messages.append(value))
    server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                   "params": {"name": "remote_bash",
                              "arguments": {"host": "192.0.2.10", "command": "true"}}})
    details = messages[0]["error"]["data"]
    assert details["category"] == "caller"
    assert details["submission_state"] == "not_sent"
    assert details["input_repair"]["missing"] == ["port"]


def test_native_missing_operation_argument_is_caller_and_actionable():
    from remote_dev.mcp.native_spec import HarnessProfileError

    with pytest.raises(HarnessProfileError) as caught:
        tools.call_tool("remote_exec_command",
                        {"host": "192.0.2.10", "port": 22}, profile="codex")
    assert repair_of(caught.value)["missing"] == ["cmd"]


def test_transport_error_never_inherits_caller_repair():
    from remote_dev.core.errors import RemoteExecutionError, caller_error

    caller = caller_error("missing port", repair={
        "missing": ["port"],
        "action": "Retry with host and port together.",
    })
    transport = RemoteExecutionError(
        "rpc send failed", category="rpc_send", submission_state="uncertain")
    transport.input_repair = {"missing": ["port"], "action": "retry input"}
    transport.__cause__ = caller
    details = error_details(transport)
    assert details["category"] == "rpc_send"
    assert details["submission_state"] == "uncertain"
    assert "input_repair" not in details


def test_wrong_type_for_allow_empty_field_is_not_labeled_missing():
    from remote_dev.mcp.tools import _require

    with pytest.raises(ValueError) as caught:
        _require({"new_string": 7}, "new_string", "remote.edit", allow_empty=True)
    details = error_details(caught.value)
    assert details["category"] == "caller"
    assert "missing" not in details["input_repair"]
    assert "as a string" in details["input_repair"]["action"]
    assert "not missing" in details["input_repair"]["action"]


def test_partial_host_selector_does_not_use_configured_default(monkeypatch):
    monkeypatch.setattr(
        endpoint_module, "_read_endpoint_aliases_and_default",
        lambda: ({"a2": {"host": "192.0.2.10", "port": 22}}, "a2"),
    )
    with pytest.raises(EndpointError) as caught:
        resolve_endpoint({"host": "192.0.2.10"})
    assert repair_of(caught.value)["missing"] == ["port"]
