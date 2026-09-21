from __future__ import annotations

from remote_dev.observability import current_tool, observed_tool

import re
import json
from dataclasses import asdict
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from remote_dev.core.endpoint import DEFAULT_CWD, DEFAULT_ROOT, Endpoint
from remote_dev.core.container_endpoint import FULL_CONTAINER_ID, pinned_endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS, compact_text
from remote_dev.core.locking import record_lock
from remote_dev.core.runtime_env import runtime_env_lines
from remote_dev.core.state_store import atomic_write_json, find_job_record, job_record_path
from remote_dev.processes import control
from remote_dev.result import make_result, tool_text, utc_now_iso
from remote_dev.core.errors import error_details

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV_PREFIX = "REMOTE_DEV_JOB_"
STOP_DRAIN_SECONDS = 2.0
MAX_YIELD_MS = 300000
MAX_INCREMENTAL_READ_BYTES = 32768


def _duration_ms(start: float) -> int:
    return int(round((time.monotonic() - start) * 1000))


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"job-{stamp}-{uuid.uuid4().hex[:8]}"


def require_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value):
        raise ValueError("job id must be 3-96 chars from A-Z a-z 0-9 _ . -")
    return value


def require_env_name(value: str) -> str:
    if not ENV_NAME_RE.fullmatch(value):
        raise ValueError(f"invalid environment variable name: {value!r}")
    if value.startswith(RESERVED_ENV_PREFIX):
        raise ValueError(f"reserved environment variable name: {value!r}")
    return value


def remote_job_dir(endpoint: Endpoint, job_id: str) -> str:
    require_job_id(job_id)
    return str(PurePosixPath(endpoint.root) / ".remote-dev" / "jobs" / job_id)


def _timeout_seconds(timeout_ms: int | None) -> float | None:
    if timeout_ms is None or timeout_ms <= 0:
        return None
    return min(timeout_ms / 1000, 86400)


def _job_command(endpoint: Endpoint, command: str, runtime_enabled: bool) -> str:
    preamble = runtime_env_lines(endpoint, runtime_enabled)
    if not preamble:
        return command
    # Parse the user's command after initialization, in the same shell. This
    # preserves runtime functions/options and avoids a second SSH .bashrc load.
    return "\n".join([*preamble, f"eval -- {shlex.quote(command)}"])


_PUBLIC_JOB_KEYS = (
    "schema_version",
    "job_id",
    "description",
    "target",
    "command_preview",
    "cwd",
    "env_keys",
    "runtime_env",
    "runtime_env_file",
    "remote_dir",
    "started_at",
    "timeout_ms",
    "tty",
    "state",
    "stdin_cursors",
    "local_output_offsets",
    "diagnostics",
)


def _public_job_record(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Project saved job state for tool responses without private start-gate fields."""
    projected = {key: record[key] for key in _PUBLIC_JOB_KEYS if key in record}
    projected.update(extra)
    projected.pop("authorization", None)
    return projected


def _failure_payload(result: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    if "diagnostics" not in result:
        operation = current_tool()
        if operation:
            # Completed transport phases already exist before the enclosing
            # observed_tool finishes. Text-only clients need them as well.
            result["diagnostics"] = operation["op"].summary()
    payload = {"text": text if text is not None else str(result.get("summary") or "") + "\n", "result": result}
    payload["text"] = tool_text(payload)
    return payload


def _record_cwd(target: dict[str, Any]) -> str:
    return str(target.get("cwd") or DEFAULT_CWD or target.get("root") or DEFAULT_ROOT)


def _endpoint_from_record(record: dict[str, Any]) -> Endpoint:
    target = record.get("connection") or record.get("target", {})
    fields = Endpoint.__dataclass_fields__
    endpoint = Endpoint(**{key: value for key, value in target.items() if key in fields})
    if endpoint.container and not FULL_CONTAINER_ID.fullmatch(endpoint.container):
        raise ValueError("job record lacks a fixed full container ID; refusing to resolve a replacement by name")
    return endpoint



def endpoint_from_job_record(record: dict[str, Any]) -> Endpoint:
    return _endpoint_from_record(record)


def _load_record(endpoint: Endpoint | None, job_id: str) -> tuple[Endpoint, dict[str, Any], Path]:
    job_id = require_job_id(job_id)
    if endpoint is not None:
        path = job_record_path(endpoint, job_id)
        if not path.exists():
            raise FileNotFoundError(f"unknown remote job id for endpoint: {job_id}")
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return endpoint, data, path
    found = find_job_record(job_id)
    if not found:
        raise FileNotFoundError(f"unknown remote job id: {job_id}")
    path, data = found
    return _endpoint_from_record(data), data, path


def _start_failure(
    endpoint: Endpoint,
    *,
    cwd: str,
    started: str,
    start: float,
    job_id: str,
    outcome: str,
    status: str,
    summary: str,
    error: str,
) -> dict[str, Any]:
    result = make_result(
        tool="remote.bash",
        target={**endpoint.to_result_target(), "cwd": cwd},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=summary,
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"error": error[-4000:], "job_id": job_id},
    )
    return _failure_payload(result, text=summary + "\n")


def _classify_start_error(exc: BaseException) -> tuple[str, str, str]:
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, FileNotFoundError) or "no such file" in lowered or "cwd" in lowered and "not" in lowered and "exist" in lowered:
        return "failed", "cwd_not_found", "Remote background task failed because cwd does not exist."
    if isinstance(exc, NotADirectoryError) or "not a directory" in lowered:
        return "failed", "cwd_not_directory", "Remote background task failed because cwd is not a directory."
    if "escapes the runtime root" in lowered or "outside" in lowered:
        return "blocked", "cwd_outside_root", "Remote background task blocked because cwd is outside root."
    if "reused with different" in lowered:
        return "blocked", "job_id_exists", "Remote background task blocked because job_id already exists."
    if "not a verified waiting supervisor" in lowered:
        return "blocked", "job_id_exists", "Remote background task blocked because job_id already exists."
    return "failed", "job_start_failed", "Remote background task failed to start."


def _yield_ms(value: int | None, default: int) -> int:
    return max(0, min(MAX_YIELD_MS, default if value is None else int(value)))


def _save_output(record, path, row):
    # Hold the record lock from read through exchange and cursor commit.
    # Overwrite from the committed length after a crash before cursor commit.
    cursors = record.setdefault("stdin_cursors", {})
    local_offsets = record.setdefault("local_output_offsets", {})
    for name in ("stdout", "stderr"):
        log = path.with_name(path.stem + "." + name + ".log")
        with log.open("r+b" if log.exists() else "w+b") as stream:
            stream.seek(int(local_offsets.get(name, 0)))
            stream.write(str(row.get(name) or "").encode("utf-8"))
            stream.truncate()
            local_offsets[name] = stream.tell()
        cursors[name + "_offset"] = int(row.get(name + "_offset", cursors.get(name + "_offset", 0)))
    record["state"] = row.get("state", "unknown")
    atomic_write_json(path, record)


def _session_result(endpoint, record, path, row, *, tool, started, start, budget):
    state = str(row.get("state") or "unknown")
    quiet = bool(row.get("quiet"))
    pending = {name: int(row.get(name + "_bytes_remaining") or 0) for name in ("stdout", "stderr")}
    exit_code = (row.get("result") or {}).get("exit_code")
    accepted = row.get("accepted", True)
    outcome = ("failed" if not accepted or state in {"failed", "absent", "lost"}
               else "timeout" if state == "timeout" else "cancelled" if state == "cancelled" or row.get("cancellation_requested") else "success")
    job_id = record["job_id"]
    summary = f"Remote command {state}." + (f" Exit code: {exit_code}." if exit_code is not None else "")
    preview = {name: str(row.get(name) or "") for name in ("stdout", "stderr")}
    warnings = []
    if row.get("stdin_buffer_full"):
        warnings.append("Input partially accepted; resend chars[stdin.written_chars:] after the program consumes input.")
    if row.get("eof_deferred"):
        warnings.append("EOF deferred until all input is accepted; resend the remainder with eof=true.")
    if any(pending.values()):
        warnings.append("Output remains; poll the session to continue from the saved cursor.")
    if not accepted:
        warnings.append(str(row.get("reason") or "stdin rejected"))
    refs = {"job_record": str(path), "remote_dir": record["remote_dir"]}
    refs.update({name: str(path.with_name(path.stem + "." + name + ".log")) for name in ("stdout", "stderr")})
    result = make_result(
        tool=tool, target={**endpoint.to_result_target(), "cwd": record["cwd"]},
        outcome=outcome, status=state if accepted else "stdin_rejected", summary=summary,
        started_at=started, duration_ms=_duration_ms(start), preview=preview, refs=refs, warnings=warnings,
        extra={"job_id": job_id, "session_id": job_id if not quiet or any(pending.values()) else None,
               "state": state, "quiet": quiet, "exit_code": exit_code,
               "cancellation_requested": bool(row.get("cancellation_requested")),
               "timings": {**row.get("timings", {}), **row.get("transport", {})},
               "cursors": record.get("stdin_cursors", {}), "bytes_remaining": pending,
               "output_budget_bytes": budget,
               "stdin": {key: row[key] for key in ("accepted", "written", "written_chars", "eof", "eof_deferred", "stdin_buffer_full", "retryable") if key in row},
               "environment": {key: record.get(key) for key in ("runtime_env", "runtime_env_file", "env_keys", "timeout_ms")}},
    )
    text = summary + "\n"
    if result["session_id"]:
        text += f"session_id: {job_id}\n"
    for warning in warnings:
        text += warning + "\n"
    for name, body in preview.items():
        if body:
            text += f"__{name.upper()}__\n{body}"
    return {"text": text, "result": result}


@pinned_endpoint
def start_remote_job(
    endpoint: Endpoint, *, command: str, cwd: str | None = None,
    env: dict[str, str] | None = None, timeout_ms: int | None = None,
    runtime_env: bool | None = None, description: str | None = None,
    job_id: str | None = None, yield_time_ms: int | None = None,
    max_output_tokens: int | None = None, tty: bool = False, wait: bool = False,
) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    env = env or {}
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    job_id = require_job_id(job_id or new_job_id())
    cwd = cwd or endpoint.effective_cwd
    path = job_record_path(endpoint, job_id)
    spec = {"command": _job_command(endpoint, command, runtime_enabled), "cwd": cwd,
            "env": {require_env_name(key): str(value) for key, value in env.items()},
            "timeout_seconds": _timeout_seconds(timeout_ms), "interactive": not wait, "tty": bool(tty)}
    record = {"schema_version": "remote-dev.job.v1", "job_id": job_id,
              "description": description, "target": {**endpoint.to_result_target(), "cwd": cwd},
              "connection": {**asdict(endpoint), "cwd": cwd}, "command_preview": command[:500],
              "cwd": cwd, "env_keys": sorted(env), "runtime_env": runtime_enabled,
              "runtime_env_file": endpoint.runtime_env_file, "remote_dir": remote_job_dir(endpoint, job_id),
              "started_at": started, "timeout_ms": timeout_ms, "tty": bool(tty),
              "authorization": {"token": uuid.uuid4().hex, "job_id": job_id}}
    budget = _output_budget_bytes(max_output_tokens)
    with record_lock(path):
        if path.exists() or find_job_record(job_id):
            return _start_failure(endpoint, cwd=cwd, started=started, start=start, job_id=job_id,
                                  outcome="blocked", status="job_id_exists", summary="Remote job id already exists.", error=job_id)
        # Save before submission: a lost reply retains the exact recovery id.
        # A submitted launch is never automatically replayed.
        atomic_write_json(path, record)
        try:
            row = control(endpoint, job_id, "launch", spec=spec, authorization=record["authorization"],
                          stdout_offset=0, stderr_offset=0, max_bytes=budget, shared_budget=True,
                          wait_for_exit=wait,
                          yield_time_ms=_yield_ms(yield_time_ms, 10000))
            _save_output(record, path, row)
            first_output = {name: str(row.get(name) or "") for name in ("stdout", "stderr")}
            while wait and (not row.get("quiet") or any(row.get(name + "_bytes_remaining") for name in ("stdout", "stderr"))):
                if row.get("state") in {"absent", "lost", "unknown"} or row.get("unknown"):
                    break
                row = control(endpoint, job_id, "exchange", **record["stdin_cursors"],
                              max_bytes=MAX_INCREMENTAL_READ_BYTES, shared_budget=True,
                              wait_for_exit=True, yield_time_ms=1000)
                _save_output(record, path, row)
                for name in ("stdout", "stderr"):
                    left = max(0, budget - sum(len(body.encode("utf-8")) for body in first_output.values()))
                    first_output[name] += str(row.get(name) or "").encode("utf-8")[:left].decode("utf-8", "ignore")
            if wait:
                row.update(first_output)
        except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
            outcome, status, summary = _classify_start_error(exc)
            failure = _start_failure(endpoint, cwd=cwd, started=started, start=start, job_id=job_id,
                                    outcome=outcome, status=status, summary=summary, error=str(exc))
            failure["result"]["refs"] = {"job_record": str(path)}
            failure["result"]["session_id"] = job_id
            failure["result"]["error_details"] = error_details(exc)
            if failure["result"]["error_details"].get("submission_state") == "uncertain":
                failure["result"]["status"] = "submission_uncertain"
                failure["result"]["summary"] = "Remote submission outcome is uncertain; observe the original job."
            return _failure_payload(failure["result"])
    return _session_result(endpoint, record, path, row, tool="remote.bash", started=started, start=start, budget=budget)


@observed_tool("remote.job_status")
@pinned_endpoint
def remote_job_status(endpoint: Endpoint | None, *, job_id: str) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "status")
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_status",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job {job_id} status failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job": _public_job_record(record, error=str(exc)[-4000:]), "error_details": error_details(exc)},
        )
        return _failure_payload(result)
    status = str(supervisor.get("state") or "unknown")
    result = make_result(
        tool="remote.job_status",
        target=endpoint.to_result_target(),
        outcome="success",
        status=status,
        summary=f"Remote job {job_id} is {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"job": _public_job_record(record, remote_status=supervisor, quiet=supervisor.get("quiet"))},
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}


@observed_tool("remote.job_tail")
@pinned_endpoint
def remote_job_tail(endpoint: Endpoint | None, *, job_id: str, lines: int = 80, stream: str = "both") -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    warnings = []
    if lines > MAX_JOB_TAIL_LINES:
        warnings.append(f"lines clamped from {lines} to {MAX_JOB_TAIL_LINES}")
        lines = MAX_JOB_TAIL_LINES
    if lines < 1:
        lines = 1
    try:
        supervisor = control(endpoint, job_id, "tail", lines=lines)
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_tail",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job tail for {job_id} failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job_id": job_id, "error": str(exc)[-4000:], "error_details": error_details(exc)},
        )
        return _failure_payload(result)
    requested: list[str] = []
    sections: list[str] = []
    missing: list[str] = []
    for name in ("stdout", "stderr"):
        if stream not in {name, "both"}:
            continue
        requested.append(name)
        if name not in supervisor:
            missing.append(name)
            continue
        body = str(supervisor.get(name) or "")
        sections.append(f"__{name.upper()}__\n{body}".rstrip() + ("\n" if body else ""))
    for name in missing:
        warnings.append(f"{name}.log does not exist in the remote job dir")
    all_missing = bool(requested) and len(missing) == len(requested)
    text = compact_text("".join(sections), limit=MAX_TEXT_CHARS)
    failed = all_missing or supervisor.get("state") == "absent"
    result = make_result(
        tool="remote.job_tail",
        target=endpoint.to_result_target(),
        outcome="failed" if failed else "success",
        status="log_not_found" if all_missing else "ok",
        summary=f"Remote job tail for {job_id}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"tail": text, "stderr": ""},
        warnings=warnings,
        extra={"job_id": job_id, "lines": lines, "missing_logs": missing, "state": supervisor.get("state")},
    )
    return {"text": text, "result": result}


def _output_budget_bytes(max_output_tokens: int | None) -> int:
    """Shared UTF-8 byte budget; reserve half for each output projection.

    Text and structuredContent expose the same preview. At four bytes/token,
    each receives two bytes/token; bounded status/refs metadata is separate.
    Cursors retain every unreturned byte.
    """
    if max_output_tokens is None:
        return 8192
    if int(max_output_tokens) < 1:
        raise ValueError("max_output_tokens must be positive")
    return max(4, min(MAX_INCREMENTAL_READ_BYTES, int(max_output_tokens) * 2))


@observed_tool("remote.job_stdin")
@pinned_endpoint
def remote_job_stdin(endpoint: Endpoint | None, *, job_id: str, chars: str | None = None,
                     eof: bool = False, yield_time_ms: int | None = None,
                     max_output_tokens: int | None = None) -> dict[str, Any]:
    endpoint, record, path = _load_record(endpoint, job_id)
    started, start = utc_now_iso(), time.monotonic()
    budget = _output_budget_bytes(max_output_tokens)
    delay = _yield_ms(yield_time_ms, 250 if chars or eof else 1000)
    # A long observation must not hold the local cursor lock: otherwise a
    # waiting poll also blocks stdin, consuming the reserved control workers.
    # Snapshot the cursor, wait unlocked, then commit only against that snapshot.
    observed = None
    if delay > 250:
        with record_lock(path):
            snapshot = json.loads(path.read_text(encoding="utf-8")).get("stdin_cursors", {})
        observed = control(endpoint, job_id, "exchange", data=chars or "", eof=bool(eof),
                           stdout_offset=int(snapshot.get("stdout_offset") or 0),
                           stderr_offset=int(snapshot.get("stderr_offset") or 0),
                           max_bytes=budget, shared_budget=True, yield_time_ms=delay)
    with record_lock(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        cursors = record.get("stdin_cursors", {})
        if observed is not None and cursors == snapshot:
            row = observed
        else:
            row = control(endpoint, job_id, "exchange", data="" if observed is not None else chars or "",
                          eof=False if observed is not None else bool(eof),
                          stdout_offset=int(cursors.get("stdout_offset") or 0),
                          stderr_offset=int(cursors.get("stderr_offset") or 0),
                          max_bytes=budget, shared_budget=True, yield_time_ms=0 if observed is not None else delay)
            if observed is not None:
                # Input was submitted once, before the cursor conflict. Preserve
                # its acknowledgement; only the output observation is repeated.
                for key in ("accepted", "written", "written_chars", "eof", "eof_deferred",
                            "stdin_buffer_full", "retryable", "reason", "cancellation_requested"):
                    if key in observed:
                        row[key] = observed[key]
        _save_output(record, path, row)
    return _session_result(endpoint, record, path, row, tool="remote.job_stdin", started=started, start=start, budget=budget)


@observed_tool("remote.job_stop")
@pinned_endpoint
def remote_job_stop(endpoint: Endpoint | None, *, job_id: str, force: bool = False) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "stop", force=force)
        deadline = time.monotonic() + STOP_DRAIN_SECONDS
        while not supervisor.get("quiet") and time.monotonic() < deadline:
            time.sleep(0.05)
            supervisor = control(endpoint, job_id, "status")
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool="remote.job_stop",
            target=endpoint.to_result_target(),
            outcome="failed",
            status="failed",
            summary=f"Remote job {job_id} stop failed.",
            started_at=started,
            duration_ms=_duration_ms(start),
            extra={"job_id": job_id, "error": str(exc)[-4000:], "error_details": error_details(exc)},
        )
        return _failure_payload(result)
    state = str(supervisor.get("state") or "unknown")
    quiet = bool(supervisor.get("quiet"))
    if quiet and state in {"cancelled", "succeeded", "failed", "timeout"}:
        outcome = "cancelled" if state == "cancelled" else "success"
        status = state
    elif quiet:
        outcome, status = "success", state
    else:
        outcome, status = "failed", state
    result = make_result(
        tool="remote.job_stop",
        target=endpoint.to_result_target(),
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"Remote job {job_id} {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"job_id": job_id, "quiet": quiet, "remote_status": supervisor},
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}
