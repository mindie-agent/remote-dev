"""Explicit connection diagnostics; never replay a caller's business command."""
from __future__ import annotations

import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace

from remote_dev.core.ssh_transport import _uses_shared_mux, run_script
from remote_dev.core.container_endpoint import pinned_endpoint
from remote_dev.core.execution import timeout_value

# One fixed read-only request gathers related facts without model imports.
# This protocol is not a wrapper around an arbitrary caller command.
CONNECTION_PROBE_SCRIPT = """python3 - <<'REMOTE_DEV_PROBE'
import json, os, platform, sys, time
started = time.perf_counter()
facts = {"system": platform.system(), "cwd": os.getcwd(), "python": sys.version.split()[0]}
elapsed = (time.perf_counter() - started) * 1000
print(json.dumps({"marker": "remote-dev-connection-ok", "facts": facts, "remote_execution_ms": elapsed}))
REMOTE_DEV_PROBE
"""


def _probe_payload(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("marker") == "remote-dev-connection-ok":
            return value
    return {}


def ssh_details(endpoint, timeout_ms=None) -> dict:
    return {"kind": "ssh", "mode": "multiplexed" if _uses_shared_mux(endpoint) else "independent",
            "connect_timeout_ms": (None if endpoint.connect_timeout_ms is None else
                                   max(1, endpoint.connect_timeout_ms // 1000) * 1000),
            "timeout_ms": timeout_ms}


@pinned_endpoint
def diagnose_ssh(endpoint, *, timeout_ms=None) -> dict:
    timeout_value(timeout_ms)
    deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000
    probes = []
    for target in (endpoint, replace(endpoint, ssh_mux=False)):
        if probes and (probes[0]["ok"] or not _uses_shared_mux(endpoint)):
            break
        budget = (timeout_ms if not probes or deadline is None else
                  math.ceil((deadline - time.monotonic()) * 1000))
        if budget is not None and budget <= 0:
            break
        result = run_script(target, CONNECTION_PROBE_SCRIPT, timeout_ms=budget, trace_connection=True)
        payload = _probe_payload(result.stdout)
        timings = dict(result.timings)
        remote_ms = payload.get("remote_execution_ms")
        if (result.returncode == 0 and not result.timed_out and isinstance(remote_ms, (int, float))
                and not isinstance(remote_ms, bool) and math.isfinite(remote_ms) and remote_ms >= 0):
            timings["remote_execution_ms"] = round(remote_ms, 3)
            ssh_ms = timings.get("ssh_process_ms")
            known_ms = remote_ms + (timings.get("connection_ms") or 0) + (timings.get("drain_and_exit_ms") or 0)
            if isinstance(ssh_ms, (int, float)) and ssh_ms >= known_ms:
                timings["unattributed_ssh_ms"] = round(ssh_ms - known_ms, 3)
        probes.append({**ssh_details(target, budget), "exit_code": result.returncode,
                       "timed_out": result.timed_out,
                       "ok": result.returncode == 0 and not result.timed_out and bool(payload),
                       "timings": timings, "facts": payload.get("facts", {}),
                       "stderr": result.stderr[-1000:]})
    recovered = len(probes) == 2 and probes[1]["ok"]
    return {"probes": probes, "business_command_replayed": False,
            "status": "independent_connection_works" if recovered else "ok" if probes[0]["ok"] else "unavailable",
            "next": {"ssh_mux": False} if recovered else None}


def http_connection(request, *, proxy_mode="direct") -> dict:
    if proxy_mode not in {"direct", "environment"}:
        raise ValueError("proxy_mode must be direct or environment")
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    authority = host + (":" + str(parsed.port) if parsed.port else "")
    # Credentials and query parameters never enter diagnostics.
    return {"kind": "http", "target": urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", "")),
            "proxy_mode": proxy_mode}


def open_http(request, *, timeout, proxy_mode="direct"):
    http_connection(request, proxy_mode=proxy_mode)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}) if proxy_mode == "direct" else urllib.request.ProxyHandler())
    return opener.open(request, timeout=timeout)


def http_failure(exc: BaseException) -> dict:
    if isinstance(exc, urllib.error.HTTPError):
        return {"kind": "http_status", "status_code": exc.code}
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    kind = ("dns" if isinstance(cause, socket.gaierror) else
            "timeout" if isinstance(cause, (TimeoutError, socket.timeout)) else
            "connection_refused" if isinstance(cause, ConnectionRefusedError) else "connection")
    return {"kind": kind, "message": str(cause)[:300]}
