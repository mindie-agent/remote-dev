"""
[Role] Remote background job lifecycle and interactive session orchestration (`job_ops.py`).
[Invariants]
- Enforces `STOP_DRAIN_SECONDS >= STOP_GRACE_SECONDS + 3.0` (`max(5.0, grace + 3.0)`) so `remote.job_stop` waits long enough for the remote supervisor's SIGTERM drain + SIGKILL escalation.
- Persists the local job record before launching the remote job and never replays an uncertain launch or acknowledged `stdin` write.
- Preserves acknowledged remote execution outcomes when local cursor/log commit fails (`_recording_failure`).
[Timeouts & Locks]
- Uses `record_lock(path)` around local cursor and log offset updates (`_save_output`), releasing the lock during long `yield_time_ms > 250` polls so concurrent `stdin` or `stop` calls do not block.
[Side-Effect Policy]
- Writes local `<job_id>.json` and `.stdout.log`/`.stderr.log` under `state/endpoints/<id>/jobs/` and drives remote `processes/worker.py` via `processes.control`.
"""
from __future__ import annotations

from remote_dev.observability import current_tool, observed_tool

import re
import json
import math
import copy
from dataclasses import asdict
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from remote_dev.core.endpoint import DEFAULT_CWD, DEFAULT_ROOT, Endpoint
from remote_dev.core.container_endpoint import FULL_CONTAINER_ID, pinned_endpoint
from remote_dev.core import container_endpoint as _container_endpoint
from remote_dev.core.errors import RemoteExecutionError, caller_error
from remote_dev.core.job_logs import format_job_log_output, job_log_refs, render_job_refs_text
from remote_dev.core.job_results import (
    UNKNOWN_JOB_STATES, TERMINAL_JOB_STATES, _public_job_record,
    _session_result, _recording_failure,
)
from remote_dev.core.preview import MAX_JOB_TAIL_LINES, MAX_TEXT_CHARS, compact_text_span, fold_repeated_lines
from remote_dev.core.locking import record_lock
from remote_dev.core.runtime_env import runtime_env_lines
from remote_dev.core.state_store import atomic_write_json, find_job_record, job_record_path, resolve_ledger_scope
from remote_dev.processes import control
from remote_dev.result import make_result, tool_text, utc_now_iso
from remote_dev.core.errors import error_details

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV_PREFIX = "REMOTE_DEV_JOB_"


def _default_stop_drain_seconds() -> float:
    override = os.environ.get("REMOTE_DEV_STOP_DRAIN_SECONDS")
    if override is not None:
        return float(override)
    grace = float(os.environ.get("REMOTE_DEV_STOP_GRACE_SECONDS", "2.0"))
    return max(5.0, grace + 3.0)


_INITIAL_STOP_DRAIN_SECONDS = _default_stop_drain_seconds()
STOP_DRAIN_SECONDS = _INITIAL_STOP_DRAIN_SECONDS


def _stop_drain_seconds() -> float:
    if STOP_DRAIN_SECONDS != _INITIAL_STOP_DRAIN_SECONDS:
        return float(STOP_DRAIN_SECONDS)
    return _default_stop_drain_seconds()


MAX_YIELD_MS = 600000
POSIX_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
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
    from .execution import timeout_value
    timeout_value(timeout_ms)
    if timeout_ms is None:
        return None
    return timeout_ms / 1000


def _job_command(endpoint: Endpoint, command: str, runtime_enabled: bool,
                 shell: str | None = None, login: bool | None = None) -> str:
    preamble = runtime_env_lines(endpoint, runtime_enabled)
    executed = command
    if preamble and (shell is not None or login is not None):
        chosen = shell or "bash"
        if os.path.basename(chosen) not in POSIX_SHELLS:
            raise caller_error("runtime_env requires a POSIX-family shell",
                               error_code="unsupported_shell_for_runtime_env", field="shell")
        login_flag = "-l " if login else ""
        inner = "\n".join([*preamble, f"eval {shlex.quote(chr(10) + command)}"])
        return f"exec {shlex.quote(chosen)} {login_flag}-c {shlex.quote(inner)}"
    if shell is not None or login is not None:
        chosen = shell or "bash"
        login_flag = "-l " if login else ""
        executed = f"exec {shlex.quote(chosen)} {login_flag}-c {shlex.quote(command)}"
    if not preamble:
        return executed
    # Parse the user's command after initialization, in the same shell. This
    # preserves runtime functions/options and avoids a second SSH .bashrc load.
    return "\n".join([*preamble, f"eval -- {shlex.quote(executed)}"])


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
        import json

        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise caller_error(
                f"unknown remote job id for endpoint: {job_id}", FileNotFoundError,
                error_code="unknown_job", field="job_id"
            ) from None
        data = json.loads(content)
        return endpoint, data, path
    found = find_job_record(job_id)
    if not found:
        raise caller_error(f"unknown remote job id: {job_id}", FileNotFoundError,
                            error_code="unknown_job", field="job_id")
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


def _save_output(record, path, row, *, cursor_key="stdin_cursors"):
    # Hold the record lock from read through exchange and cursor commit.
    # Overwrite from the committed length after a crash before cursor commit.
    # Do not expose a cursor that the local transaction did not commit. If a
    # file write or the receipt fails, the remote observation still exists and
    # the next read resumes the previous committed cursor without replaying input.
    updated = copy.deepcopy(record)
    cursors = updated.setdefault(cursor_key, {})
    local_offsets = updated.setdefault("local_output_offsets", {})
    for name in ("stdout", "stderr"):
        log = path.with_name(path.stem + "." + name + ".log")
        with log.open("r+b" if log.exists() else "w+b") as stream:
            stream.seek(int(local_offsets.get(name, 0)))
            stream.write(str(row.get(name) or "").encode("utf-8"))
            stream.truncate()
            local_offsets[name] = stream.tell()
        cursors[name + "_offset"] = int(row.get(name + "_offset", cursors.get(name + "_offset", 0)))
    updated["state"] = row.get("state", "unknown")
    atomic_write_json(path, updated)
    record.clear()
    record.update(updated)


@pinned_endpoint
def start_remote_job(
    endpoint: Endpoint, *, command: str, cwd: str | None = None,
    env: dict[str, str] | None = None, timeout_ms: int | None = None,
    runtime_env: bool | None = None, description: str | None = None,
    job_id: str | None = None, yield_time_ms: int | None = None,
    max_output_tokens: int | None = None, tty: bool = False, wait: bool = False,
    shell: str | None = None, login: bool | None = None, foreground: bool = False,
) -> dict[str, Any]:
    started, start = utc_now_iso(), time.monotonic()
    env = env or {}
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    job_id = require_job_id(job_id or new_job_id())
    cwd = cwd or endpoint.effective_cwd
    path = job_record_path(endpoint, job_id)
    spec = {"command": _job_command(endpoint, command, runtime_enabled, shell=shell, login=login), "cwd": cwd,
            "env": {require_env_name(key): str(value) for key, value in env.items()},
            "timeout_seconds": _timeout_seconds(timeout_ms), "interactive": not wait, "tty": bool(tty)}
    record = {"schema_version": "remote-dev.job.v1", "job_id": job_id,
              "description": description, "target": {**endpoint.to_result_target(), "cwd": cwd},
              "connection": {**asdict(endpoint), "cwd": cwd}, "command_preview": command[:500],
              "cwd": cwd, "env_keys": sorted(env), "runtime_env": runtime_enabled,
              "runtime_env_file": endpoint.runtime_env_file, "remote_dir": remote_job_dir(endpoint, job_id),
              "started_at": started, "timeout_ms": timeout_ms, "tty": bool(tty),
              "shell": shell, "login": login,
              "session_scope": resolve_ledger_scope(),
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
            try:
                _save_output(record, path, row)
            except TimeoutError:
                raise
            except (OSError, ValueError, RuntimeError) as error:
                return _recording_failure(endpoint, record, path, row, error, tool="remote.bash", started=started, start=start, budget=budget)
            first_output = {name: str(row.get(name) or "") for name in ("stdout", "stderr")}
            while wait and (not row.get("quiet") or any(row.get(name + "_bytes_remaining") for name in ("stdout", "stderr"))):
                if row.get("state") in UNKNOWN_JOB_STATES | {"absent"} or row.get("unknown"):
                    break
                row = control(endpoint, job_id, "exchange", **record["stdin_cursors"],
                              max_bytes=MAX_INCREMENTAL_READ_BYTES, shared_budget=True,
                              wait_for_exit=True, yield_time_ms=1000)
                try:
                    _save_output(record, path, row)
                except TimeoutError:
                    raise
                except (OSError, ValueError, RuntimeError) as error:
                    return _recording_failure(endpoint, record, path, row, error, tool="remote.bash", started=started, start=start, budget=budget)
                for name in ("stdout", "stderr"):
                    left = max(0, budget - sum(len(body.encode("utf-8")) for body in first_output.values()))
                    first_output[name] += str(row.get(name) or "").encode("utf-8")[:left].decode("utf-8", "ignore")
            if wait:
                row.update(first_output)
        except TimeoutError:
            raise
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
    pending = {name: int(row.get(name + "_bytes_remaining") or 0) for name in ("stdout", "stderr")}
    tail_sources = None
    preview_error = None
    if foreground and row.get("state") in TERMINAL_JOB_STATES and any(pending.values()) and not wait:
        # Keep both preview reads inside a small shared deadline. This is a
        # secondary decoration of an already completed command, never a reason
        # to lose its observed exit/refs or to extend host tool deadlines.
        preview_deadline = time.monotonic() + 6.0
        try:
            heads = control(endpoint, job_id, "tail", max_bytes=65536,
                            stdout_offset=0, stderr_offset=0,
                            timeout_ms=max(250, int((preview_deadline - time.monotonic()) * 1000)))
            tails = control(endpoint, job_id, "tail", lines=MAX_JOB_TAIL_LINES,
                            timeout_ms=max(250, int((preview_deadline - time.monotonic()) * 1000)))
        except (TimeoutError, RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
            heads, tails = {}, {}
            preview_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        if heads or tails:
            tail_sources = {
                "head": {name: str((heads or {}).get(name) or "") for name in ("stdout", "stderr")},
                "tail": {name: str((tails or {}).get(name) or "") for name in ("stdout", "stderr")},
            }
    return _session_result(
        endpoint, record, path, row, tool="remote.bash", started=started, start=start,
        budget=budget, tail_sources=tail_sources, preview_error=preview_error,
        prefer_cache_edges=bool(foreground and wait and not any(pending.values())
                                and row.get("state") in TERMINAL_JOB_STATES),
    )


@observed_tool("remote.job_status")
@pinned_endpoint
def remote_job_status(endpoint: Endpoint | None, *, job_id: str) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "status")
    except TimeoutError:
        raise
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
        outcome="failed" if status in UNKNOWN_JOB_STATES | {"absent"} or supervisor.get("unknown") else "success",
        status=status,
        summary=f"Remote job {job_id} is {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={"job": _public_job_record(
            record, state=status, remote_status=supervisor, quiet=supervisor.get("quiet")
        )},
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}


def _job_tail_impl(
    endpoint: Endpoint | None,
    *,
    job_id: str,
    lines: int = 80,
    stream: str = "both",
    yield_time_ms: int | None = None,
    max_output_tokens: int | None = None,
    timeout_ms: int | None = None,
    tool: str = "remote.job_tail",
) -> dict[str, Any]:
    from .execution import timeout_value
    timeout_value(timeout_ms)
    started = utc_now_iso()
    start = time.monotonic()
    absolute_deadline = None if timeout_ms is None else start + int(timeout_ms) / 1000.0

    def _remaining_s() -> float | None:
        if absolute_deadline is None:
            return None
        return absolute_deadline - time.monotonic()

    if endpoint is not None and endpoint.container is not None:
        remaining = _remaining_s()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"timeout_ms={timeout_ms} elapsed before the container pin")
        endpoint = _container_endpoint.pin_container_endpoint(
            endpoint, timeout_ms=None if remaining is None else max(1, math.ceil(remaining * 1000)))
    endpoint, record, record_path = _load_record(endpoint, job_id)
    warnings = []
    if lines > MAX_JOB_TAIL_LINES:
        warnings.append(f"lines clamped from {lines} to {MAX_JOB_TAIL_LINES}")
        lines = MAX_JOB_TAIL_LINES
    if lines < 1:
        lines = 1
    budget = None if max_output_tokens is None else _output_budget_bytes(max_output_tokens)
    def _tail_control() -> dict[str, Any]:
        remaining = _remaining_s()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"timeout_ms={timeout_ms} elapsed before the job tail control call")
        remaining_ms = None if remaining is None else max(1, math.ceil(remaining * 1000))
        return control(endpoint, job_id, "tail", lines=lines, timeout_ms=remaining_ms)

    try:
        supervisor = _tail_control()
        if yield_time_ms is not None and int(yield_time_ms) > 0:
            wait_sec = min(20.0, max(0.0, int(yield_time_ms) / 1000.0))
            deadline = time.monotonic() + wait_sec
            if absolute_deadline is not None:
                deadline = min(deadline, absolute_deadline)
            initial_out = (supervisor.get("stdout"), supervisor.get("stderr"))
            while (
                supervisor.get("state") in {"prepared", "running"}
                and (supervisor.get("stdout"), supervisor.get("stderr")) == initial_out
                and time.monotonic() < deadline
            ):
                time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    break
                supervisor = _tail_control()
    except TimeoutError:
        raise
    except (RemoteExecutionError, ValueError, RuntimeError, OSError) as exc:
        result = make_result(
            tool=tool,
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
    streams: list[tuple[str, str]] = []
    missing: list[str] = []
    folded_bytes = 0
    for name in ("stdout", "stderr"):
        if stream not in {name, "both"}:
            continue
        requested.append(name)
        if name not in supervisor:
            missing.append(name)
            continue
        raw = str(supervisor.get(name) or "")
        compacted = fold_repeated_lines(raw) if budget is None else raw
        folded_bytes += len(raw.encode("utf-8")) - len(compacted.encode("utf-8"))
        streams.append((name, compacted))
    for name in missing:
        warnings.append(f"{name}.log does not exist in the remote job dir")
    all_missing = bool(requested) and len(missing) == len(requested)
    refs = job_log_refs(
        job_id=job_id,
        record_path=record_path,
        record=record,
        endpoint_id=endpoint.endpoint_id,
    )
    refs_text = render_job_refs_text(refs)
    body_limit = max(1, MAX_TEXT_CHARS - (len(refs_text) + 3 if refs_text else 0))
    total_raw_bytes = sum(len(raw.encode("utf-8")) for _name, raw in streams)
    omitted_bytes = folded_bytes
    if budget is not None:
        # The budget applies to selected log bytes only; stream labels and refs
        # are rendered metadata outside it. Newest bytes win, stderr last.
        label_chars = sum(len(f"__{name.upper()}__\n") + 1 for name, _raw in streams)
        remaining = min(budget, max(1, body_limit - label_chars))
        kept_bodies: dict[str, str] = {}
        for name, raw in reversed(streams):
            encoded = raw.encode("utf-8")
            if len(encoded) > remaining:
                kept = encoded[len(encoded) - remaining:] if remaining > 0 else b""
                while kept and (kept[0] & 0xC0) == 0x80:
                    kept = kept[1:]
            else:
                kept = encoded
            remaining -= len(kept)
            kept_bodies[name] = kept.decode("utf-8")
        kept_bytes = sum(len(value.encode("utf-8")) for value in kept_bodies.values())
        omitted_bytes = total_raw_bytes - kept_bytes
        rendered = ""
        for name, _raw in streams:
            part = kept_bodies[name]
            rendered += f"__{name.upper()}__\n" + part + ("" if part.endswith("\n") or not part else "\n")
        if omitted_bytes:
            warnings.append(f"output truncated to max_output_tokens budget; {omitted_bytes} earlier bytes omitted from this preview (log refs and stream labels are separate)")
        body = "".join(kept_bodies[name] for name, _raw in streams)
        text_body = rendered.rstrip("\n")
    else:
        pieces: list[tuple[bool, str]] = []
        for name, raw in streams:
            pieces.append((False, f"__{name.upper()}__\n"))
            if raw:
                pieces.append((True, raw))
                if not raw.endswith("\n"):
                    pieces.append((False, "\n"))
        joined = "".join(part for _is_raw, part in pieces)
        compacted, cut_start, cut_end = compact_text_span(joined, limit=body_limit, fold_repeats=False)
        if cut_end > cut_start:
            offset = 0
            for is_raw, part in pieces:
                lo, hi = max(offset, cut_start), min(offset + len(part), cut_end)
                if is_raw and hi > lo:
                    omitted_bytes += len(part[lo - offset:hi - offset].encode("utf-8"))
                offset += len(part)
            warnings.append(f"output truncated to the text size limit; {omitted_bytes} raw log bytes omitted from this preview (full logs are available via refs)")
        body = compacted.rstrip("\n")
        text_body = body
    text = text_body
    if refs_text:
        text = (text + "\n\n" + refs_text) if text else refs_text
    text = text.rstrip("\n") + "\n" if text else ""
    failed = all_missing or supervisor.get("state") == "absent"
    result = make_result(
        tool=tool,
        target=endpoint.to_result_target(),
        outcome="failed" if failed else "success",
        status="log_not_found" if all_missing else "ok",
        summary=f"Remote job tail for {job_id}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview={"tail": body, "stderr": ""},
        refs=refs,
        warnings=warnings,
        extra={"job_id": job_id, "lines": lines, "missing_logs": missing, "state": supervisor.get("state"),
               "truncated": omitted_bytes > 0, "omitted_bytes": omitted_bytes,
               "repeated_lines_folded": folded_bytes > 0},
    )
    return {"text": text, "result": result}


@observed_tool("remote.job_tail")
def remote_job_tail(
    endpoint: Endpoint | None, *, job_id: str, lines: int = 80, stream: str = "both", yield_time_ms: int | None = None,
    max_output_tokens: int | None = None, timeout_ms: int | None = None
) -> dict[str, Any]:
    return _job_tail_impl(endpoint, job_id=job_id, lines=lines, stream=stream, yield_time_ms=yield_time_ms,
                          max_output_tokens=max_output_tokens, timeout_ms=timeout_ms, tool="remote.job_tail")


@observed_tool("remote.job_output")
def remote_job_output(
    endpoint: Endpoint | None, *, job_id: str, lines: int = 200, stream: str = "both", yield_time_ms: int | None = None,
    max_output_tokens: int | None = None, timeout_ms: int | None = None
) -> dict[str, Any]:
    return _job_tail_impl(endpoint, job_id=job_id, lines=lines, stream=stream, yield_time_ms=yield_time_ms,
                          max_output_tokens=max_output_tokens, timeout_ms=timeout_ms, tool="remote.job_output")


@observed_tool("remote.job_output")
@pinned_endpoint
def remote_job_output_incremental(
    endpoint: Endpoint | None,
    *,
    job_id: str,
    wait: bool = False,
    timeout_ms: int | None = None,
    max_bytes: int = MAX_INCREMENTAL_READ_BYTES,
) -> dict[str, Any]:
    """Consuming read used by DSH ``job_output``.

    Returns only bytes after this job's committed output cursor. With
    ``wait=True`` it keeps polling until the job reaches a terminal state or
    the caller's timeout budget expires; a timed-out wait leaves the job
    running. Cursors are committed under the same record lock that guards
    stdin, so concurrent readers never replay acknowledged output.
    """
    endpoint, record, path = _load_record(endpoint, job_id)
    started, start = utc_now_iso(), time.monotonic()
    wait_ms = 30000 if timeout_ms is None else min(int(timeout_ms), 600000)
    deadline = start + (max(0, wait_ms) / 1000.0) if wait else start
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    row: dict[str, Any] = {}
    attempts = 0
    while True:
        with record_lock(path):
            live = json.loads(path.read_text(encoding="utf-8"))
            snapshot = dict(live.get("output_cursors") or {})
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000)) if wait else 0
        delay = min(1000, remaining_ms) if wait else 0
        row = control(
            endpoint, job_id, "exchange", data="", eof=False,
            stdout_offset=int(snapshot.get("stdout_offset") or 0),
            stderr_offset=int(snapshot.get("stderr_offset") or 0),
            max_bytes=max_bytes, shared_budget=True, yield_time_ms=delay,
        )
        with record_lock(path):
            live = json.loads(path.read_text(encoding="utf-8"))
            current = dict(live.get("output_cursors") or {})
            if current != snapshot and attempts < 3:
                attempts += 1
                continue
            _save_output(live, path, row, cursor_key="output_cursors")
        stdout_parts.append(str(row.get("stdout") or ""))
        stderr_parts.append(str(row.get("stderr") or ""))
        state = str(row.get("state") or "unknown")
        if not wait or state in TERMINAL_JOB_STATES or time.monotonic() >= deadline:
            break
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    state = str(row.get("state") or "unknown")
    exit_code = (row.get("result") or {}).get("exit_code") if state in TERMINAL_JOB_STATES else None
    record = json.loads(path.read_text(encoding="utf-8"))
    pending = {name: int(row.get(name + "_bytes_remaining") or 0) for name in ("stdout", "stderr")}
    refs = job_log_refs(
        job_id=job_id,
        record_path=path,
        record=record,
        endpoint_id=endpoint.endpoint_id,
        observed_offsets={name: int(row.get(name + "_offset") or 0) for name in ("stdout", "stderr")},
        unread_bytes=pending,
    )
    formatted = format_job_log_output(stdout, stderr, refs=refs, preserve_full_page=True)
    result = make_result(
        tool="remote.job_output",
        target=endpoint.to_result_target(),
        outcome="success",
        status="ok",
        summary=f"Read output from remote job {job_id}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        preview=formatted["preview"],
        refs=refs,
        extra={
            "job_id": job_id,
            "state": state,
            "wait": bool(wait),
            "exit_code": exit_code,
            "output_preview": {
                "truncated": formatted["truncated"],
                "omitted_chars": formatted["omitted_chars"],
                "omitted_bytes": formatted["omitted_bytes"],
                "omitted_chars_exact": formatted["omitted_chars_exact"],
                "repeated_lines_folded": formatted["repeated_lines_folded"],
                "local_cache_status": refs.get("local_cache_status"),
            },
        },
    )
    return {"text": formatted["text"], "result": result}


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
        try:
            _save_output(record, path, row)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            return _recording_failure(endpoint, record, path, row, error, tool="remote.job_stdin", started=started, start=start, budget=budget, preserve_full_page=True)
    return _session_result(endpoint, record, path, row, tool="remote.job_stdin", started=started, start=start, budget=budget, preserve_full_page=True)


@observed_tool("remote.job_stop")
@pinned_endpoint
def remote_job_stop(endpoint: Endpoint | None, *, job_id: str, force: bool = False) -> dict[str, Any]:
    endpoint, record, _record_path = _load_record(endpoint, job_id)
    started = utc_now_iso()
    start = time.monotonic()
    try:
        supervisor = control(endpoint, job_id, "stop", force=force)
        deadline = time.monotonic() + _stop_drain_seconds()
        while not supervisor.get("quiet") and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            supervisor = control(endpoint, job_id, "status", timeout_ms=min(5000, remaining_ms))
    except TimeoutError:
        raise
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
    descendants_drained = bool((supervisor.get("result") or {}).get("descendants_drained"))
    result = make_result(
        tool="remote.job_stop",
        target=endpoint.to_result_target(),
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"Remote job {job_id} {status}.",
        started_at=started,
        duration_ms=_duration_ms(start),
        extra={
            "job_id": job_id,
            "quiet": quiet,
            "descendants_drained": descendants_drained,
            "remote_status": supervisor,
        },
    )
    return {"text": f"Remote job {job_id}: {status}\n", "result": result}
