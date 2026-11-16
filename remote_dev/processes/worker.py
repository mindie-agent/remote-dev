"""Linux remote job supervisor shipped through the process-control transport.

[Role] Remote asynchronous background job daemon & process-group supervisor (`worker.py`).
[Invariants]
- Runs on the remote target host under `python -I` isolated startup to prevent `cwd` stdlib shadowing.
- `cancel_and_drain()` waits `max(5.0, STOP_GRACE_SECONDS + 3.0)` after `SIGTERM` before returning an uncertain observation.
- Persists atomic `status.json` transitions via `.tmp.<pid>` + `os.replace()`.
- Streams child output through `worker_runtime.CappedLog`, which keeps `<stream>.log.prev`
  plus `<stream>.log` at or below `spec.max_log_bytes` (default 256 MiB per stream) and
  publishes `logical_bytes_written` / `truncated_head_bytes` in `logs.json`/`result.json`.
- A terminal job directory is only removed by `worker_gc.collect_terminal_jobs`
  while holding the exclusive `worker.lock`/`lock` fences across re-verification and
  `os.replace`; `running`, `unknown`, and `retain_until_release` jobs are kept.
- `stop` records `stop.json` and signals before optional maintenance. The entire bounded
  maintenance check attaches `maintenance_warning` on failure and never aborts prepare/status/stop.
[Timeouts & Locks]
- Uses non-blocking `fcntl.flock` on `worker.lock` to guarantee a single active supervisor per job directory.
- Tail reads take `logs.lock` in shared mode against the supervisor's exclusive append/rotate lock.
- `STOP_GRACE_SECONDS` defaults to `2.0` (`REMOTE_DEV_STOP_GRACE_SECONDS` may override it); the process group is escalated to `SIGKILL` after that window.
[Side-Effect Policy]
- Writes `status.json`, `logs.json`, `stdout.log`, `stderr.log`, `worker.lock`, and
  `worker.lock`-scoped housekeeping records exclusively inside the assigned remote job
  directory or its owned `.remote-dev/jobs` parent; signals only the target child process group.
"""
import contextlib, ctypes, errno, fcntl, hashlib, json, math, os, pty, re
import select, signal, subprocess, struct, sys, termios, time, uuid
from pathlib import Path

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
JOB_TOKEN_ENV = "REMOTE_DEV_JOB_TOKEN"
JOB_ENV_PREFIX = "REMOTE_DEV_JOB_"
JOBS_DIRNAME = ".remote-dev"
# Cap FIFO bytes buffered in worker RAM; past it we stop draining so writers block.
STDIN_BUFFER_CAP = 262144
# POSIX PIPE_BUF writes are atomic, keeping accepted stdin prefixes on exact bytes.
STDIN_WRITE_CHUNK = getattr(select, "PIPE_BUF", 512)
# States in which a job may still append to its logs.
LIVE_JOB_STATES = frozenset({"prepared", "running", "uncertain"})
_INITIAL_STOP_GRACE_SECONDS = float(os.environ.get("REMOTE_DEV_STOP_GRACE_SECONDS", "2.0"))
STOP_GRACE_SECONDS = _INITIAL_STOP_GRACE_SECONDS

def _stop_grace_seconds() -> float:
    if STOP_GRACE_SECONDS != _INITIAL_STOP_GRACE_SECONDS:
        return float(STOP_GRACE_SECONDS)
    return float(os.environ.get("REMOTE_DEV_STOP_GRACE_SECONDS", str(STOP_GRACE_SECONDS)))

def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()

def _log_cap_bytes(spec):
    value = spec.get("max_log_bytes", DEFAULT_MAX_LOG_BYTES)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("jobs require max_log_bytes to be a finite positive number")
    return int(value)

def job_status(directory):
    receipt = read_json(directory / "receipt.json")
    if receipt is None:
        return {"state": "uncertain", "reason": "launch intent exists without a process receipt", "quiet": False}
    result = read_json(directory / "result.json")
    # A verified subreaper completion is stronger than a /proc walk; its own
    # receipt PID may still be exiting Python and is not a running child.
    if (result and result.get("descendants_drained") and
            receipt.get("supervision") == "subreaper" and receipt["boot_id"] == boot_id()):
        processes = []
        unknown = []
    else:
        processes, unknown = owned_processes(receipt)
        if result and result.get("descendants_drained"):
            processes = [row for row in processes if row["pid"] != receipt["pid"]]
    gate = read_json(directory / "go.json")
    if receipt.get("supervision") == "subreaper" and result is None and not any(
            row["pid"] == receipt["pid"] and row["start_ticks"] == receipt["start_ticks"] for row in processes):
        unknown.append("supervisor lost without a descendant-drained receipt")
    if gate and processes:
        spec = read_json(directory / "spec.json")
        opened = gate["opened_at"]
        timeout_seconds = (spec or {}).get("timeout_seconds")
        if timeout_seconds is not None and time.time() >= opened + timeout_seconds:
            # The shell may exit while a background descendant stays alive.
            # Report a timeout so the caller also stops that process family.
            result = {"state": "timeout", "reason": "owned descendants exceeded the execution deadline"}
    if unknown:
        state = "uncertain"
    elif processes:
        state = "running" if gate else "prepared"
    elif result:
        state = result["state"]
    elif (directory / "stop.json").exists():
        # Only legacy receipts without subreaper supervision reach this branch:
        # their supervisor could exit on stop without publishing result.json.
        # A subreaper worker always publishes a result, even for cancellation.
        state = "cancelled"
    else:
        state = "lost_outcome"
    public = {key: value for key, value in receipt.items() if key != "marker"}
    # A random ownership marker is not a credential. The caller retains
    # process ownership while any marked process is alive.
    public["process_guard"] = {"marker": receipt["marker"], "boot_id": receipt["boot_id"]}
    if receipt.get("supervision") == "subreaper":
        # A vanished supervisor cannot prove its clean-environment descendants
        # are gone. Callers must wait for verified completion.
        public["process_guard"]["retain_until_release"] = True
    return {"state": state, "quiet": not processes and not unknown, "receipt": public,
            "processes": processes, "unknown": unknown, "result": result,
            "supervisor_error": read_json(directory / "supervisor-error.json"),
            "remote_dir": str(directory), "gate_open": bool(gate)}

def worker(directory):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot supervise orphan descendants")
    spec = read_json(directory / "spec.json")
    # Hold the supervisor fence for this process lifetime so housekeeping never
    # mistakes a live supervisor for a terminal one.
    worker_lock = (directory / "worker.lock").open("a+b")
    fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Interactive jobs proxy the on-disk FIFO into the child pipe, attaching the
    # reader before supervisor-ready.json so a post-prepare write always finds it.
    tty = bool(spec.get("tty"))
    interactive = bool(spec.get("interactive")) or tty
    fifo_fd = None
    if interactive:
        fifo_fd = os.open(directory / "stdin.pipe", os.O_RDONLY | os.O_NONBLOCK)
    atomic_json(directory / "supervisor-ready.json", {"pid": os.getpid()})
    prepared_timeout = spec.get("prepared_timeout_seconds")
    deadline = None if prepared_timeout is None else time.monotonic() + prepared_timeout
    while not (directory / "go.json").exists():
        if (directory / "stop.json").exists() or (deadline is not None and time.monotonic() >= deadline):
            if fifo_fd is not None:
                os.close(fifo_fd)
            atomic_json(directory / "result.json", {"state": "cancelled", "reason": "start gate not opened", "descendants_drained": True})
            return
        time.sleep(0.005)
    gate = read_json(directory / "go.json")
    if (directory / "stop.json").exists() or (gate.get("valid_until") is not None and gate["valid_until"] <= time.time()):
        if fifo_fd is not None:
            os.close(fifo_fd)
        atomic_json(directory / "result.json", {"state": "cancelled", "reason": "activation ticket expired", "descendants_drained": True})
        return
    pipe_w = None
    master_fd = None
    if tty:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.set_blocking(master_fd, False)
        pipe_w = master_fd
    elif interactive:
        pipe_r, pipe_w = os.pipe()
        os.set_blocking(pipe_w, False)
    command_started = time.monotonic()
    command_started_at = time.time()
    max_log_bytes = _log_cap_bytes(spec)
    with contextlib.ExitStack() as log_stack:
        stdout_log = log_stack.enter_context(CappedLog(directory, "stdout", max_log_bytes))
        stderr_log = log_stack.enter_context(CappedLog(directory, "stderr", max_log_bytes))
        output_pipes = {}
        pipe_eof = set()
        environment = {**os.environ, **spec["env"]}
        if tty:
            environment.setdefault("TERM", "xterm-256color")

            def terminal_child():
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.tcsetpgrp(slave_fd, os.getpgrp())

            child = subprocess.Popen(["bash", "-c", spec["command"]], cwd=spec["cwd"], env=environment,
                                     stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                     preexec_fn=terminal_child)
            os.close(slave_fd)
        else:
            child = subprocess.Popen(["bash", "-c", spec["command"]], cwd=spec["cwd"], env=environment,
                                     stdin=pipe_r if interactive else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for stream_name, output in (("stdout", child.stdout), ("stderr", child.stderr)):
                os.set_blocking(output.fileno(), False)
                output_pipes[stream_name] = output
        if interactive and not tty:
            os.close(pipe_r)
        receipt = read_json(directory / "receipt.json")
        timeout_seconds = spec.get("timeout_seconds")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        spawned_at = time.monotonic()
        stopping_at, terminal = None, None
        shell_exit_observed = None
        pending_stdin = bytearray()
        stdin_eof = False
        fifo_drained = False
        terminal_eof_sent = False
        while True:
            code = child.poll()
            if code is not None and shell_exit_observed is None:
                shell_exit_observed = time.monotonic()
            if master_fd is not None:
                # Drain the PTY into the same durable output log. stdout and
                # stderr share a terminal, as with a native tty session.
                for _ in range(16):
                    try:
                        chunk = os.read(master_fd, 65536)
                    except BlockingIOError:
                        break
                    except TimeoutError:
                        raise
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break  # last slave closed
                    if not chunk:
                        break
                    stdout_log.write(chunk)
            for stream_name, output in output_pipes.items():
                if stream_name in pipe_eof:
                    continue
                for _ in range(16):
                    try:
                        chunk = os.read(output.fileno(), 65536)
                    except BlockingIOError:
                        break
                    except TimeoutError:
                        raise
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        pipe_eof.add(stream_name)
                        break
                    if not chunk:
                        pipe_eof.add(stream_name)
                        break
                    (stdout_log if stream_name == "stdout" else stderr_log).write(chunk)
            if code is not None:
                # poll() reaps the direct Popen child first. Then reap adopted
                # orphans without stealing the shell's exit status.
                with contextlib.suppress(ChildProcessError):
                    while os.waitpid(-1, os.WNOHANG)[0]:
                        pass
            if interactive:
                # Backpressure: once the in-memory buffer hits the cap, stop
                # draining the FIFO so writers block instead of growing RAM.
                if fifo_fd is not None and len(pending_stdin) < STDIN_BUFFER_CAP:
                    try:
                        chunk = os.read(fifo_fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                    if chunk:
                        pending_stdin.extend(chunk)
                    elif stdin_eof:
                        # b"" from a FIFO read means every writer is gone and
                        # the kernel buffer is empty: all accepted stdin bytes
                        # are now in pending_stdin, none stay behind in the FIFO.
                        fifo_drained = True
                if pending_stdin and pipe_w is not None:
                    try:
                        written = os.write(pipe_w, bytes(pending_stdin))
                        del pending_stdin[:written]
                    except BlockingIOError:
                        pass
                    except TimeoutError:
                        raise
                    except OSError as exc:
                        if exc.errno not in (errno.EPIPE, errno.EIO):
                            raise
                        pending_stdin.clear()
                        if not tty:
                            os.close(pipe_w)
                            pipe_w = None
                if not stdin_eof and (directory / "stdin-eof.json").exists():
                    stdin_eof = True
                if stdin_eof and fifo_drained and fifo_fd is not None:
                    os.close(fifo_fd)
                    fifo_fd = None
                if stdin_eof and fifo_drained and not pending_stdin and pipe_w is not None:
                    # EOF reaches the child only after every accepted byte.
                    if tty:
                        if not terminal_eof_sent:
                            os.write(pipe_w, b"\x04")
                            terminal_eof_sent = True
                    else:
                        os.close(pipe_w)
                        pipe_w = None
            stop = read_json(directory / "stop.json")
            timed_out = deadline is not None and time.monotonic() >= deadline
            if terminal is None and (stop or timed_out):
                terminal = "timeout" if timed_out else "cancelled"
                stopping_at = time.monotonic()
            if terminal:
                processes, _ = owned_processes(receipt)
                force = bool(stop and stop.get("force")) or time.monotonic() >= stopping_at + _stop_grace_seconds()
                signal_processes(processes, signal.SIGKILL if force else signal.SIGTERM, exclude=os.getpid())
            # This kernel list includes zombies and adopted clean-env children.
            # A process cannot fork again after it has been reaped; only this
            # empty-child boundary allows the supervisor to publish completion.
            children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text(encoding="utf-8", errors="replace").strip()
            if code is not None and not children and len(pipe_eof) >= len(output_pipes):
                break
            time.sleep(0.01)
        for output in output_pipes.values():
            with contextlib.suppress(OSError):
                output.close()
        if pipe_w is not None:
            os.close(pipe_w)
        if fifo_fd is not None:
            os.close(fifo_fd)
    command_finished = time.monotonic()
    shell_exit_observed = shell_exit_observed if shell_exit_observed is not None else command_finished
    log_accounting = {"max_log_bytes": max_log_bytes}
    for _name, _log in (("stdout", stdout_log), ("stderr", stderr_log)):
        log_accounting[_name] = {
            "logical_bytes_written": _log.logical_bytes_written,
            "truncated_head_bytes": _log.truncated_head_bytes,
            "generation": _log.generation,
            "retained_bytes": _file_size(_log.prev_path) + _file_size(_log.path),
        }
        if _log.accounting_warning:
            log_accounting[_name]["accounting_warning"] = _log.accounting_warning
    _warnings = [
        _log.accounting_warning
        for _log in (stdout_log, stderr_log) if _log.accounting_warning
    ]
    if _warnings:
        log_accounting["accounting_warning"] = "; ".join(_warnings)
    result = {"state": terminal or ("succeeded" if code == 0 else "failed"),
              "exit_code": code, "descendants_drained": True,
              "started_at": command_started_at, "finished_at": time.time(),
              "logs": log_accounting,
              "timings": {"clock_domain": "remote-supervisor:" + boot_id() + ":" + str(os.getpid()),
                          "spawn_ms": round((spawned_at - command_started) * 1000, 3),
                          "shell_ms": round((shell_exit_observed - spawned_at) * 1000, 3),
                          "descendant_drain_ms": round((command_finished - shell_exit_observed) * 1000, 3)}}
    atomic_json(directory / "result.json", result)

def resolve_runtime_root(request, *, absent_is_result):
    """Resolve the runtime root without creating it; absent is a result only for observation."""
    try:
        return Path(request["root"]).resolve(strict=True)
    except FileNotFoundError:
        if absent_is_result:
            return None
        raise

def cancel_and_drain(request, source):
    status = control_job({**request, "action": "stop"}, source)
    deadline = time.monotonic() + max(5.0, _stop_grace_seconds() + 3.0)
    while not status.get("quiet") and not status.get("unknown") and time.monotonic() < deadline:
        time.sleep(0.02)
        status = control_job({**request, "action": "status"}, source)
    return status

def _bounded_housekeeping(root, identifier):
    """Run opportunistic GC without ever failing prepare/status/stop."""
    try:
        return maybe_run_job_housekeeping(root, exclude=(identifier,))
    except TimeoutError as exc:
        return {"skipped": "timeout", "error": f"TimeoutError: {exc}"}
    except OSError as exc:
        return {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}
    except (TypeError, ValueError) as exc:
        return {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}


def control_job(request, source, cancel_event=None):
    """One process authority for gated launches and native execute/poll calls."""
    action = request["action"]
    if action == "launch":
        if cancel_event is not None and cancel_event.is_set():
            # A pre-cancelled launch has not prepared or opened its gate. If
            # this identity already exists, reconcile/cancel that same job.
            return {**cancel_and_drain(request, source), "cancellation_requested": True}
        started = time.monotonic()
        prepared = control_job({**request, "action": "prepare"}, source, cancel_event)
        prepared_at = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return cancel_and_drain(request, source)
        if not prepared.get("gate_open"):
            control_job({**request, "action": "go"}, source)
        activated_at = time.monotonic()
        observation = control_job({**request, "action": "exchange"}, source, cancel_event)
        observation["timings"] = {"prepare_ms": round((prepared_at-started)*1000),
                                  "activate_ms": round((activated_at-prepared_at)*1000),
                                  "wait_observe_ms": round((time.monotonic()-activated_at)*1000)}
        return observation
    if action == "exchange":
        reply = {"accepted": True, "written": 0, "written_chars": 0}
        has_input = bool(request.get("data") or request.get("eof"))
        if has_input:
            reply = control_job({**request, "action": "stdin"}, source)
        identifier = request["job_id"]
        if not JOB_ID_RE.fullmatch(identifier):
            raise ValueError("invalid job id")
        # Input-free exchange is an output/quiet observation and may report an
        # absent root. An exchange carrying data/eof has already gone through
        # the strict stdin path, so a vanished root stays visible.
        root = resolve_runtime_root(request, absent_is_result=not has_input)
        if root is None:
            return {"state": "absent", "quiet": True}
        directory = root / JOBS_DIRNAME / "jobs" / identifier
        if root not in directory.resolve().parents:
            raise ValueError("job directory escapes the runtime root")
        wait_ms = max(0, min(300000, int(request.get("yield_time_ms") or 0)))
        deadline = time.monotonic() + wait_ms / 1000
        first_available_at = None
        last_output_change = None
        last_sizes = (int(request.get("stdout_offset") or 0), int(request.get("stderr_offset") or 0))
        while reply.get("accepted") and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                break
            if (directory / "result.json").exists() or not directory.exists():
                break
            sizes = []
            available = False
            for stream in ("stdout", "stderr"):
                size = log_stream_size(directory, stream)
                sizes.append(size)
                if size > int(request.get(stream + "_offset") or 0):
                    available = True
            now = time.monotonic()
            if tuple(sizes) != last_sizes:
                last_sizes = tuple(sizes)
                last_output_change = now
            if available:
                if first_available_at is None:
                    first_available_at = now
                if last_output_change is None:
                    last_output_change = now
                # Synchronous capture waits for completion; otherwise coalesce short output.
                if not request.get("wait_for_exit"):
                    grace_deadline = min(deadline, first_available_at + 0.5, last_output_change + 0.15)
                    if now >= grace_deadline:
                        break
                    time.sleep(min(0.01, max(0, grace_deadline - now)))
                    continue
            time.sleep(min(0.01, max(0, deadline - now)))
        if cancel_event is not None and cancel_event.is_set():
            cancel_and_drain(request, source)
            reply["cancellation_requested"] = True
        observation = control_job({**request, "action": "tail"}, source)
        return {**reply, **observation}
    identifier = request["job_id"]
    if not JOB_ID_RE.fullmatch(identifier):
        raise ValueError("invalid job id")
    # Observation/stop controls treat an absent root as absent; mutating actions
    # keep strict resolution and surface the failure.
    root = resolve_runtime_root(request, absent_is_result=action in {"status", "stop", "tail"})
    if root is None:
        return {"state": "absent", "quiet": True}
    directory = root / JOBS_DIRNAME / "jobs" / identifier
    if root not in directory.resolve().parents:
        raise ValueError("job directory escapes the runtime root")
    if not directory.exists() and action != "prepare":
        return {"state": "absent", "quiet": True}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    maintenance = None
    with (directory / "lock").open("a", encoding="utf-8") as lock:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("process control cancelled before lock acquisition")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.02)
        if action == "prepare":
            spec = request["spec"]
            cwd = Path(spec["cwd"]).resolve(strict=True)
            if cwd != root and root not in cwd.parents:
                raise ValueError("command cwd escapes the runtime root")
            if not cwd.is_dir():
                raise NotADirectoryError("command cwd is not a directory")
            timeout_seconds = spec.get("timeout_seconds")
            if timeout_seconds is not None and (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise ValueError("jobs require timeout_seconds None or a finite positive number")
            prepared_timeout = spec.get("prepared_timeout_seconds")
            if prepared_timeout is not None and (type(prepared_timeout) not in (int, float) or not math.isfinite(prepared_timeout) or prepared_timeout <= 0):
                raise ValueError("jobs require prepared_timeout_seconds None or a finite positive number")
            _log_cap_bytes(spec)
            if any(not ENV_NAME_RE.fullmatch(key) or key.startswith(JOB_ENV_PREFIX) for key in spec["env"]):
                raise ValueError("invalid or reserved environment variable")
            for flag in ("interactive", "tty"):
                if flag in spec and type(spec[flag]) is not bool:
                    raise ValueError(f"jobs require {flag} to be a boolean")
            intent = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            existing = read_json(directory / "intent.json")
            if existing:
                if existing["digest"] != intent:
                    raise ValueError("job id reused with different launch arguments")
                return job_status(directory)
            atomic_json(directory / "intent.json", {"digest": intent})
            atomic_json(directory / "spec.json", spec)
            if spec.get("interactive") or spec.get("tty"):
                # The stdin channel is an on-disk FIFO owned by the job dir.
                # The worker holds the read end and proxies bytes into the
                # child pipe; writers use the "stdin" control action.
                try:
                    os.mkfifo(directory / "stdin.pipe", 0o600)
                except FileExistsError:
                    pass
            script = directory / "runner.py"
            script.write_text(source, encoding="utf-8")
            os.chmod(script, 0o600)
            marker = uuid.uuid4().hex
            with (directory / "supervisor.log").open("ab") as supervisor_log:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(script), "--worker", str(directory)],
                    env={**os.environ, JOB_TOKEN_ENV: marker},
                    stdin=subprocess.DEVNULL, stdout=supervisor_log, stderr=supervisor_log,
                    start_new_session=True,
                )
            try:
                identity = process_identity(process.pid)
                if not identity or identity["pgid"] != process.pid:
                    raise RuntimeError("waiting supervisor has no verified process identity")
                atomic_json(directory / "receipt.json", {**identity, "boot_id": boot_id(), "marker": marker,
                                                          "diagnostics_context": {key: value for key, value in (request.get("diagnostics_context") or {}).items()
                                                              if key in {"trace_id", "operation_id", "parent_operation_id"}
                                                              and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)},
                                                          "supervision": "subreaper", "job_id": identifier,
                                                          "prepared_timeout_seconds": prepared_timeout,
                                                          "prepared_at": time.time()})
                while not (directory / "supervisor-ready.json").exists():
                    if cancel_event is not None and cancel_event.is_set():
                        atomic_json(directory / "stop.json", {"requested_at": time.time()})
                        raise RuntimeError("supervisor preparation cancelled; inspect the original job receipt")
                    if process.poll() is not None:
                        with (directory / "supervisor.log").open("rb") as log:
                            log.seek(max(0, log.seek(0, 2) - 8000))
                            detail = log.read().decode("utf-8", "replace")
                        raise RuntimeError(f"waiting supervisor exited with {process.returncode} before readiness: {detail}")
                    time.sleep(0.005)
            except BaseException as error:
                # The mutation lock still excludes go; reclaim our exact child on failure.
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                except Exception as cleanup_error:
                    error.cleanup_error = f"{type(cleanup_error).__name__}: {cleanup_error}"
                    error.add_note("Supervisor cleanup also failed: " + error.cleanup_error)
                raise
            maintenance = _bounded_housekeeping(root, identifier)
        elif action == "go":
            status = job_status(directory)
            existing = read_json(directory / "go.json")
            if existing:
                if existing["authorization"] != request["authorization"]:
                    raise ValueError("start gate already belongs to another authorization")
                return status
            if status["state"] != "prepared" or (directory / "stop.json").exists():
                raise RuntimeError("job is not a verified waiting supervisor")
            opened = time.time()
            activation_timeout = request.get("activation_timeout_seconds")
            if activation_timeout is not None and (type(activation_timeout) not in (int, float) or not math.isfinite(activation_timeout) or activation_timeout <= 0):
                raise ValueError("jobs require activation_timeout_seconds None or a finite positive number")
            atomic_json(directory / "go.json", {"authorization": request["authorization"], "opened_at": opened,
                                               "valid_until": None if activation_timeout is None else opened + activation_timeout})
        elif action == "stop":
            receipt = read_json(directory / "receipt.json")
            if receipt is None:
                maintenance = _bounded_housekeeping(root, identifier)
                return attach_maintenance_warning(job_status(directory), maintenance)
            status = job_status(directory)
            if status["quiet"] or status["unknown"]:
                maintenance = _bounded_housekeeping(root, identifier)
                return attach_maintenance_warning(status, maintenance)
            # Record and signal the stop FIRST; housekeeping must never delay
            # the cancellation side effect.
            processes = status["processes"]
            atomic_json(directory / "stop.json", {"at": time.time(), "force": bool(request.get("force"))})
            # Signal only PIDs whose marker/start ticks were just observed; keep
            # the subreaper alive so it drains and publishes completion.
            signal_processes(processes, signal.SIGKILL if request.get("force") else signal.SIGTERM,
                             exclude=receipt["pid"] if receipt.get("supervision") == "subreaper" else None)
            maintenance = _bounded_housekeeping(root, identifier)
        elif action == "tail":
            lines = min(200, max(1, int(request.get("lines", 60))))
            result = job_status(directory)
            max_bytes = min(32768, max(1, int(request.get("max_bytes") or 32768)))
            shared_budget = bool(request.get("shared_budget"))
            remaining_budget = max_bytes
            live = result.get("state") in LIVE_JOB_STATES
            for name in ("stdout", "stderr"):
                path = directory / (name + ".log")
                if not path.exists() and not (directory / (name + ".log.prev")).exists():
                    continue
                offset_key = name + "_offset"
                if request.get(offset_key) is not None:
                    # Incremental read: logical bytes after the caller cursor only.
                    start_offset = max(0, int(request[offset_key]))
                    window = read_log_window(
                        directory, name, start_offset,
                        remaining_budget if shared_budget else max_bytes)
                    chunk = window["data"]
                    hold = _utf8_incomplete_tail(chunk)
                    if hold == len(chunk) and not shared_budget:
                        # Progress guarantee: a page must always advance the
                        # cursor, even for budgets smaller than one UTF-8
                        # character; the partial bytes flush as U+FFFD.
                        hold = 0
                    elif not live and window["end"] >= window["logical_bytes"]:
                        # Terminal job at the retained end: no later write can
                        # complete the sequence, so flush the tail now.
                        hold = 0
                    if hold:
                        # Hold back a split UTF-8 character; the next poll
                        # re-reads it and bytes_remaining stays accurate.
                        chunk = chunk[:-hold]
                    if shared_budget:
                        # Invalid source bytes expand to three-byte U+FFFD.
                        # Budget the decoded response, retaining unread raw
                        # bytes at the cursor instead of discarding output.
                        while (decoded_size := len(chunk.decode("utf-8", "replace").encode("utf-8"))) > remaining_budget:
                            chunk = chunk[:len(chunk) * remaining_budget // decoded_size]
                            hold = _utf8_incomplete_tail(chunk)
                            if hold:
                                chunk = chunk[:-hold]
                    result[name] = chunk.decode("utf-8", errors="replace")
                    if shared_budget:
                        remaining_budget = max(0, remaining_budget - len(result[name].encode("utf-8")))
                    result[offset_key] = window["start"] + len(chunk)
                    result[name + "_bytes_remaining"] = max(
                        0, window["logical_bytes"] - result[offset_key])
                    result[name + "_logical_bytes"] = window["logical_bytes"]
                    result[name + "_truncated_head_bytes"] = window["truncated_head_bytes"]
                    if window["skipped_bytes"]:
                        # The retained ring rotated away bytes before the cursor;
                        # report the jump instead of presenting it as clean EOF.
                        result[name + "_truncated"] = True
                        result[name + "_skipped_bytes"] = window["skipped_bytes"]
                else:
                    window = read_log_window(directory, name, None, 32000, tail_bytes=32000)
                    result[name] = "\n".join(window["data"].decode(errors="replace").splitlines()[-lines:])
                    result[name + "_logical_bytes"] = window["logical_bytes"]
                    result[name + "_truncated_head_bytes"] = window["truncated_head_bytes"]
            return result
        elif action == "stdin":
            status = job_status(directory)
            data = str(request.get("data") or "")
            if status["state"] != "running":
                if not data:
                    # Pure output polls (and a redundant eof) stay legal on a
                    # terminal job, mirroring Codex write_stdin's final poll.
                    return {"state": status["state"], "accepted": True, "written": 0,
                            "eof": bool(request.get("eof")), "polled_terminal": True}
                return {"state": status["state"], "accepted": False, "written": 0,
                        "reason": f"job is {status['state']}, not running; stdin writes need a running job"}
            fifo = directory / "stdin.pipe"
            if not fifo.exists():
                return {"state": status["state"], "accepted": False, "written": 0,
                        "reason": "job has no stdin channel; start a writable remote.bash session"}
            raw = data.encode("utf-8")
            if raw and (directory / "stdin-eof.json").exists():
                return {"state": status["state"], "accepted": False, "written": 0,
                        "reason": "stdin is already closed (eof); start a new interactive job for more input"}
            written = 0
            if raw:
                # Each write is <= PIPE_BUF and ends on a UTF-8 boundary, so a
                # nonblocking write lands completely or raises EAGAIN. The accepted
                # prefix maps to an exact character count for retrying the rest.
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                except TimeoutError:
                    raise
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        raise
                    # No reader is attached (supervisor still starting, or a
                    # job dir prepared by an older worker): a truthful
                    # retryable refusal — no bytes are accepted or dropped and
                    # EOF is not armed.
                    return {"state": status["state"], "accepted": False, "written": 0,
                            "written_chars": 0, "retryable": True,
                            "reason": "stdin channel is not ready yet; the supervisor has not attached its reader — retry"}
                try:
                    while written < len(raw):
                        chunk = raw[written:written + STDIN_WRITE_CHUNK]
                        complete = len(chunk) - _utf8_incomplete_tail(chunk)
                        if complete <= 0:
                            break  # unreachable for encoded UTF-8; never spin
                        try:
                            written += os.write(fd, chunk[:complete])
                        except BlockingIOError:
                            break
                finally:
                    os.close(fd)
            buffer_full = written < len(raw)
            eof_requested = bool(request.get("eof"))
            # EOF applies only once every byte of this operation has been
            # accepted into the FIFO. A partial write defers EOF explicitly so
            # the exact unwritten remainder can be retried with eof=true;
            # closing early would make that remainder unsendable.
            eof_applied = eof_requested and not buffer_full
            if eof_applied:
                atomic_json(directory / "stdin-eof.json", {"at": time.time()})
            return {"state": status["state"], "accepted": True, "written": written,
                    "written_chars": len(raw[:written].decode("utf-8")),
                    "eof": eof_applied, "eof_deferred": eof_requested and buffer_full,
                    "stdin_buffer_full": buffer_full}
        elif action != "status":
            raise ValueError("unsupported job action")
        if action == "status" and request.get("housekeeping"):
            maintenance = _bounded_housekeeping(root, identifier)
        return attach_maintenance_warning(job_status(directory), maintenance)

def run_supervisor(directory):
    """Retain an internal failure and drain children before publishing it."""
    try:
        worker(directory)
    except BaseException as exc:
        import traceback
        detail = {"type": type(exc).__name__, "message": str(exc)[-4000:],
                  "log": str(directory / "supervisor.log")}
        sys.stderr.write(traceback.format_exc()[-8000:])
        sys.stderr.flush()
        try:
            atomic_json(directory / "supervisor-error.json", detail)
            receipt = read_json(directory / "receipt.json")
            deadline = time.monotonic() + 5
            drained = False
            if receipt is not None:
                while True:
                    processes, unknown = owned_processes(receipt)
                    children = [row for row in processes if row["pid"] != os.getpid()]
                    if not children and not unknown:
                        drained = True
                        break
                    signal_processes(children, signal.SIGKILL)
                    with contextlib.suppress(ChildProcessError):
                        while os.waitpid(-1, os.WNOHANG)[0]:
                            pass
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(.02)
            if drained:
                failure = {"state": "failed", "reason": "supervisor failed",
                           "descendants_drained": True, "supervisor_error": detail}
                log_accounting = read_json(directory / "logs.json")
                if log_accounting:
                    failure["logs"] = log_accounting
                atomic_json(directory / "result.json", failure)
        except BaseException:
            # The original exception is already durable in supervisor.log.
            # Append cleanup/record failure without replacing the primary.
            sys.stderr.write("Supervisor cleanup/recording also failed:\n" + traceback.format_exc()[-8000:])
            sys.stderr.flush()
        raise SystemExit(1)

if __name__ == "__main__" and "CappedLog" not in globals():
    # Direct `python worker.py --worker <dir>` debugging: load the companions
    # that worker_source() normally concatenates ahead of this module.
    for _companion in ("worker_runtime.py", "worker_gc.py"):
        _runtime = Path(__file__).with_name(_companion)
        exec(compile(_runtime.read_text(encoding="utf-8"), str(_runtime), "exec"), globals())

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--worker", "supervise"):
        run_supervisor(Path(sys.argv[2]))
    else:
        worker_source = globals().get("WORKER_SOURCE")
        if worker_source is None:
            raise SystemExit(
                "WORKER_SOURCE is undefined: invoke via remote_dev.processes.control"
            )
        print(json.dumps(control_job(json.loads(sys.argv[1]), worker_source)))
