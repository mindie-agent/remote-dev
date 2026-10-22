"""
[Role] Attached live-stream execution over SSH (`ssh_stream.py`): dual-sided deadline, merged/separate channels, and byte-accurate forwarding.
[Invariants]
- `run_stream` returns a `RemoteCompleted` with the real exit code, `timed_out=True`, or raises an explicit cancellation/cleanup error; silence is never success.
- POSIX uses non-blocking fds + `select`; native Windows uses one reader thread per pipe because `select` cannot wait on subprocess pipes.
- All monkeypatch-sensitive lookups resolve through `ssh_transport` (or local overrides) so existing `from remote_dev.core import ssh_transport` patches keep working.
[Timeouts & Locks]
- Caller `timeout_ms` is enforced remotely (`timeout --preserve-status`) and locally via `STREAM_SELECT_SLICE_SECONDS = 0.1` plus `OwnedProcess.stop()`.
[Side-Effect Policy]
- Spawns one local SSH process group through `OwnedProcess`, streams bytes to caller `output`/`on_output`, and never writes remote files.
"""
from __future__ import annotations

import codecs
import os
import queue
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from .container_endpoint import pinned_endpoint
from .endpoint import Endpoint


def _transport():
    from . import ssh_transport
    return ssh_transport


_MISSING = object()
_ORIGINALS: dict[str, Any] = {}


def _register_reexports() -> None:
    """Snapshot the re-exported objects so either module can be monkeypatched."""
    transport = _transport()
    for name in (
        "_STREAM_READ_BYTES", "_AttachedPipes", "_StreamChannel", "_close_pipe",
        "_emit_stream_text", "_flush_channel_lines", "_pipe_select_supported",
        "_queue_get", "_read_attached", "_read_fd", "run_stream", "os", "select",
        "subprocess", "OwnedProcess", "RemoteCompleted", "current_event",
        "record_cleanup_failure", "RemoteExecutionError",
        "STREAM_SELECT_SLICE_SECONDS", "stream_ssh_command",
    ):
        if hasattr(transport, name):
            _ORIGINALS[name] = getattr(transport, name)


def _resolve(name: str):
    """Return a transport- or stream-level monkeypatch over the original object."""
    transport = _transport()
    local = globals().get(name)
    remote = getattr(transport, name, local)
    original = _ORIGINALS.get(name, _MISSING)
    if original is not _MISSING and remote is not original:
        return remote
    if local is not None and (original is _MISSING or local is not original):
        return local
    return remote


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

    Returns the real remote exit code, or ``timed_out=True`` after a local
    wall-clock deadline. ``merge_stderr=True`` forwards merged output and
    leaves captures empty; pass ``merge_stderr=False`` to capture both channels
    and invoke ``on_output(channel, text)`` per line. This is a transport
    primitive, not a ``remote-dev.result.v1`` document or a detached job.
    """
    dest = sys.stderr if output is None and merge_stderr else output
    cmd = _resolve("stream_ssh_command")(endpoint, None, timeout_ms=timeout_ms)
    payload = script.encode("utf-8")
    subprocess_mod = _resolve("subprocess")
    owner = _resolve("OwnedProcess")(
        cmd,
        stdin=subprocess_mod.PIPE,
        stdout=subprocess_mod.PIPE,
        stderr=subprocess_mod.STDOUT if merge_stderr else subprocess_mod.PIPE,
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
        except TimeoutError:
            raise
        except (BrokenPipeError, OSError):
            pass  # The reader reports SSH exit status and stderr.
        finally:
            _resolve("_close_pipe")(proc.stdin)

    writer = threading.Thread(target=upload, daemon=True, name="remote-dev-stream-upload")
    primary_error = None
    completed = None
    try:
        writer.start()
        completed = _resolve("_read_attached")(
            owner,
            timeout_ms=timeout_ms,
            forward_prefix=forward_prefix,
            output=dest,
            on_output=on_output,
            capture=not merge_stderr,
        )
        return completed
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            owner.stop()
        except Exception as cleanup_error:
            if primary_error is not None:
                _resolve("record_cleanup_failure")(primary_error, cleanup_error)
            else:
                completed_with_exit = completed is not None and completed.returncode is not None
                failure = _resolve("RemoteExecutionError")("attached SSH operation ended but local cleanup failed",
                                               category="cleanup",
                                               submission_state="acknowledged" if completed_with_exit else "uncertain")
                failure.operation_completed = completed_with_exit
                failure.completed = completed
                _resolve("record_cleanup_failure")(failure, cleanup_error)
                raise failure from cleanup_error
        finally:
            if writer.ident is not None:
                writer.join(timeout=1)


_STREAM_READ_BYTES = 4096


def _read_fd(fd: int) -> bytes | None:
    """Read available bytes. ``None`` means EOF; ``b''`` means try again."""
    try:
        chunk = _resolve("os").read(fd, _STREAM_READ_BYTES)
    except BlockingIOError:
        return b""
    except TimeoutError:
        raise
    except OSError:
        return None
    return chunk if chunk else None


def _pipe_select_supported() -> bool:
    """``select`` on subprocess pipes is POSIX-only. Windows needs threads."""
    return _resolve("os").name != "nt"


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
        self.use_select = _resolve("_pipe_select_supported")()
        self._queue: queue.Queue[tuple[int, bytes | None]] | None = None
        self._threads: list[threading.Thread] = []
        if self.use_select:
            for channel in channels:
                _resolve("os").set_blocking(channel.fd, False)
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
            chunk = _resolve("_read_fd")(channel.fd)
            if chunk is None:
                self._queue.put((channel.fd, None))
                return
            if chunk:
                self._queue.put((channel.fd, chunk))

    def wait(self, open_fds: set[int], timeout: float | None) -> list[tuple[int, bytes | None]]:
        if not open_fds:
            return []
        if self.use_select:
            ready, _, _ = _resolve("select").select(list(open_fds), [], [], timeout)
            return [(fd, _resolve("_read_fd")(fd)) for fd in ready]
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
                    chunk = _resolve("_read_fd")(fd)
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
        _resolve("_emit_stream_text")(
            line,
            channel=channel.name,
            forward_prefix=forward_prefix,
            output=output,
            on_output=on_output,
            captured=channel.captured if capture else None,
        )
    if final and channel.pending:
        _resolve("_emit_stream_text")(
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
    except TimeoutError:
        raise
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
    """Deadline-aware merged/separate reader (`capture=False` forwards live only)."""
    proc = owner.process
    assert proc.stdout is not None
    channels = [_StreamChannel(name="stdout", fd=proc.stdout.fileno())]
    if proc.stderr is not None and proc.stderr is not proc.stdout:
        channels.append(_StreamChannel(name="stderr", fd=proc.stderr.fileno()))
    by_fd = {channel.fd: channel for channel in channels}
    open_fds = set(by_fd)
    pipes = _AttachedPipes(channels)
    started = time.monotonic()
    deadline = None if timeout_ms is None else started + (timeout_ms / 1000)
    cancelled = _resolve("current_event")()
    timeout_message = (
        f"remote command exceeded {timeout_ms} ms wall-clock limit" if timeout_ms is not None else ""
    )

    def captured_stdout() -> str:
        stdout = next(channel for channel in channels if channel.name == "stdout")
        return "".join(stdout.captured)

    def captured_stderr() -> str:
        stderr = next((channel for channel in channels if channel.name == "stderr"), None)
        return "" if stderr is None else "".join(stderr.captured)

    def stop_owner() -> None:
        # Capture the observed exit before cleanup: a subsequent kill must
        # not manufacture acknowledgement of the original remote operation.
        returncode = proc.poll()
        try:
            owner.stop()
        except Exception as cleanup_error:
            failure = _resolve("RemoteExecutionError")("attached SSH cleanup failed",
                                           category="cleanup",
                                           submission_state="acknowledged" if returncode is not None else "uncertain")
            failure.operation_completed = returncode is not None
            failure.completed = _resolve("RemoteCompleted")(returncode,
                                                captured_stdout() if capture else "",
                                                captured_stderr() if capture else "")
            _resolve("record_cleanup_failure")(failure, cleanup_error)
            raise failure from cleanup_error

    def finalize_open_channels() -> None:
        for channel in channels:
            leftover = ""
            try:
                leftover = channel.decoder.decode(b"", final=True)
            except Exception:
                leftover = ""
            _resolve("_flush_channel_lines")(
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
                _resolve("_flush_channel_lines")(channel, text=channel.decoder.decode(chunk),
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
        return _resolve("RemoteCompleted")(
            None,
            captured_stdout() if capture else "",
            stderr,
            timed_out=True,
        )

    def close_channel(channel: _StreamChannel) -> None:
        stream = proc.stdout if channel.name == "stdout" else proc.stderr
        _resolve("_close_pipe")(stream)

    def close_pipes() -> None:
        seen: set[int] = set()
        for stream in (proc.stdout, proc.stderr):
            if stream is None or id(stream) in seen:
                continue
            seen.add(id(stream))
            _resolve("_close_pipe")(stream)

    def remaining_wait() -> float | None:
        if deadline is None:
            return _resolve("STREAM_SELECT_SLICE_SECONDS")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(remaining, _resolve("STREAM_SELECT_SLICE_SECONDS"))

    primary_error = None
    try:
        while True:
            if cancelled is not None and cancelled.is_set():
                failure = _resolve("RemoteExecutionError")("attached SSH operation cancelled; remote outcome may be unknown",
                                               category="cancelled", submission_state="uncertain")
                try:
                    stop_owner()
                except Exception as cleanup_error:
                    _resolve("record_cleanup_failure")(failure, cleanup_error)
                raise failure
            wait = remaining_wait()
            if wait is not None and wait <= 0:
                stop_owner()
                return timed_out_result()
            if not open_fds:
                # Pipes have closed. Wait until the real deadline (or forever
                # if none). A select-slice TimeoutExpired must not kill here:
                # grandchildren can close stdout/stderr while the child still
                # has work left.
                if proc.poll() is not None:
                    try:
                        returncode = proc.wait(timeout=5.0)
                    except _resolve("subprocess").TimeoutExpired:
                        with contextlib.suppress(OSError):
                            proc.kill()
                        returncode = proc.wait(timeout=5.0)
                    return _resolve("RemoteCompleted")(
                        returncode,
                        captured_stdout() if capture else "",
                        captured_stderr() if capture else "",
                    )
                time.sleep(wait)
                continue
            ready = pipes.wait(open_fds, wait)
            if ready:
                for fd, chunk in ready:
                    channel = by_fd[fd]
                    if chunk is None:
                        _resolve("_flush_channel_lines")(
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
                    _resolve("_flush_channel_lines")(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                if proc.poll() is not None:
                    stop_owner()
                continue
            if proc.poll() is None:
                continue
            # Parent exit does not close descriptors inherited by a proxy child.
            # Stop the owned local group before draining or closing its pipes.
            stop_owner()
            pipes.finish_readers()
            drained = pipes.drain(open_fds)
            for fd, chunk in drained:
                channel = by_fd[fd]
                if chunk:
                    _resolve("_flush_channel_lines")(
                        channel,
                        text=channel.decoder.decode(chunk),
                        forward_prefix=forward_prefix,
                        output=output,
                        on_output=on_output,
                        capture=capture,
                        final=False,
                    )
                if not chunk:
                    _resolve("_flush_channel_lines")(
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
                _resolve("_flush_channel_lines")(
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
            try:
                returncode = proc.wait(timeout=5.0)
            except _resolve("subprocess").TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
                returncode = proc.wait(timeout=5.0)
            return _resolve("RemoteCompleted")(
                returncode,
                captured_stdout() if capture else "",
                captured_stderr() if capture else "",
            )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            stop_owner()
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            _resolve("record_cleanup_failure")(primary_error, cleanup_error)
        finally:
            pipes.finish_readers()
            close_pipes()

