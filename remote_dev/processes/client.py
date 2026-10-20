"""
[Role] Client boundary for remote background process supervision (`processes/client.py`).
[Invariants]
- Ships `processes/worker.py` over the pooled SSH JSON-RPC channel (`rpc_transport.request`) keyed by SHA-256 code digest (`lru_cache` loaded).
- Validates that every supervisor reply is a JSON object containing an explicit `state` field.
[Timeouts & Locks]
- Control RPCs have a 30s connection/control budget plus any requested output
  observation window. Durable job deadlines remain in the supervisor spec.
[Side-Effect Policy]
- Dispatches process-control RPCs to the remote endpoint's `.remote-dev/jobs/<job_id>` directory.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from functools import lru_cache
from typing import Any
from mindie_diagnostics import current_context

from remote_dev.core.endpoint import Endpoint, resolve_endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.rpc_transport import request as rpc_request

ACTIONS = frozenset({"prepare", "go", "status", "tail", "stop", "stdin", "launch", "exchange"})
WORKER_RELATIVE = Path(__file__).with_name("worker.py")
CONTROL_TIMEOUT_MS = 30000

@lru_cache(maxsize=1)
def worker_source() -> str:
    """Return the Linux supervisor source shipped into the remote root."""
    path = WORKER_RELATIVE
    if not path.is_file():
        raise RuntimeError(f"this install is missing {path.name}")
    return path.read_text(encoding="utf-8")


def _as_endpoint(endpoint: Endpoint | Mapping[str, Any]) -> Endpoint:
    if isinstance(endpoint, Endpoint):
        return endpoint
    if isinstance(endpoint, Mapping):
        return resolve_endpoint(dict(endpoint))
    raise TypeError("endpoint must be an Endpoint or mapping")


def control(endpoint: Endpoint | Mapping[str, Any], job_id: str, action: str,
            *, timeout_ms: int | None = None, **parameters: Any) -> dict[str, Any]:
    """Run one generic process-control action on an explicit endpoint.

    ``endpoint`` is an :class:`~remote_dev.core.endpoint.Endpoint` or an
    ordinary connection mapping (``host`` + ``port`` plus auth/root/cwd).
    ``action`` is ``prepare``, ``go``, ``status``, ``tail``, ``stop``, or
    ``stdin``. ``prepare`` takes ``spec={"command", "cwd", "env",
    "timeout_seconds", "interactive"}``; an interactive spec gives the job a
    writable stdin channel driven by the ``stdin`` action (``data`` bytes to
    write, ``eof`` to close input). ``go`` may carry an opaque
    ``authorization`` value; this package does not interpret resource-lease
    or fence semantics. Return value is the structured supervisor dict
    (``state``, ``quiet``, ``receipt``, ...).

    ``spec.prepared_timeout_seconds`` explicitly bounds waiting for ``go``;
    ``go.activation_timeout_seconds`` explicitly bounds delivery of activation
    to the waiting supervisor. Both default to None (no elapsed-time deadline)
    and accept only finite positive numbers when supplied. A coordinator owns
    its queue/lease limits and passes them explicitly. ``timeout_seconds``
    starts when the gated command is launched.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported job action: {action}")
    target = _as_endpoint(endpoint)
    request = {"root": target.root, "job_id": job_id, "action": action, **parameters,
               "diagnostics_context": current_context()}
    # launch/exchange observe output for at most five minutes, independently
    # of how long the detached job may run after this request returns.
    observation_ms = (
        max(0, min(300000, int(parameters.get("yield_time_ms") or 0)))
        if action in {"launch", "exchange"} else 0
    )
    data = rpc_request(target, "control", worker_source(), request,
                       timeout_ms=(CONTROL_TIMEOUT_MS if timeout_ms is None
                                   else max(1, int(timeout_ms))) + observation_ms)
    if not isinstance(data, dict):
        raise RemoteExecutionError("process control returned a non-object")
    if "state" not in data:
        raise RemoteExecutionError("process control response has no state")
    return data
