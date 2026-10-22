"""
[Role] SSH argv builder, ControlMaster policy, one-shot script/bytes/RPC execution (`ssh_transport.py`).
[Invariants]
- Places all SSH options before `--` and uses `-l <user> -p <port> -- <host>` so user/host values cannot be parsed as OpenSSH flags.
- Refuses shared `ControlMaster` multiplexing on attached streams (`run_stream`) and `-N -L` port forwards (`open_local_forward`), because OpenSSH mux silently exits `rc=0` while tearing down tunnels.
- Re-exports every public and private symbol from `ssh_stream` and `ssh_forward`; callers and monkeypatches keep using `remote_dev.core.ssh_transport`.
[Timeouts & Locks]
- Enforces caller `timeout_ms` both remotely (`timeout --preserve-status`) and locally (`STREAM_SELECT_SLICE_SECONDS = 0.1` + `OwnedProcess.stop()`).
[Side-Effect Policy]
- Lazily prepares `~/.ssh/remote-dev-mux` (`0o700`) once per process when multiplexing is active; spawns local SSH processes via `OwnedProcess`.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
import queue
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .endpoint import Endpoint
from .container_endpoint import pin_container_endpoint, pinned_endpoint
from .errors import RemoteExecutionError, record_cleanup_failure
from .cancellation import current_event
from .execution import timeout_value
from .local_process import OwnedProcess
from remote_dev.observability import confirmed_failure, observed_operation

_MUX_DIR = Path(os.environ.get("REMOTE_DEV_SSH_MUX_DIR") or (Path.home() / ".ssh" / "remote-dev-mux")).expanduser()

# Decide mux-dir readiness once per process. None = undecided, True/False =
# usable / not usable.
_MUX_READY: bool | None = None

# Process-scoped multiplexing *default*. Unset or exact "1" keeps the shared
# ControlMaster; exact "0" forces independent connections. An endpoint's
# ``ssh_mux`` field overrides this for that endpoint only. Read on each
# invocation; never written back to os.environ or cached as a module global.
SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"

# Local reader slice. Small enough that a wall-clock deadline is honoured
# promptly, large enough that a quiet-but-alive stream is not spun on.
# POSIX uses ``select`` on pipes; Windows uses reader threads (select cannot
# wait on subprocess pipes there).
STREAM_SELECT_SLICE_SECONDS = 0.1

# Keepalive for endpoints that set the ``keepalive`` mechanism flag.
# Conditional: see ``_keepalive_options``. Hour-scale streams go through
# ``Endpoint.for_long_stream``, not this flag alone.
SERVER_ALIVE_INTERVAL_SECONDS = 30
SERVER_ALIVE_COUNT_MAX = 10

STREAM_MUX_REFUSAL = (
    "attached streams cannot use a multiplexed SSH connection: ControlMaster "
    "delegates -N forwards and hour-scale sessions to the mux master and the "
    "client exits rc=0 immediately, tearing the stream down. That failure is "
    "silent — rc=0 with the work gone. OpenSSH first-option-wins makes a later "
    "ControlMaster=no override ineffective, and ControlMaster=no alone is not "
    "enough because a client can still attach to an existing ControlPath. "
    "Use Endpoint.for_long_stream(...) or set ssh_mux=False before building "
    "the command."
)

INTERACTIVE_MUX_REFUSAL = (
    "interactive bootstrap cannot use a multiplexed SSH connection: a "
    "password prompt through a ControlMaster is meaningless and hangs. "
    "It is impossible to combine BatchMode=no with multiplexing. Pass "
    "ssh_mux=False on the endpoint (do not attach this session to a mux "
    "master)."
)

# Native Windows supports independent connections only.
WINDOWS_MUX_UNSUPPORTED = (
    "OpenSSH ControlMaster is not supported by remote-dev on native Windows. "
    "Ordinary Windows connections already use an independent SSH connection "
    "(ControlMaster=no, ControlPath=none, ControlPersist=no); no extra "
    "flag is required. Do not set ssh_mux=True or REMOTE_DEV_SSH_MUX=1."
)

# OpenSSH -N through a mux master exits 0 while the tunnel is gone. A dead
# forward is never reported as success.
FORWARD_DEAD_EXIT_CODE = 255

@dataclass
class RemoteCompleted:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    timings: dict[str, float | None] = field(default_factory=dict)
    cancelled: bool = False
    cleanup_error: str | None = None

@dataclass
class RemoteBytesCompleted:
    """Binary SDK result with explicit execution and cleanup facts."""
    args: list[str]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    cleanup_error: str | None = None

def _capture_command(command, *, input=None, timeout=None):
    """Capture one owned SSH process; quiet work has no default deadline."""
    from .cancellation import current_event
    cancelled = current_event()
    if cancelled is not None and cancelled.is_set():
        raise RemoteExecutionError("SSH capture cancelled before launch", category="cancelled", submission_state="not_sent")
    owner = OwnedProcess(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = owner.process
    deadline = None if timeout is None else time.monotonic() + timeout
    first_input = input
    stdout = stderr = b""
    observed_exit = None
    timed_out = was_cancelled = False
    cleanup = []
    primary = None
    try:
        while True:
            was_cancelled = cancelled is not None and cancelled.is_set()
            timed_out = deadline is not None and time.monotonic() >= deadline
            if was_cancelled or timed_out:
                observed_exit = proc.poll()
                try:
                    owner.stop()
                    stdout, stderr = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired as exc:
                    stdout, stderr = exc.stdout or stdout, exc.stderr or stderr
                    cleanup.append("SSH output pipes did not close after cancellation")
                except Exception as exc:
                    cleanup.append(f"{type(exc).__name__}: {exc}")
                break
            try:
                stdout, stderr = proc.communicate(input=first_input, timeout=.05)
                observed_exit = proc.returncode
                break
            except subprocess.TimeoutExpired as exc:
                first_input = None
                stdout, stderr = exc.stdout or stdout, exc.stderr or stderr
                if proc.poll() is not None:
                    # The leader exited. A descendant retaining an inherited
                    # pipe is cleanup work, not an ongoing SSH operation.
                    observed_exit = proc.returncode
                    try:
                        owner.stop()
                        stdout, stderr = proc.communicate(timeout=1)
                    except subprocess.TimeoutExpired as tail:
                        stdout, stderr = tail.stdout or stdout, tail.stderr or stderr
                        cleanup.append("SSH output pipes did not close after process exit")
                    except Exception as cleanup_error:
                        cleanup.append(f"{type(cleanup_error).__name__}: {cleanup_error}")
                    break
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            owner.stop()
        except Exception as exc:
            if primary is not None:
                record_cleanup_failure(primary, exc)
            else:
                cleanup.append(f"{type(exc).__name__}: {exc}")
    # Keep the observed exit before cleanup; a later SIGKILL is not an ACK.
    return RemoteBytesCompleted(list(command), observed_exit, stdout, stderr, timed_out, was_cancelled,
                                "; ".join(cleanup) if cleanup else None)

def _control_master_options(identity_file: str | None = None) -> list[str]:
    """OpenSSH connection reuse through a ControlMaster socket directory (prepared once)."""
    global _MUX_READY
    if _MUX_READY is None:
        try:
            _MUX_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(_MUX_DIR, 0o700)
            _MUX_READY = True
        except TimeoutError:
            raise
        except OSError as exc:
            sys.stderr.write(
                f"[remote-dev] WARNING: SSH ControlMaster disabled; could not "
                f"prepare {_MUX_DIR} ({exc}). Remote tool calls will pay a fresh "
                f"SSH handshake each time. Fix ~/.ssh permissions to restore reuse.\n"
            )
            _MUX_READY = False
    if not _MUX_READY:
        return []
    # OpenSSH's %C hashes host/port/user but not the identity file; without a
    # per-key suffix two endpoints that differ only by SSH key would silently
    # share one master connection.
    key_suffix = ""
    if identity_file:
        key_suffix = "-" + hashlib.sha256(identity_file.encode("utf-8")).hexdigest()[:12]
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={_MUX_DIR}/%C{key_suffix}",
        "-o",
        "ControlPersist=120",
    ]

def _independent_ssh_connection_options() -> list[str]:
    # ControlMaster=no alone is not enough: a client can still attach to an
    # existing ControlPath. ControlPath=none blocks socket reuse, and
    # ControlPersist=no blocks inherited persistence.
    return [
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
    ]

def _shared_mux_requested() -> bool:
    """Read the process-wide REMOTE_DEV_SSH_MUX default without mutating globals."""
    value = os.environ.get(SSH_MUX_ENV)
    if value is None or value == "1":
        return True
    if value == "0":
        return False
    raise RemoteExecutionError(
        f"{SSH_MUX_ENV}={value!r} is not supported; accepted values are unset, "
        f"'1' (shared ControlMaster), or '0' (independent connections)"
    )

def _native_windows_ssh_client() -> bool:
    return os.name == "nt"

def _windows_ssh_executable() -> str:
    """Resolve Git for Windows SSH without probing or retrying a connection."""
    git = shutil.which("git")
    if git:
        try:
            executable = Path(git).resolve(strict=True)
            parent = executable.parent
            if parent.name.casefold() in {"cmd", "bin"}:
                root = parent.parent
                if parent.name.casefold() == "bin" and root.name.casefold() in {"mingw64", "mingw32"}:
                    root = root.parent
                ssh = root / "usr" / "bin" / "ssh.exe"
                if ssh.is_file():
                    return str(ssh)
        except TimeoutError:
            raise
        except (OSError, RuntimeError) as exc:
            raise RemoteExecutionError(
                "Git for Windows SSH could not be resolved",
                category="configuration", submission_state="not_sent",
            ) from exc
    raise RemoteExecutionError(
        "Native Windows SSH requires Git for Windows with its bundled usr/bin/ssh.exe; "
        "make the installed Git executable available to this process",
        category="configuration", submission_state="not_sent",
    )

def _ssh_executable() -> str:
    return _windows_ssh_executable() if _native_windows_ssh_client() else "ssh"

def _uses_shared_mux(endpoint: Endpoint) -> bool:
    """Choose ControlMaster reuse per endpoint before argv is built.

    An explicit ``endpoint.ssh_mux`` wins, else ``REMOTE_DEV_SSH_MUX`` decides
    (unset/``1`` shared, ``0`` independent). Native Windows is independent
    only: an explicit shared request raises. OpenSSH first-option-wins means
    the choice cannot be overridden later, so ``ssh -N -L`` tunnels and
    hour-scale attached streams must use ``ssh_mux=False``.
    """
    if endpoint.ssh_mux is False:
        return False
    if _native_windows_ssh_client():
        value = os.environ.get(SSH_MUX_ENV)
        if endpoint.ssh_mux is True or value == "1":
            raise RemoteExecutionError(WINDOWS_MUX_UNSUPPORTED)
        if value is None or value == "0":
            return False
        raise RemoteExecutionError(
            f"{SSH_MUX_ENV}={value!r} is not supported; accepted values are unset, "
            f"'1' (shared ControlMaster), or '0' (independent connections)"
        )
    if endpoint.ssh_mux is not None:
        return bool(endpoint.ssh_mux)
    return _shared_mux_requested()

def _keepalive_options(endpoint: Endpoint) -> list[str]:
    """ServerAlive probes only when the endpoint sets the ``keepalive`` flag.

    It adds TCP probes, not a stream-long mux policy: hour-scale streams use
    ``Endpoint.for_long_stream`` (``ssh_mux=False`` + ``keepalive=True``).
    """
    if not endpoint.keepalive:
        return []
    return [
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_SECONDS}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
    ]

def _ssh_cmd(endpoint: Endpoint, option_tokens: Sequence[str] = ()) -> list[str]:
    """Compose SSH argv with owned options before ``--`` and the host.

    This helper is the only place that emits ``--``; anything after the host
    is a remote command, never an option. ``option_tokens`` is internal only.
    """
    if _uses_shared_mux(endpoint):
        mux_options = _control_master_options(endpoint.identity_file)
    else:
        mux_options = _independent_ssh_connection_options()
    cmd = [
        _ssh_executable(),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        *([] if endpoint.connect_timeout_ms is None else
          ["-o", f"ConnectTimeout={max(1, int(endpoint.connect_timeout_ms / 1000))}"]),
        *mux_options,
        *_keepalive_options(endpoint),
        *option_tokens,
    ]
    if endpoint.identity_file:
        cmd.extend(["-i", endpoint.identity_file])
    # User and host are never positional options. `-l` consumes `user`
    # even when it begins with `-`, and `--` stops option parsing before
    # `host`. `Endpoint.destination()` (`user@host`) is display-only.
    cmd.extend(["-l", endpoint.user, "-p", str(endpoint.port), "--", endpoint.host])
    return cmd

def ssh_base_cmd(endpoint: Endpoint) -> list[str]:
    if endpoint.container:
        raise RemoteExecutionError("ssh_base_cmd is a host-only argv prefix; use ssh_command for a container endpoint")
    return _ssh_cmd(endpoint)

def ssh_command(endpoint: Endpoint, *remote_tokens: str) -> list[str]:
    """Build a complete SSH command, executing only inside a selected container.

    Container argv is parsed and re-quoted so operators cannot escape into the
    host shell; Docker's configured user and environment are preserved.
    """
    endpoint = pin_container_endpoint(endpoint)
    if not endpoint.container:
        return [*ssh_base_cmd(endpoint), *remote_tokens]
    if not remote_tokens:
        raise RemoteExecutionError("container SSH command requires an explicit remote command")
    from dataclasses import replace
    host = replace(endpoint, container=None, container_selector=None)
    argv = shlex.split(" ".join(remote_tokens))
    if not argv:
        raise RemoteExecutionError("container SSH command requires an explicit remote command")
    command = shlex.join(["docker", "exec", "-i", endpoint.container, *argv])
    return [*ssh_base_cmd(host), command]

def stream_remote_payload(script: str, timeout_ms: int | None) -> str:
    """Wrap ``script`` in remote-side ``timeout --preserve-status`` when asked."""
    timeout_value(timeout_ms)
    if timeout_ms is None:
        return script
    timeout_s = timeout_ms / 1000
    margin = f"{timeout_s:g}"
    return f"timeout --preserve-status {margin}s bash -lc {shlex.quote(script)}"

def _require_independent_stream(endpoint: Endpoint) -> None:
    """Refuse a multiplexed endpoint; first-option-wins prevents a later fix."""
    if _uses_shared_mux(endpoint):
        raise RemoteExecutionError(STREAM_MUX_REFUSAL)

@pinned_endpoint
def stream_ssh_command(endpoint: Endpoint, script: str | None, *, timeout_ms: int | None = None) -> list[str]:
    """Argv for an attached stream; ``script=None`` reads it from binary stdin."""
    timeout_value(timeout_ms)
    _require_independent_stream(endpoint)
    if script is None:
        remote = "bash -s"
        if timeout_ms is not None and timeout_ms > 0:
            margin = f"{timeout_ms / 1000:g}"
            remote = f"timeout --preserve-status {margin}s bash -ls"
        return ssh_command(endpoint, remote)
    return ssh_command(endpoint, "bash", "-c", shlex.quote(stream_remote_payload(script, timeout_ms)))

@pinned_endpoint
@observed_operation("ssh.script", level="DEBUG")
def run_script(endpoint: Endpoint, script: str, *, timeout_ms: int | None = None,
               trace_connection: bool = False) -> RemoteCompleted:
    timeout_value(timeout_ms)
    started = time.perf_counter()
    timeout = None if timeout_ms is None else timeout_ms / 1000
    command = ssh_command(endpoint, "bash", "-s")
    payload = script.encode("utf-8")
    prepared = time.perf_counter()
    if trace_connection:
        # Used by the fixed diagnostic probe only. Ordinary command stderr and
        # process execution retain their existing blocking transport path.
        if len(payload) > 4096:
            raise ValueError("connection trace is restricted to small diagnostic probes")
        command.insert(1, "-v")
        return _run_traced_script(command, payload, timeout_ms, started, prepared)
    # Byte-oriented stdin preserves LF on Windows and binary payloads.
    proc = _capture_command(command, input=payload, timeout=timeout)
    code, stdout, stderr, timed_out = proc.returncode, proc.stdout, proc.stderr, getattr(proc, 'timed_out', False)
    finished = time.perf_counter()
    stdout, stderr = _decode_stream(stdout), _decode_stream(stderr)
    decoded = time.perf_counter()
    timings = {"prepare_ms": round((prepared - started) * 1000, 3),
               "ssh_process_ms": round((finished - prepared) * 1000, 3),
               "decode_ms": round((decoded - finished) * 1000, 3),
               "total_ms": round((decoded - started) * 1000, 3),
               # subprocess.run cannot distinguish handshake, remote execution
               # and transfer. Explicit probes may add a remote command timer.
               "connection_ms": None, "remote_execution_ms": None, "transfer_ms": None}
    return RemoteCompleted(code, stdout, stderr, timed_out=timed_out, timings=timings,
                           cancelled=getattr(proc, 'cancelled', False), cleanup_error=getattr(proc, 'cleanup_error', None))

def _run_traced_script(command, payload, timeout_ms, started, prepared):
    milestones = {}

    def observe(channel, text):
        now = time.perf_counter()
        if channel == "stderr":
            if "debug1: Connection established." in text:
                milestones.setdefault("tcp_connected", now)
            if text.startswith("Authenticated to ") or "Authentication succeeded (" in text:
                milestones.setdefault("authenticated", now)
        elif text:
            milestones.setdefault("first_output", now)
            milestones["last_output"] = now

    with OwnedProcess(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, bufsize=0) as owner:
        proc = owner.process
        try:
            proc.stdin.write(payload)
        except BrokenPipeError:
            pass  # Read the SSH error and exit code below.
        finally:
            proc.stdin.close()
        result = _read_attached(owner, timeout_ms=timeout_ms, forward_prefix="", output=None,
                                on_output=observe, capture=True)
    finished = time.perf_counter()
    timings = {"prepare_ms": round((prepared - started) * 1000, 3),
               "ssh_process_ms": round((finished - prepared) * 1000, 3),
               "total_ms": round((finished - started) * 1000, 3),
               "connection_ms": None, "tcp_connect_ms": None,
               "remote_execution_ms": None, "transfer_ms": None,
               "decode_ms": None, "drain_and_exit_ms": None}
    for key, name in (("tcp_connected", "tcp_connect_ms"), ("authenticated", "connection_ms")):
        if key in milestones:
            timings[name] = round((milestones[key] - prepared) * 1000, 3)
    if "last_output" in milestones:
        timings["drain_and_exit_ms"] = round((finished - milestones["last_output"]) * 1000, 3)
    result.timings = timings
    # Verbose OpenSSH lines include local identity paths. Keep ordinary errors
    # but return only the interpreted timing milestones from the debug trace.
    result.stderr = "\n".join(line for line in result.stderr.splitlines()
                              if not line.startswith(("debug1:", "debug2:", "debug3:", "Authenticated to ")))
    return result

@pinned_endpoint
def run_bytes(
    endpoint: Endpoint,
    remote_command: str,
    *,
    stdin: bytes | None = None,
    timeout_ms: int | None = None,
) -> RemoteBytesCompleted:
    timeout = None if timeout_ms is None else timeout_ms / 1000
    return _capture_command(
        ssh_command(endpoint, f"bash -c {shlex.quote(remote_command)}"),
        input=stdin,
        timeout=timeout,
    )

_RPC_SCRIPT = r"""
import json, subprocess, sys
payload = json.load(sys.stdin)
result = subprocess.run(["bash", "-c", payload["script"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=payload.get("timeout_s", 300))
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
"""

@pinned_endpoint
def run_rpc_script(endpoint: Endpoint, script: str, *, timeout_ms: int | None = None,
                   mutation: bool = False) -> RemoteCompleted:
    """Bounded package scripts over RPC, including scoped cancellation.

    User commands use shell_ops' durable supervisor. This primitive is for
    package-owned scripts such as a validated git-apply transaction.
    """
    from .rpc_transport import request
    row = request(endpoint, "python", _RPC_SCRIPT, {"script": script, "_mutation": mutation}, timeout_ms=timeout_ms)
    return RemoteCompleted(row["returncode"], row["stdout"], row["stderr"],
                           timed_out=bool(row.get("timed_out")), cancelled=bool(row.get("cancelled")))

@pinned_endpoint
def run_remote_python(
    endpoint: Endpoint,
    code: str,
    payload: dict[str, Any],
    *,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    from .rpc_transport import request
    try:
        row = request(endpoint, "python", code, payload,
                      timeout_ms=timeout_ms)
    except RemoteExecutionError as exc:
        from .errors import error_details
        details = error_details(exc)
        return {"status": "failed", "error": str(exc), "error_details": details,
                "remote_outcome": "not_submitted" if details.get("submission_state") == "not_sent" else "unknown"}
    if row.get("timed_out") or row.get("cancelled"):
        return {"status": "timeout" if row.get("timed_out") else "cancelled",
                "error_details": {"category": "command_timeout" if row.get("timed_out") else "command_cancelled",
                                  "submission_state": "uncertain" if payload.get("_mutation") else "acknowledged", "retryable": False,
                                  "exit_code": row["returncode"], "stderr_tail": row["stderr"][-4000:]},
                "remote_outcome": "unknown" if payload.get("_mutation") else "failed",
                "error": "remote python timed out" if row.get("timed_out") else "remote python cancelled",
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    if row["returncode"] != 0:
        return {"status": "failed", "error": "remote python failed", "exit_code": row["returncode"],
                "error_details": {"category": "command_exit", "submission_state": "uncertain" if payload.get("_mutation") else "acknowledged", "retryable": False,
                                  "exit_code": row["returncode"], "stderr_tail": row["stderr"][-4000:]},
                "remote_outcome": "unknown" if payload.get("_mutation") else "failed",
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    try:
        data = json.loads(row["stdout"].strip())
    except json.JSONDecodeError as exc:
        confirmed_failure("remote.python", stage="protocol_decode", category="command_protocol", exception=exc)
        return {"status": "failed", "error": f"remote python returned non-JSON: {exc}",
                "error_details": {"category": "command_protocol", "submission_state": "uncertain" if payload.get("_mutation") else "acknowledged", "retryable": False,
                                  "exit_code": row["returncode"], "stderr_tail": row["stderr"][-4000:]},
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    if not isinstance(data, dict):
        confirmed_failure("remote.python", stage="protocol_decode", category="command_protocol")
        return {"status": "failed", "error": "remote python JSON was not an object",
                "error_details": {"category": "command_protocol", "submission_state": "uncertain" if payload.get("_mutation") else "acknowledged", "retryable": False,
                                  "exit_code": row["returncode"], "stderr_tail": row["stderr"][-4000:]}}
    return data

def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# Compatibility re-exports: every symbol defined in the split modules remains
# reachable through `remote_dev.core.ssh_transport`, including private helpers
# that tests monkeypatch on this module.
from .ssh_stream import (
    _STREAM_READ_BYTES,
    _AttachedPipes,
    _StreamChannel,
    _close_pipe,
    _emit_stream_text,
    _flush_channel_lines,
    _pipe_select_supported,
    _queue_get,
    _read_attached,
    _read_fd,
    run_stream,
)
from .ssh_forward import (
    LocalForward,
    _as_long_stream,
    _find_free_local_port,
    _rewrite_forward_exit,
    _validate_forward_host,
    _validate_port,
    interactive_ssh_command,
    local_forward_ssh_command,
    open_local_forward,
    run_interactive,
)

from . import ssh_stream as _ssh_stream

_ssh_stream._register_reexports()
