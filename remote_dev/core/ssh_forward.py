"""
[Role] Local SSH port forwarding (`ssh -N -L`) and interactive bootstrap (`ssh_forward.py`).
[Invariants]
- Forward and interactive argv always choose independent connections (`ControlMaster=no`, `ControlPath=none`, `ControlPersist=no`); a multiplexed endpoint is refused.
- A dead forward is never reported as success: rc=0 or a vanished client maps to `FORWARD_DEAD_EXIT_CODE = 255`.
- Validation rejects option-shaped hosts and ports outside `1..65535`; no caller-supplied `-o` tokens are accepted.
[Timeouts & Locks]
- `wait_ready` polls until its explicit deadline; `run_interactive` bounds `subprocess.run` with `timeout=300.0` by default. No locks are held.
[Side-Effect Policy]
- Spawns one local SSH process group for a forward and kills it on close; interactive mode inherits the local TTY and starts no remote state.
"""
from __future__ import annotations

import os
import socket
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .endpoint import Endpoint
from .errors import RemoteExecutionError, record_cleanup_failure
from .local_process import OwnedProcess


def _transport():
    from . import ssh_transport
    return ssh_transport


def _rewrite_forward_exit(returncode: int | None) -> int:
    """A dead ``-N`` forward is never success. Mux-absorbed clients exit 0."""
    if returncode is None or returncode == 0:
        return _transport().FORWARD_DEAD_EXIT_CODE
    return int(returncode)


def _validate_forward_host(value: str, *, field: str) -> str:
    host = str(value)
    if not host or host.startswith("-"):
        raise _transport().RemoteExecutionError(f"{field} must be a hostname, not an option: {value!r}")
    if any(char in host for char in ("\n", "\r", "\0", " ")):
        raise _transport().RemoteExecutionError(f"{field} contains invalid characters: {value!r}")
    return host


def _validate_port(value: int, *, field: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise _transport().RemoteExecutionError(f"{field} must be an integer") from exc
    if isinstance(value, bool) or not (1 <= port <= 65535):
        raise _transport().RemoteExecutionError(f"{field} must be in 1..65535, got {value!r}")
    return port


def _find_free_local_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _as_long_stream(endpoint: Endpoint) -> Endpoint:
    """Normalize to the for_long_stream shape after refusing a muxed endpoint."""
    _transport()._require_independent_stream(endpoint)
    if endpoint.keepalive:
        return endpoint
    return Endpoint.for_long_stream(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        root=endpoint.root,
        cwd=endpoint.cwd,
        runtime_env=endpoint.runtime_env,
        runtime_env_file=endpoint.runtime_env_file,
        identity_file=endpoint.identity_file,
        connect_timeout_ms=endpoint.connect_timeout_ms,
        kind=endpoint.kind,
        alias=endpoint.alias,
        source=endpoint.source,
        container=endpoint.container,
        container_selector=endpoint.container_selector,
    )


def local_forward_ssh_command(
    endpoint: Endpoint,
    *,
    local_host: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
) -> list[str]:
    """Argv for ``ssh -N -L`` on the ``for_long_stream`` shape.

    Refuses a multiplexed endpoint. Always includes ``ControlMaster=no``,
    keepalives, ``ExitOnForwardFailure=yes``, and ``-N``. Callers cannot
    inject extra ``-o`` strings.
    """
    transport = _transport()
    if endpoint.container:
        raise transport.RemoteExecutionError("container endpoints do not support SSH port forwarding; use an explicit host endpoint and a reachable published port")
    endpoint = transport._as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    local_port = _validate_port(local_port, field="local_port")
    remote_port = _validate_port(remote_port, field="remote_port")
    return _transport()._ssh_cmd(
        endpoint,
        (
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-L",
            f"{local_host}:{local_port}:{remote_host}:{remote_port}",
        ),
    )


class LocalForward:
    """Handle for one local→remote SSH port forward.

    ``local_port`` is the bound loopback port. ``wait_ready`` blocks until
    that port accepts connections. ``close`` kills the process group so no
    child is left behind. ``poll`` and ``close`` never report rc=0 for a
    dead forward.
    """

    def __init__(
        self,
        *,
        owner: OwnedProcess,
        local_host: str,
        local_port: int,
        remote_host: str,
        remote_port: int,
    ) -> None:
        self.local_host = local_host
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._owner = owner
        self._proc = owner.process
        self._stderr = ""
        self._stderr_read = False
        self._closed = False

    def _consume_stderr(self) -> str:
        if self._stderr_read:
            return self._stderr
        self._stderr_read = True
        if self._proc.stderr is None:
            self._stderr = ""
            return self._stderr
        data = self._proc.stderr.read() or ""
        self._stderr = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        return self._stderr

    def poll(self) -> int | None:
        rc = self._proc.poll()
        if rc is None:
            return None
        return _transport()._rewrite_forward_exit(rc)

    def wait_ready(self, timeout_s: float | None = None) -> None:
        """Block until ``local_port`` accepts connections.

        Raises :class:`RemoteExecutionError` if the process dies or the
        timeout expires. A process that exits 0 is reported as
        :data:`FORWARD_DEAD_EXIT_CODE`.
        """
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))
        last_error = ""
        family = socket.AF_INET6 if ":" in self.local_host else socket.AF_INET
        while deadline is None or time.monotonic() < deadline:
            cancelled = _transport().current_event()
            if cancelled is not None and cancelled.is_set():
                raise _transport().RemoteExecutionError("ssh local forward cancelled")
            rc = self._proc.poll()
            if rc is not None:
                rewritten = _transport()._rewrite_forward_exit(rc)
                detail = f"rc={rewritten}"
                if rc != rewritten:
                    detail += f", ssh rc={rc}"
                failure = _transport().RemoteExecutionError(f"ssh local forward exited early ({detail})")
                failure.exit_code = rc
                try:
                    self._owner.stop()
                    stderr = self._consume_stderr()
                    failure.args = (str(failure) + ": " + stderr[:2000],)
                    failure.stderr_tail = stderr[-4000:]
                except Exception as cleanup_error:
                    _transport().record_cleanup_failure(failure, cleanup_error)
                raise failure
            try:
                probe_timeout = 0.5 if deadline is None else min(0.5, max(0.05, deadline - time.monotonic()))
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.settimeout(probe_timeout)
                    sock.connect((self.local_host, self.local_port))
                return
            except (TimeoutError, OSError) as exc:
                last_error = str(exc)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(0.05 if deadline is not None else 0.2)
        raise _transport().RemoteExecutionError(
            f"timed out waiting for ssh local forward on {self.local_host}:{self.local_port} ({last_error})"
        )

    def close(self) -> RemoteCompleted:
        """Kill the forward process group. Never returns rc=0."""
        if self._closed:
            rc = self._proc.poll()
            return _transport().RemoteCompleted(_transport()._rewrite_forward_exit(rc), "", self._stderr)
        rc = _transport()._rewrite_forward_exit(self._owner.stop(force=False))
        stderr = self._consume_stderr()
        self._closed = True
        return _transport().RemoteCompleted(rc, "", stderr)

    def __enter__(self) -> LocalForward:
        return self

    def __exit__(self, _type, error, _traceback) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if error is None:
                raise
            record_cleanup_failure(error, cleanup_error)


def open_local_forward(
    endpoint: Endpoint,
    remote_port: int,
    *,
    remote_host: str = "127.0.0.1",
    local_host: str = "127.0.0.1",
    local_port: int | None = None,
    ready_timeout_s: float | None = None,
) -> LocalForward:
    """Open a local→remote forward and return a :class:`LocalForward` handle.

    Built on :meth:`Endpoint.for_long_stream`: independent connection,
    keepalives, ``ExitOnForwardFailure=yes``, ``-N``. Refuses a multiplexed
    endpoint the same way :func:`run_stream` does. When ``local_port`` is
    omitted an ephemeral loopback port is chosen. The local port must accept connections before this returns. There is
    no readiness deadline unless ``ready_timeout_s`` is explicitly supplied.
    """
    transport = _transport()
    if endpoint.container:
        raise transport.RemoteExecutionError("container endpoints do not support SSH port forwarding; use an explicit host endpoint and a reachable published port")
    endpoint = transport._as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    remote_port = _validate_port(remote_port, field="remote_port")
    if local_port is None:
        chosen_port = _find_free_local_port(local_host)
    else:
        chosen_port = _validate_port(local_port, field="local_port")
    cmd = transport.local_forward_ssh_command(
        endpoint,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    popen_kwargs: dict[str, Any] = {
        "stdout": transport.subprocess.DEVNULL,
        "stderr": transport.subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    try:
        owner = transport.OwnedProcess(cmd, **popen_kwargs)
    except FileNotFoundError as exc:
        raise transport.RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    handle = transport.LocalForward(
        owner=owner,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    try:
        handle.wait_ready(ready_timeout_s)
    except Exception as error:
        try:
            handle.close()
        except Exception as cleanup_error:
            transport.record_cleanup_failure(error, cleanup_error)
        raise
    return handle


def interactive_ssh_command(
    endpoint: Endpoint,
    remote_command: Sequence[str] = (),
) -> list[str]:
    """Argv for a one-off interactive bootstrap SSH.

    ``BatchMode=no``, password/keyboard-interactive only, never multiplexed.
    Refuses a multiplexed endpoint. Does not accept extra ``-o`` strings.
    This is first-contact bootstrap, not a general PTY facility.
    """
    if endpoint.container:
        raise _transport().RemoteExecutionError("interactive bootstrap does not support container endpoints; use remote_bash with tty=True for a container PTY")
    transport = _transport()
    if transport._uses_shared_mux(endpoint):
        raise transport.RemoteExecutionError(transport.INTERACTIVE_MUX_REFUSAL)
    cmd = [
        transport._ssh_executable(),
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        *([] if endpoint.connect_timeout_ms is None else
          ["-o", f"ConnectTimeout={max(1, int(endpoint.connect_timeout_ms / 1000))}"]),
        *transport._independent_ssh_connection_options(),
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-l",
        endpoint.user,
        "-p",
        str(endpoint.port),
        "--",
        endpoint.host,
    ]
    cmd.extend(str(item) for item in remote_command)
    return cmd


def run_interactive(
    endpoint: Endpoint,
    remote_command: Sequence[str] | str = (),
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 300.0,
) -> int:
    """Run a one-off interactive SSH, inheriting the local TTY.

    Returns the ``ssh`` returncode. ``env`` is merged into the process
    environment for wrappers such as ``SSH_ASKPASS``; it cannot inject
    SSH ``-o`` options. Refuses a multiplexed endpoint.
    """
    argv: Sequence[str]
    if isinstance(remote_command, str):
        argv = (remote_command,)
    else:
        argv = tuple(str(item) for item in remote_command)
    transport = _transport()
    cmd = transport.interactive_ssh_command(endpoint, argv)
    full_env = None if env is None else {**os.environ, **dict(env)}
    try:
        proc = transport.subprocess.run(cmd, env=full_env, timeout=timeout)
    except FileNotFoundError as exc:
        raise transport.RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    return int(proc.returncode)
