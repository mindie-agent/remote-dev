"""
[Role] Remote endpoint probing (`remote.probe`) and context snapshot persistence (`remote.context_snapshot`) (`context_snapshot.py`).
[Invariants]
- Gathers host/Git/Python and optional distribution source metadata in the selected interpreter without workload imports, or delegates `diagnose_connection=True` to `diagnostics.diagnose_ssh`.
- Preserves probe results with `operation_completed=True` even if local snapshot persistence fails.
[Timeouts & Locks]
- Metadata probes have a 30s default total budget; explicit caller budgets also bound transport and subprocess work. Connection diagnostics use their own split budget.
[Side-Effect Policy]
- Writes `context-<timestamp>.json` and `latest.json` atomically under `state/endpoints/<id>/context/`.
"""
from __future__ import annotations

from remote_dev.observability import observed_tool

import json
from functools import lru_cache
from pathlib import Path, PurePosixPath
import shlex
import time
from typing import Any

from remote_dev import package_version
from .endpoint import Endpoint
from .container_endpoint import pinned_endpoint
from remote_dev.result import make_result, utc_now_iso
from .ssh_transport import run_script
from .runtime_env import runtime_env_lines
from .path_policy import assert_under_root
from .state_store import atomic_write_json, ensure_endpoint_state


@lru_cache(maxsize=1)
def probe_source() -> str:
    return (Path(__file__).parents[1] / "processes" / "probe.py").read_text(encoding="utf-8")


def _probe(endpoint, *, timeout_ms, modules, distributions, python_executable,
           python_candidates=None, required_distributions=None):
    for label, names in (("modules", modules), ("distributions", distributions),
                         ("required_distributions", [] if required_distributions is None else required_distributions),
                         ("python_candidates", [] if python_candidates is None else python_candidates)):
        if (not isinstance(names, list) or len(names) > 32
                or any(not isinstance(name, str) or not name or len(name) > 256
                       or "\x00" in name for name in names)):
            raise ValueError(f"{label} must contain at most 32 non-empty names, each at most 256 characters")
    if any(not PurePosixPath(path).is_absolute() or "\x00" in path for path in (python_candidates or [])):
        raise ValueError("python_candidates must be absolute remote paths")
    if python_candidates is not None and len(python_candidates) > 16:
        raise ValueError("python_candidates accepts at most 16 additional paths")
    if python_executable is not None and (
        not isinstance(python_executable, str) or not PurePosixPath(python_executable).is_absolute()
        or "\x00" in python_executable
    ):
        raise ValueError("python_executable must be an absolute remote path")
    cwd = assert_under_root(endpoint.effective_cwd, endpoint.root)
    budget = 30000 if timeout_ms is None else timeout_ms
    payload = {"root": endpoint.root, "cwd": cwd, "modules": modules,
               "distributions": distributions, "timeout_ms": budget}
    if python_candidates is not None or required_distributions:
        if modules:
            raise ValueError("interpreter discovery reads metadata only; probe modules after selecting an interpreter")
        from .python_discovery import inventory_command
        command = inventory_command({**payload, "python_executable": python_executable,
                                     "python_candidates": python_candidates or [],
                                     "required_distributions": required_distributions or []})
    else:
        command = shlex.join([python_executable or "python3", "-I", "-c", probe_source(), json.dumps(payload)])
    script = "\n".join(["set -e", *runtime_env_lines(endpoint, endpoint.runtime_env),
                         "cd -- " + shlex.quote(cwd), "exec " + command]) + "\n"
    proc = run_script(endpoint, script, timeout_ms=budget)
    if getattr(proc, "timed_out", False) or getattr(proc, "cancelled", False):
        return {"status": "timeout" if getattr(proc, "timed_out", False) else "cancelled",
                "error": "remote probe did not complete", "stderr_tail": proc.stderr[-4000:]}
    if proc.returncode != 0:
        return {"status": "failed", "error": "remote probe exited unsuccessfully",
                "exit_code": proc.returncode, "stderr_tail": proc.stderr[-4000:]}
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
            raise ValueError("probe response must contain a summary object")
        return data
    except (ValueError, TypeError) as exc:
        return {"status": "failed", "error": f"invalid remote probe response: {exc}",
                "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


@pinned_endpoint
def write_context_snapshot(endpoint: Endpoint, summary: dict[str, Any], full_probe: dict[str, Any] | None = None) -> dict[str, Any]:
    base = ensure_endpoint_state(endpoint) / "context"
    payload = {
        "schema_version": "remote-dev.context.v1",
        "endpoint": {
            "endpoint_id": endpoint.endpoint_id,
            "host": endpoint.host,
            "port": endpoint.port,
            "user": endpoint.user,
            "root": endpoint.root,
            "default_cwd": endpoint.effective_cwd,
        },
        "summary": summary,
        "volatility": {
            "endpoint": "stable",
            "git_state": "semi_stable",
            "service_state": "volatile",
            "job_state": "volatile",
            "npu_state": "volatile",
        },
        "refs": {},
        "created_at": utc_now_iso(),
        "ttl_seconds": 300,
        "package_version": package_version(),
    }
    stamp = payload["created_at"].replace(":", "").replace("-", "")
    full_path = base / f"context-{stamp}.json"
    atomic_write_json(full_path, payload if full_probe is None else {**payload, "full_probe": full_probe})
    atomic_write_json(base / "latest.json", payload)
    payload["refs"]["full_probe"] = str(full_path)
    return payload


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def _probe_continuation(endpoint, python_executable):
    # These are observed connection parameters, never a guessed source path.
    arguments = {key: getattr(endpoint, key) for key in (
        "host", "port", "user", "root", "runtime_env", "runtime_env_file", "container",
        "auth_ref", "identity_file", "known_hosts_file") if getattr(endpoint, key) is not None}
    arguments["cwd"] = endpoint.effective_cwd
    return {
        "suggested_action": "inspect_candidates_or_use_native_script",
        "tool": "remote.bash", "endpoint_arguments": arguments,
        "python_executable": python_executable,
        "message": (
            "Only this probe failed. Reuse the returned candidate facts and endpoint. "
            "Choose an explicit interpreter or inspect the environment with an existing CLI "
            "or a task-local script via remote.bash or authorized native SSH. "
            "Discovery is optional; no package installation or source relocation is required "
            "to bypass it. Continue independent task steps and validate the eventual result."
        ),
    }


@observed_tool("remote.probe")
@pinned_endpoint
def remote_probe(endpoint: Endpoint, *, timeout_ms: int | None = None, diagnose_connection: bool = False,
                 modules: list[str] | None = None, distributions: list[str] | None = None,
                 python_executable: str | None = None, python_candidates: list[str] | None = None,
                 required_distributions: list[str] | None = None) -> dict[str, Any]:
    if diagnose_connection:
        from remote_dev.diagnostics import diagnose_ssh
        diagnosis = diagnose_ssh(endpoint, timeout_ms=timeout_ms)
        snapshot = write_context_snapshot(endpoint, {"connection": diagnosis})
        result = make_result(tool="remote.probe", target=endpoint.to_result_target(),
                             outcome="failed" if diagnosis["status"] == "unavailable" else "success",
                             status=diagnosis["status"],
                             summary=diagnosis.get("summary") or "Read-only SSH connection diagnostic.",
                             refs=snapshot["refs"], next=diagnosis["next"], extra={"connection": diagnosis})
        return {"text": result["summary"], "result": result}
    started = utc_now_iso()
    start = time.monotonic()
    data = _probe(endpoint, timeout_ms=timeout_ms, modules=[] if modules is None else modules,
                  distributions=[] if distributions is None else distributions,
                  python_executable=python_executable, python_candidates=python_candidates,
                  required_distributions=required_distributions)
    status = str(data.get("status", "failed"))
    summary = data.get("summary", {}) if isinstance(data.get("summary"), dict) else {}
    recording_error = None
    try:
        snapshot = write_context_snapshot(endpoint, summary, data) if status == "ok" else None
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        snapshot = None
        recording_error = f"{type(exc).__name__}: {exc}"
    result = make_result(
        tool="remote.probe",
        target=endpoint.to_result_target(),
        outcome="success" if status == "ok" else ("timeout" if status == "timeout" else "failed"),
        status=status,
        summary="Remote probe completed." if status == "ok" else "Remote probe failed.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"summary": summary},
        next=_probe_continuation(endpoint, python_executable) if status != "ok" else None,
        refs=snapshot.get("refs", {}) if isinstance(snapshot, dict) else {},
        extra={"snapshot": snapshot, "error": data.get("error"), "error_details": data.get("error_details"), "probe": data},
    )
    text = "RemoteProbe {status} on {user}@{host}:{port}\n".format(
        status=status,
        user=endpoint.user,
        host=endpoint.host,
        port=endpoint.port,
    )
    if summary:
        text += "hostname: {hostname}\npython: {python}\n".format(
            hostname=summary.get("hostname", ""),
            python=summary.get("python", ""),
        )
    if result.get("next"):
        text += result["next"]["message"] + "\n"
    if recording_error:
        execution_outcome, execution_status = result["outcome"], result["status"]
        result.update(outcome="failed", status="local_recording_failed", operation_completed=True,
                      execution_outcome=execution_outcome, execution_status=execution_status,
                      recording_error=recording_error,
                      summary="Remote probe completed, but the local context snapshot could not be recorded.")
        text += result["summary"] + "\n" + recording_error + "\n"
    return {"text": text, "result": result}


@observed_tool("remote.context_snapshot")
@pinned_endpoint
def remote_context_snapshot(endpoint: Endpoint, *, timeout_ms: int | None = None, live_probe: bool = True) -> dict[str, Any]:
    if live_probe:
        payload = remote_probe(endpoint, timeout_ms=timeout_ms)
        payload["result"]["tool"] = "remote.context_snapshot"
        return payload
    snapshot = write_context_snapshot(endpoint, {"status": "ok", "note": "snapshot requested without live probe"})
    result = make_result(
        tool="remote.context_snapshot",
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary="Context snapshot written.",
        preview={"summary": snapshot["summary"]},
        refs=snapshot.get("refs", {}),
        extra={"snapshot": snapshot},
    )
    return {"text": "Remote context snapshot written.\n", "result": result}
