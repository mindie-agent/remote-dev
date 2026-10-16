"""Resolve an explicit Docker coordinate without preparing its environment.

Names are fresh selections; full IDs are immutable coordinates. No name cache,
daemon, automatic repair or retry of a submitted operation belongs here.
"""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
import json
import re

from .errors import EndpointError
from .execution import timeout_value

FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
CONTAINER_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

_INSPECT = r'''
import json, subprocess, sys
request = json.load(sys.stdin)
result = subprocess.run(['docker', 'inspect', '--type', 'container', '--format', '{{json .}}', request['container']],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
if result.returncode:
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
value = json.loads(result.stdout)
if not value.get('State', {}).get('Running'):
    raise RuntimeError('Selected container is not running; remote-dev does not start or repair it')
print(json.dumps({'container': value['Id']}))
'''


def validate_container(container):
    if container is not None and (not isinstance(container, str) or not CONTAINER_SELECTOR.fullmatch(container)):
        raise EndpointError("container must be a nonempty Docker name or ID, without whitespace, shell syntax or leading options")


def pin_container_endpoint(endpoint, *, timeout_ms=None):
    """Return the endpoint with its selected container fixed to a full Docker ID.

    Full IDs and ordinary endpoints perform no lookup. Resolve names before
    creating local ledgers/job records, and retain this returned endpoint for
    all stages of one operation. A missing old ID must never fall back to a name.
    """
    timeout_value(timeout_ms)
    if endpoint is None or endpoint.container is None:
        return endpoint
    validate_container(endpoint.container)
    if FULL_CONTAINER_ID.fullmatch(endpoint.container):
        return endpoint
    from .rpc_transport import request
    host = replace(endpoint, container=None, container_selector=None, root="/", cwd="/",
                   runtime_env=False, runtime_env_file=None)
    row = request(host, "python", _INSPECT, {"container": endpoint.container},
                  timeout_ms=timeout_ms)
    if row.get("returncode") != 0 or row.get("timed_out") or row.get("cancelled"):
        detail = str(row.get("stderr") or row.get("stdout") or "Docker inspect did not complete")[-4000:]
        raise EndpointError("Cannot resolve existing container " + repr(endpoint.container) + ": " + detail)
    try:
        resolved = json.loads(row["stdout"])["container"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EndpointError("Docker inspect returned no full container ID") from exc
    if not isinstance(resolved, str) or not FULL_CONTAINER_ID.fullmatch(resolved):
        raise EndpointError("Docker inspect returned an invalid full container ID")
    return replace(endpoint, container=resolved, container_selector=endpoint.container)


def pinned_endpoint(function):
    """Use identical container pinning at native Python and MCP operation entry."""
    @wraps(function)
    def invoke(endpoint, *args, **kwargs):
        timeout_ms = timeout_value(kwargs.get("timeout_ms"))
        endpoint = pin_container_endpoint(endpoint, timeout_ms=timeout_ms)
        return function(endpoint, *args, **kwargs)
    return invoke
