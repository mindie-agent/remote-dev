from __future__ import annotations

import codecs
import hashlib
import json
import os
import queue
import select
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .endpoint import Endpoint
from .container_endpoint import pin_container_endpoint, pinned_endpoint
from .errors import RemoteExecutionError
from .local_process import OwnedProcess
from remote_dev.observability import confirmed_failure, observed_operation

# ControlMaster socket directory. Consumers that already keep an OpenSSH mux
# directory for their own tooling can point remote-dev at it so both share
# one master connection per endpoint.
_MUX_DIR = Path(os.environ.get("REMOTE_DEV_SSH_MUX_DIR") or (Path.home() / ".ssh" / "remote-dev-mux")).expanduser()

# Decide mux-dir readiness once per process. None = undecided, True/False =
# usable / not usable.
_MUX_READY: bool | None = None

# Process-scoped multiplexing *default*. Unset or exact "1" keeps the shared
# ControlMaster; exact "0" forces independent connections. An endpoint's
# ``ssh_mux`` field overrides this for that endpoint only. Read on each
# invocation; never written back to os.environ or cached as a module global.
SSH_MUX_ENV = "REMOTE_DEV_SSH_MUX"

# Remote-side ``timeout(1)`` fires this many seconds before the local reader
# deadline so the remote process can exit with a useful status first.
REMOTE_TIMEOUT_GRACE_SECONDS = 5

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

# Win32-OpenSSH documents Client ControlMaster as out of project scope.
# https://github.com/PowerShell/Win32-OpenSSH/wiki/Project-Scope
# https://github.com/PowerShell/Win32-OpenSSH/issues/1328
WINDOWS_MUX_UNSUPPORTED = (
    "OpenSSH ControlMaster is not supported on native Windows "
    "(Win32-OpenSSH Client ControlMaster is out of project scope). "
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


def _control_master_options(identity_file: str | None = None) -> list[str]:
    """OpenSSH connection reuse through a ControlMaster socket directory.

    Prepared once per process. On failure we emit a single visible warning
    instead of silently disabling reuse (which reads as "the remote is slow").
    """
    global _MUX_READY
    if _MUX_READY is None:
        try:
            _MUX_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(_MUX_DIR, 0o700)
            _MUX_READY = True
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
    """Read REMOTE_DEV_SSH_MUX without mutating os.environ or module globals.

    This is the process-wide default only. Per-endpoint selection lives on
    ``Endpoint.ssh_mux`` and is applied by ``_uses_shared_mux``.
    """
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
    """True when this process is a native Win32 SSH client, not POSIX OpenSSH."""
    return os.name == "nt"


def _uses_shared_mux(endpoint: Endpoint) -> bool:
    """Choose ControlMaster reuse for one endpoint, before argv is built.

    An explicit ``endpoint.ssh_mux`` wins. Otherwise the process-wide
    ``REMOTE_DEV_SSH_MUX`` default applies (unset or ``1`` shared, ``0``
    independent). The choice cannot be appended later: OpenSSH
    first-option-wins semantics make a trailing ``ControlMaster=no``
    override ineffective.

    Native Windows is different: Win32-OpenSSH does not support Client
    ControlMaster. Ordinary Windows connections (``ssh_mux`` omitted,
    ``REMOTE_DEV_SSH_MUX`` unset or ``0``) therefore take the independent
    triple automatically — agents do not need a Windows-only flag.
    ``ssh_mux=True`` or ``REMOTE_DEV_SSH_MUX=1`` is an explicit unsupported
    request and raises rather than emitting ``ControlMaster=auto``.

    Pass ``ssh_mux=False`` — or construct the endpoint with
    :meth:`Endpoint.for_long_stream` — for ``ssh -N -L`` tunnels and
    hour-scale attached streams on POSIX. ControlMaster delegates ``-N``
    forwards to the mux master and the client exits rc=0 immediately,
    tearing the tunnel down. That failure is silent — rc=0 with the tunnel
    gone. The same class of hang appears on hour-scale streams: the mux
    master stays up after the remote side has finished, and the attached
    client never notices. A later ``ControlMaster=no`` cannot fix this.
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
    """ServerAlive probes when the ``keepalive`` mechanism flag is set.

    This flag is orthogonal to mux: it only adds TCP probes. It does not
    mean "this is a long stream". Hour-scale streams and ``ssh -N -L``
    tunnels use :meth:`Endpoint.for_long_stream`, which sets
    ``ssh_mux=False`` and ``keepalive=True`` together.
    :func:`stream_ssh_command` refuses a multiplexed endpoint, because
    ControlMaster delegates ``-N`` forwards to the mux master and the
    client exits rc=0 immediately, tearing the tunnel down. That failure
    is silent; OpenSSH first-option-wins makes a later
    ``ControlMaster=no`` override ineffective.

    Conditional, not always-on. A connection carrying a slow-producing
    multi-hour job otherwise dies to an idle timeout somewhere in the path.
    Short multiplexed commands finish in seconds and do not sit idle;
    attaching ServerAlive to them would set TCP keepalive policy on the
    shared ControlMaster (the master owns the TCP connection, and OpenSSH
    first-option-wins makes the first client's ServerAlive the master's).
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
    """Compose an SSH argv. ``option_tokens`` are placed before ``--``.

    ``--`` stops OpenSSH option parsing. Anything after the host is a
    remote command, not an option: ``-N`` / ``-L`` / ``-o`` appended past
    the destination are executed on the far side and never take effect.
    This helper is the only place that emits ``--``, so package callers
    cannot reintroduce that split. ``option_tokens`` is not a public
    extra-options escape hatch; only this module passes tokens it owns.
    """
    if _uses_shared_mux(endpoint):
        mux_options = _control_master_options(endpoint.identity_file)
    else:
        mux_options = _independent_ssh_connection_options()
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={max(1, int(endpoint.connect_timeout_ms / 1000))}",
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

    Tokens use OpenSSH's quoting convention for one command argv. For shell
    syntax supply ``bash -c`` and a quoted script. Container argv is parsed and
    re-quoted so operators or expansions can never escape into the host shell.
    Container execution preserves Docker's configured user and environment.
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
    """Wrap ``script`` in remote-side ``timeout --preserve-status`` when asked.

    The grace margin makes the remote killer fire first so the remote can
    report something useful before the local reader gives up.
    ``--preserve-status`` keeps a successful command's real exit code.
    """
    if timeout_ms is None or timeout_ms <= 0:
        return script
    timeout_s = timeout_ms / 1000
    margin = max(int(timeout_s) - REMOTE_TIMEOUT_GRACE_SECONDS, 1)
    return f"timeout --preserve-status {margin}s bash -lc {shlex.quote(script)}"


def _require_independent_stream(endpoint: Endpoint) -> None:
    """Refuse a multiplexed connection for an attached stream.

    The failure this guards is silent: ControlMaster delegates ``-N``
    forwards and hour-scale sessions to the mux master, the client exits
    rc=0, and the tunnel or stream is gone. OpenSSH first-option-wins
    makes a later ``ControlMaster=no`` override ineffective, so the
    independent triple has to be chosen before argv is built.
    ``ControlMaster=no`` alone is not enough — a client can still attach
    to an existing ``ControlPath``. A docstring is not a control for a
    failure that announces success.
    """
    if _uses_shared_mux(endpoint):
        raise RemoteExecutionError(STREAM_MUX_REFUSAL)


@pinned_endpoint
def stream_ssh_command(endpoint: Endpoint, script: str | None, *, timeout_ms: int | None = None) -> list[str]:
    """Argv for an attached live-stream SSH invocation.

    Refuses a multiplexed endpoint. Use :meth:`Endpoint.for_long_stream`
    or pass ``ssh_mux=False``. ``script=None`` reads the script from binary
    stdin, avoiding client and remote command-line length limits.
    """
    _require_independent_stream(endpoint)
    if script is None:
        remote = "bash -s"
        if timeout_ms is not None and timeout_ms > 0:
            margin = max(int(timeout_ms / 1000) - REMOTE_TIMEOUT_GRACE_SECONDS, 1)
            remote = f"timeout --preserve-status {margin}s bash -ls"
        return ssh_command(endpoint, remote)
    return ssh_command(endpoint, "bash", "-c", shlex.quote(stream_remote_payload(script, timeout_ms)))


@pinned_endpoint
@observed_operation("ssh.script", level="DEBUG")
def run_script(endpoint: Endpoint, script: str, *, timeout_ms: int | None = None,
               trace_connection: bool = False) -> RemoteCompleted:
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
    try:
        proc = subprocess.run(
            command,
            # A text-mode stdin rewrites LF to CRLF on Windows, corrupting
            # shell options, heredocs and Python payloads on the Linux peer.
            input=payload,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        code, stdout, stderr, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr, timed_out = None, exc.stdout, exc.stderr, True
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
    return RemoteCompleted(code, stdout, stderr, timed_out=timed_out, timings=timings)


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
def run_stream(
    endpoint: Endpoint,
    script: str,
    *,
    timeout_ms: int | None = None,
    forward_prefix: str = "[remote] ",
    output: TextIO | None = None,
    on_output: Callable[[str, str], None] | None = None,
    merge_stderr: bool = True,
) -> RemoteCompleted:
    """Run a remote command, forwarding stdout/stderr live as they arrive.

    This is a transport primitive. It returns :class:`RemoteCompleted` with
    the remote exit code (or ``timed_out=True``). By default, live bytes are
    written to ``output`` (default ``stderr``) and are not stuffed into
    ``RemoteCompleted.stdout``. It does **not** emit ``remote-dev.result.v1``;
    that contract is the result of one finished tool call. A later tool
    wrapper that calls this should wrap the completed invocation in
    ``make_result``.

    ``on_output(channel, text)`` is a generic line callback. ``channel`` is
    ``"stdout"`` or ``"stderr"``. This module does not parse application
    sentinels.

    Default ``merge_stderr=True`` keeps the historical merged-stream
    behaviour: stdout and stderr are joined, forwarded with
    ``forward_prefix``, and ``RemoteCompleted.stdout`` / ``.stderr`` stay
    empty (except the timeout message). Pass ``merge_stderr=False`` to keep
    the channels separate, capture both, and invoke ``on_output`` per line.
    Separate-channel mode does not auto-forward unless ``output`` is set.

    Not ``remote.job_*``. Jobs are detached through
    ``remote_dev.processes.control``, persist a job dir, and ``job_tail``
    snapshots supervisor logs. The supervisor owns remote-side timeout,
    stop, and descendant drain. An attached stream is required when an
    agent must see stage progress as it happens and must tell a hang from
    slow progress. Detach-and-tail is not a substitute for this live
    local wall-clock reader: a stalled pipe is still a hang until the
    attached timeout fires.

    Silent-hang handling: ``timeout_ms`` is enforced two ways at once.

    1. Remote-side kill. The command is wrapped in
       ``timeout --preserve-status <s>s bash -ls`` so an unresponsive
       remote process is killed at the source even when it has stopped
       producing output. A five-second grace margin lets the remote timeout
       fire first. ``--preserve-status`` keeps a successful command's real
       exit code.
    2. Local-side kill. The local reader waits with a small slice so a
       wall-clock timeout is honoured immediately even when output is
       sitting in a slow pipe buffer. POSIX uses ``select`` on pipes;
       native Windows uses one reader thread per pipe because ``select``
       cannot wait on subprocess pipes there.

    Either alone leaves a hole: remote-only misses a dead network;
    local-only leaves an orphan remote process.

    Callers that stream hour-scale jobs construct the endpoint with
    :meth:`Endpoint.for_long_stream`. :func:`stream_ssh_command` refuses
    a multiplexed endpoint: ControlMaster delegates the session to the
    mux master and the client exits rc=0, which looks like success.
    """
    dest = sys.stderr if output is None and merge_stderr else output
    cmd = stream_ssh_command(endpoint, None, timeout_ms=timeout_ms)
    payload = script.encode("utf-8")
    owner = OwnedProcess(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        bufsize=0,
    )
    proc = owner.process
    # Upload concurrently with output draining: a script larger than pipe
    # capacity must not stall progress or bypass the reader's local deadline.
    # Binary writes preserve LF on native Windows.
    def upload() -> None:
        try:
            view = memoryview(payload)
            while view:
                written = proc.stdin.write(view[:65536])
                if not written:
                    break
                view = view[written:]
        except (BrokenPipeError, OSError):
            pass  # The reader reports SSH exit status and stderr.
        finally:
            _close_pipe(proc.stdin)

    writer = threading.Thread(target=upload, daemon=True, name="remote-dev-stream-upload")
    try:
        writer.start()
        return _read_attached(
            owner,
            timeout_ms=timeout_ms,
            forward_prefix=forward_prefix,
            output=dest,
            on_output=on_output,
            capture=not merge_stderr,
        )
    finally:
        owner.stop()
        if writer.ident is not None:
            writer.join(timeout=1)


_STREAM_READ_BYTES = 4096


def _read_fd(fd: int) -> bytes | None:
    """Read available bytes. ``None`` means EOF; ``b''`` means try again."""
    try:
        chunk = os.read(fd, _STREAM_READ_BYTES)
    except BlockingIOError:
        return b""
    except OSError:
        return None
    return chunk if chunk else None


def _pipe_select_supported() -> bool:
    """``select`` on subprocess pipes is POSIX-only. Windows needs threads."""
    return os.name != "nt"


def _queue_get(
    pending: "queue.Queue[tuple[int, bytes | None]]", timeout: float | None
) -> tuple[int, bytes | None] | None:
    try:
        if timeout is not None and timeout <= 0:
            return pending.get_nowait()
        return pending.get(timeout=timeout)
    except queue.Empty:
        return None


class _AttachedPipes:
    """Deadline-friendly wait for subprocess pipe bytes.

    POSIX: non-blocking fds + ``select``. Native Windows: one blocking
    reader thread per pipe, because ``select`` cannot wait on those pipes.
    """

    def __init__(self, channels: list[_StreamChannel]) -> None:
        self.channels = channels
        self.use_select = _pipe_select_supported()
        self._queue: queue.Queue[tuple[int, bytes | None]] | None = None
        self._threads: list[threading.Thread] = []
        if self.use_select:
            for channel in channels:
                os.set_blocking(channel.fd, False)
            return
        self._queue = queue.Queue()
        for channel in channels:
            thread = threading.Thread(
                target=self._reader,
                args=(channel,),
                name=f"remote-dev-pipe-{channel.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _reader(self, channel: _StreamChannel) -> None:
        assert self._queue is not None
        while True:
            chunk = _read_fd(channel.fd)
            if chunk is None:
                self._queue.put((channel.fd, None))
                return
            if chunk:
                self._queue.put((channel.fd, chunk))

    def wait(self, open_fds: set[int], timeout: float | None) -> list[tuple[int, bytes | None]]:
        if not open_fds:
            return []
        if self.use_select:
            ready, _, _ = select.select(list(open_fds), [], [], timeout)
            return [(fd, _read_fd(fd)) for fd in ready]
        assert self._queue is not None
        first = _queue_get(self._queue, timeout)
        if first is None:
            return []
        items = [first]
        while True:
            nxt = _queue_get(self._queue, 0)
            if nxt is None:
                break
            items.append(nxt)
        return [(fd, chunk) for fd, chunk in items if fd in open_fds]

    def drain(self, open_fds: set[int]) -> list[tuple[int, bytes | None]]:
        """Read remaining bytes after the child has exited."""
        if self.use_select:
            items: list[tuple[int, bytes | None]] = []
            for fd in list(open_fds):
                while True:
                    chunk = _read_fd(fd)
                    if not chunk:
                        items.append((fd, None if chunk is None else b""))
                        break
                    items.append((fd, chunk))
            return items
        leftover: list[tuple[int, bytes | None]] = []
        while True:
            nxt = _queue_get(self._queue, 0) if self._queue is not None else None
            if nxt is None:
                break
            if nxt[0] in open_fds:
                leftover.append(nxt)
        seen = {fd for fd, _chunk in leftover}
        for fd in open_fds:
            if fd not in seen:
                leftover.append((fd, None))
        return leftover

    def finish_readers(self) -> None:
        """After owned descendants stop, let Windows enqueue the final bytes."""
        deadline = time.monotonic() + 1.0
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


@dataclass
class _StreamChannel:
    name: str
    fd: int
    decoder: Any = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    pending: str = ""
    captured: list[str] = field(default_factory=list)


def _emit_stream_text(
    text: str,
    *,
    channel: str,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    captured: list[str] | None,
) -> None:
    if not text:
        return
    if on_output is not None:
        on_output(channel, text)
    if captured is not None:
        captured.append(text)
    if output is None:
        return
    output.write(forward_prefix + text if forward_prefix and not text.startswith(forward_prefix) else text)
    output.flush()


def _flush_channel_lines(
    channel: _StreamChannel,
    *,
    text: str,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    capture: bool,
    final: bool,
) -> None:
    channel.pending += text
    while True:
        newline = channel.pending.find("\n")
        if newline < 0:
            break
        line = channel.pending[: newline + 1]
        channel.pending = channel.pending[newline + 1 :]
        _emit_stream_text(
            line,
            channel=channel.name,
            forward_prefix=forward_prefix,
            output=output,
            on_output=on_output,
            captured=channel.captured if capture else None,
        )
    if final and channel.pending:
        _emit_stream_text(
            channel.pending,
            channel=channel.name,
            forward_prefix=forward_prefix,
            output=output,
            on_output=on_output,
            captured=channel.captured if capture else None,
        )
        channel.pending = ""


def _close_pipe(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _read_attached(
    owner: OwnedProcess,
    *,
    timeout_ms: int | None,
    forward_prefix: str,
    output: TextIO | None,
    on_output: Callable[[str, str], None] | None,
    capture: bool,
) -> RemoteCompleted:
    """Deadline-aware reader for merged or separate SSH streams.

    Uses ``select`` + ``os.read`` on POSIX, or reader threads on native
    Windows, so a partial line cannot block past the wall-clock deadline.
    ``capture=False`` is the historical merged mode (forward live, leave
    ``RemoteCompleted.stdout`` empty).
    """
    proc = owner.process
    assert proc.stdout is not None
    channels = [_StreamChannel(name="stdout", fd=proc.stdout.fileno())]
    if proc.stderr is not None and proc.stderr is not proc.stdout:
        channels.append(_StreamChannel(name="stderr", fd=proc.stderr.fileno()))
    by_fd = {channel.fd: channel for channel in channels}
    open_fds = set(by_fd)
    pipes = _AttachedPipes(channels)
    started = time.monotonic()
    deadline = None if timeout_ms is None or timeout_ms <= 0 else started + (timeout_ms / 1000)
    timeout_message = (
        f"remote command exceeded {timeout_ms} ms wall-clock limit" if timeout_ms is not None else ""
    )

    def captured_stdout() -> str:
        stdout = next(channel for channel in channels if channel.name == "stdout")
        return "".join(stdout.captured)

    def captured_stderr() -> str:
        stderr = next((channel for channel in channels if channel.name == "stderr"), None)
        return "" if stderr is None else "".join(stderr.captured)

    def finalize_open_channels() -> None:
        for channel in channels:
            leftover = ""
            try:
                leftover = channel.decoder.decode(b"", final=True)
            except Exception:
                leftover = ""
            _flush_channel_lines(
                channel,
                text=leftover,
                forward_prefix=forward_prefix,
                output=output,
                on_output=on_output,
                capture=capture,
                final=True,
            )

    def timed_out_result() -> RemoteCompleted:
        # Killing the owner closes inherited handles too. Drain bytes already
        # produced before finalizing UTF-8 decoders, even after the deadline.
        pipes.finish_readers()
        for fd, chunk in pipes.drain(open_fds):
            if chunk:
                channel = by_fd[fd]
                _flush_channel_lines(channel, text=channel.decoder.decode(chunk),
                                     forward_prefix=forward_prefix, output=output,
                                     on_output=on_output, capture=capture, final=False)
        finalize_open_channels()
        stderr = timeout_message
        if capture:
            extra = captured_stderr()
            if extra:
                if not extra.endswith("\n") and timeout_message:
                    extra = extra + "\n"
                stderr = extra + timeout_message
        return RemoteCompleted(
            None,
            captured_stdout() if capture else "",
            stderr,
            timed_out=True,
        )

    def close_channel(channel: _StreamChannel) -> None:
        stream = proc.stdout if channel.name == "stdout" else proc.stderr
        _close_pipe(stream)

    def close_pipes() -> None:
        seen: set[int] = set()
        for stream in (proc.stdout, proc.stderr):
            if stream is None or id(stream) in seen:
                continue
            seen.add(id(stream))
            _close_pipe(stream)

    def remaining_wait() -> float | None:
        if deadline is None:
            return STREAM_SELECT_SLICE_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(remaining, STREAM_SELECT_SLICE_SECONDS)

    try:
        while True:
            wait = remaining_wait()
            if wait is not None and wait <= 0:
                owner.stop()
                return timed_out_result()
            if not open_fds:
                # Pipes have closed. Wait until the real deadline (or forever
                # if none). A select-slice TimeoutExpired must not kill here:
                # grandchildren can close stdout/stderr while the child still
                # has work left.
                if proc.poll() is not None:
                    returncode = proc.wait()
                    return RemoteCompleted(
                        returncode,
                        captured_stdout() if capture else "",
                        captured_stderr() if capture else "",
                    )
                if deadline is None:
                    returncode = proc.wait()
                    return RemoteCompleted(
                        returncode,
                        captured_stdout() if capture else "",
                        captured_stderr() if capture else "",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    owner.stop()
                    return timed_out_result()
                try:
                    returncode = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    owner.stop()
                    return timed_out_result()
                return RemoteCompleted(
                    returncode,
                    captured_stdout() if capture else "",
                    captured_stderr() if capture else "",
                )
            ready = pipes.wait(open_fds, wait)
            if ready:
                for fd, chunk in ready:
                    channel = by_fd[fd]
                    if chunk is None:
                        _flush_channel_lines(
                            channel,
                            text=channel.decoder.decode(b"", final=True),
                            forward_prefix=forward_prefix,
                            output=output,
                            on_output=on_output,
                            capture=capture,
                            final=True,
                        )
                        open_fds.discard(fd)
                        close_channel(channel)
                        continue
                    if not chunk:
                        continue
                    _flush_channel_lines(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                if proc.poll() is not None:
                    owner.stop()
                continue
            if proc.poll() is None:
                continue
            # Parent exit does not close descriptors inherited by a proxy child.
            # Stop the owned local group before draining or closing its pipes.
            owner.stop()
            pipes.finish_readers()
            drained = pipes.drain(open_fds)
            for fd, chunk in drained:
                channel = by_fd[fd]
                if chunk:
                    _flush_channel_lines(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                if not chunk:
                    _flush_channel_lines(
                        channel,
                        text=channel.decoder.decode(b"", final=True),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=True,
                    )
                    open_fds.discard(fd)
                    close_channel(channel)
            for fd in list(open_fds):
                channel = by_fd[fd]
                _flush_channel_lines(
                    channel,
                    text=channel.decoder.decode(b"", final=True),
                    forward_prefix=forward_prefix,
                    output=output,
                    on_output=on_output,
                    capture=capture,
                    final=True,
                )
                open_fds.discard(fd)
                close_channel(channel)
            returncode = proc.wait()
            return RemoteCompleted(
                returncode,
                captured_stdout() if capture else "",
                captured_stderr() if capture else "",
            )
    finally:
        owner.stop()
        pipes.finish_readers()
        close_pipes()


@pinned_endpoint
def run_bytes(
    endpoint: Endpoint,
    remote_command: str,
    *,
    stdin: bytes | None = None,
    timeout_ms: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    timeout = None if timeout_ms is None else timeout_ms / 1000
    return subprocess.run(
        ssh_command(endpoint, f"bash -c {shlex.quote(remote_command)}"),
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


_RPC_SCRIPT = r"""
import json, subprocess, sys
payload = json.load(sys.stdin)
result = subprocess.run(["bash", "-c", payload["script"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                                  "submission_state": "acknowledged", "retryable": False},
                "error": "remote python timed out" if row.get("timed_out") else "remote python cancelled",
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    if row["returncode"] != 0:
        return {"status": "failed", "error": "remote python failed", "exit_code": row["returncode"],
                "error_details": {"category": "command_exit", "submission_state": "acknowledged", "retryable": False},
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    try:
        data = json.loads(row["stdout"].strip())
    except json.JSONDecodeError as exc:
        confirmed_failure("remote.python", stage="protocol_decode", category="command_protocol", exception=exc)
        return {"status": "failed", "error": f"remote python returned non-JSON: {exc}",
                "error_details": {"category": "command_protocol", "submission_state": "acknowledged", "retryable": False},
                "stdout_tail": row["stdout"][-4000:], "stderr_tail": row["stderr"][-4000:]}
    if not isinstance(data, dict):
        confirmed_failure("remote.python", stage="protocol_decode", category="command_protocol")
        return {"status": "failed", "error": "remote python JSON was not an object",
                "error_details": {"category": "command_protocol", "submission_state": "acknowledged", "retryable": False}}
    return data


def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# ---------------------------------------------------------------------------
# Local port forward (ssh -N -L) and interactive bootstrap
# ---------------------------------------------------------------------------


def _rewrite_forward_exit(returncode: int | None) -> int:
    """A dead ``-N`` forward is never success. Mux-absorbed clients exit 0."""
    if returncode is None or returncode == 0:
        return FORWARD_DEAD_EXIT_CODE
    return int(returncode)


def _validate_forward_host(value: str, *, field: str) -> str:
    host = str(value)
    if not host or host.startswith("-"):
        raise RemoteExecutionError(f"{field} must be a hostname, not an option: {value!r}")
    if any(char in host for char in ("\n", "\r", "\0", " ")):
        raise RemoteExecutionError(f"{field} contains invalid characters: {value!r}")
    return host


def _validate_port(value: int, *, field: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteExecutionError(f"{field} must be an integer") from exc
    if isinstance(value, bool) or not (1 <= port <= 65535):
        raise RemoteExecutionError(f"{field} must be in 1..65535, got {value!r}")
    return port


def _find_free_local_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _as_long_stream(endpoint: Endpoint) -> Endpoint:
    """Normalize to the for_long_stream shape after refusing a muxed endpoint."""
    _require_independent_stream(endpoint)
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
    if endpoint.container:
        raise RemoteExecutionError("container endpoints do not support SSH port forwarding; use an explicit host endpoint and a reachable published port")
    endpoint = _as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    local_port = _validate_port(local_port, field="local_port")
    remote_port = _validate_port(remote_port, field="remote_port")
    return _ssh_cmd(
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
        return _rewrite_forward_exit(rc)

    def wait_ready(self, timeout_s: float = 15.0) -> None:
        """Block until ``local_port`` accepts connections.

        Raises :class:`RemoteExecutionError` if the process dies or the
        timeout expires. A process that exits 0 is reported as
        :data:`FORWARD_DEAD_EXIT_CODE`.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        last_error = ""
        family = socket.AF_INET6 if ":" in self.local_host else socket.AF_INET
        while time.monotonic() < deadline:
            rc = self._proc.poll()
            if rc is not None:
                self._owner.stop()
                stderr = self._consume_stderr()
                rewritten = _rewrite_forward_exit(rc)
                detail = f"rc={rewritten}"
                if rc != rewritten:
                    detail += f", ssh rc={rc}"
                raise RemoteExecutionError(
                    f"ssh local forward exited early ({detail}): {stderr[:2000]}"
                )
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.5)
                    sock.connect((self.local_host, self.local_port))
                return
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.2)
        raise RemoteExecutionError(
            f"timed out waiting for ssh local forward on {self.local_host}:{self.local_port} ({last_error})"
        )

    def close(self) -> RemoteCompleted:
        """Kill the forward process group. Never returns rc=0."""
        if self._closed:
            rc = self._proc.poll()
            return RemoteCompleted(_rewrite_forward_exit(rc), "", self._stderr)
        rc = _rewrite_forward_exit(self._owner.stop(force=False))
        stderr = self._consume_stderr()
        self._closed = True
        return RemoteCompleted(rc, "", stderr)

    def __enter__(self) -> LocalForward:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def open_local_forward(
    endpoint: Endpoint,
    remote_port: int,
    *,
    remote_host: str = "127.0.0.1",
    local_host: str = "127.0.0.1",
    local_port: int | None = None,
    ready_timeout_s: float | None = 15.0,
) -> LocalForward:
    """Open a local→remote forward and return a :class:`LocalForward` handle.

    Built on :meth:`Endpoint.for_long_stream`: independent connection,
    keepalives, ``ExitOnForwardFailure=yes``, ``-N``. Refuses a multiplexed
    endpoint the same way :func:`run_stream` does. When ``local_port`` is
    omitted an ephemeral loopback port is chosen. When ``ready_timeout_s``
    is not ``None``, the local port must accept connections before this
    returns.
    """
    if endpoint.container:
        raise RemoteExecutionError("container endpoints do not support SSH port forwarding; use an explicit host endpoint and a reachable published port")
    endpoint = _as_long_stream(endpoint)
    local_host = _validate_forward_host(local_host, field="local_host")
    remote_host = _validate_forward_host(remote_host, field="remote_host")
    remote_port = _validate_port(remote_port, field="remote_port")
    if local_port is None:
        chosen_port = _find_free_local_port(local_host)
    else:
        chosen_port = _validate_port(local_port, field="local_port")
    cmd = local_forward_ssh_command(
        endpoint,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    try:
        owner = OwnedProcess(cmd, **popen_kwargs)
    except FileNotFoundError as exc:
        raise RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    handle = LocalForward(
        owner=owner,
        local_host=local_host,
        local_port=chosen_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    if ready_timeout_s is not None:
        try:
            handle.wait_ready(ready_timeout_s)
        except Exception:
            handle.close()
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
        raise RemoteExecutionError("interactive bootstrap does not support container endpoints; use remote_bash with tty=True for a container PTY")
    if _uses_shared_mux(endpoint):
        raise RemoteExecutionError(INTERACTIVE_MUX_REFUSAL)
    timeout_s = max(1, int(endpoint.connect_timeout_ms / 1000))
    cmd = [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        f"ConnectTimeout={timeout_s}",
        *_independent_ssh_connection_options(),
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
    cmd = interactive_ssh_command(endpoint, argv)
    full_env = None if env is None else {**os.environ, **dict(env)}
    try:
        proc = subprocess.run(cmd, env=full_env)
    except FileNotFoundError as exc:
        raise RemoteExecutionError(f"required local command not found: {cmd[0]}") from exc
    return int(proc.returncode)
