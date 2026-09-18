"""Client boundary for generic remote process supervision.

``remote_dev.processes.control(endpoint, job_id, action, **parameters)`` is
the package contract. Coordinator and ordinary background jobs share this
path. The Linux worker is shipped once per pooled SSH stdio connection;
subsequent requests carry JSON payloads and a code digest. The local client does
not invoke Bash or PowerShell to quote that payload.
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
CONTROL_TIMEOUT_MS = 45000
WORKER_RELATIVE = Path(__file__).with_name("worker.py")

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


def control(endpoint: Endpoint | Mapping[str, Any], job_id: str, action: str, **parameters: Any) -> dict[str, Any]:
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

    ``spec.prepared_timeout_seconds`` bounds waiting for ``go`` independently
    of command execution, defaulting to 120 seconds. It must be a number in
    ``[1, 86400]``. A coordinator that queues prepared jobs must explicitly set
    this to its bounded queue/activation window; local lease heartbeats do not
    extend it. ``timeout_seconds`` starts when the gated command is launched.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported job action: {action}")
    target = _as_endpoint(endpoint)
    request = {"root": target.root, "job_id": job_id, "action": action, **parameters,
               "diagnostics_context": current_context()}
    wait_ms = max(0, int(parameters.get("yield_time_ms") or 0))
    data = rpc_request(target, "control", worker_source(), request,
                       timeout_ms=max(CONTROL_TIMEOUT_MS, wait_ms + 15000))
    if not isinstance(data, dict):
        raise RemoteExecutionError("process control returned a non-object")
    if "state" not in data:
        raise RemoteExecutionError("process control response has no state")
    return data
