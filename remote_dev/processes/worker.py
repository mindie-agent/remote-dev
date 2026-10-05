"""Linux remote job supervisor shipped through the process-control transport.

This module has no package dependencies. Preparing a job starts only a
waiting supervisor. User code cannot run until the caller opens the start
gate. Remote target is Linux; this file is not a local Windows worker.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import pty
import re
import select
import signal
import subprocess
import struct
import sys
import termios
import time
import uuid
from pathlib import Path

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,95}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
JOB_TOKEN_ENV = "REMOTE_DEV_JOB_TOKEN"
JOB_ENV_PREFIX = "REMOTE_DEV_JOB_"
JOBS_DIRNAME = ".remote-dev"
# In-memory cap for stdin bytes accepted from the FIFO but not yet consumed by
# the child pipe. Past the cap the worker stops draining the FIFO so a writer
# blocks (or a nonblocking writer gets EAGAIN) instead of growing worker RAM.
STDIN_BUFFER_CAP = 262144
# A nonblocking pipe write of at most PIPE_BUF bytes is all-or-nothing
# (POSIX), which keeps every accepted stdin prefix on an exact byte count the
# caller can map back to characters.
STDIN_WRITE_CHUNK = getattr(select, "PIPE_BUF", 512)
# States in which a job may still append to its logs.
LIVE_JOB_STATES = frozenset({"prepared", "running", "uncertain"})


def _utf8_incomplete_tail(chunk):
    """Bytes at the end of chunk that start but do not finish a UTF-8 sequence (0-3).

    Only genuinely incomplete prefixes count; bytes that cannot extend into a
    valid sequence return 0 so they flush as U+FFFD instead of stalling a read
    cursor or a stdin write forever.
    """
    size = len(chunk)
    for back in range(1, min(4, size) + 1):
        byte = chunk[size - back]
        if byte & 0xC0 == 0x80:
            continue  # continuation byte, keep scanning for the lead byte
        if byte < 0x80:
            return 0  # ASCII: the chunk ends on a complete character
        if 0xC2 <= byte <= 0xDF:
            needed = 2
        elif 0xE0 <= byte <= 0xEF:
            needed = 3
        elif 0xF0 <= byte <= 0xF4:
            needed = 4
        else:
            return 0  # invalid lead byte: not an incomplete sequence
        return back if back < needed else 0
    return 0


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "ppid": int(fields[1]), "pgid": int(fields[2]),
                "start_ticks": fields[19], "state": fields[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def supervised_family(receipt):
    """Walk the verified subreaper's kernel child lists, including all threads.

    All job descendants remain below this live anchor, including setsid and
    clean-environment daemons. A racing exit can postpone a signal to the next
    drain pass; it cannot report quiet while the anchor remains alive.
    """
    anchor = process_identity(receipt["pid"])
    if not anchor or anchor["state"] == "Z" or anchor["start_ticks"] != receipt["start_ticks"]:
        return None
    found, pending, unknown = {anchor["pid"]: anchor}, [anchor["pid"]], []
    while pending:
        pid = pending.pop()
        try:
            tasks = Path(f"/proc/{pid}/task")
            for task in tasks.iterdir():
                try:
                    children = (task / "children").read_text().split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                for child_pid in children:
                    row = process_identity(child_pid)
                    if row and row["state"] != "Z" and row["ppid"] == pid and row["pid"] not in found:
                        found[row["pid"]] = row
                        pending.append(row["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            unknown.append(f"cannot observe children of {pid}")
    return list(found.values()), unknown


def owned_processes(receipt):
    """Observe marker ownership and ancestry below the verified subreaper.

    Ancestry also covers setsid/clean-environment daemon children. The worker
    stays alive and adopts orphans until all descendants have been reaped.
    An unowned member of the recorded process group is ambiguous, not free.
    The random marker identifies processes; it is not an access credential.
    """
    if receipt["boot_id"] != boot_id():
        return [], ["boot identity changed"]
    if receipt.get("supervision") == "subreaper":
        family = supervised_family(receipt)
        if family is not None:
            return family
    identities, tagged, unknown = {}, set(), []
    marker = (JOB_TOKEN_ENV + "=" + receipt["marker"]).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        identity = None
        try:
            identity = process_identity(entry.name)
            if not identity or identity["state"] == "Z":
                continue
            identities[identity["pid"]] = identity
            environ = (entry / "environ").read_bytes().split(b"\0")
            if marker in environ:
                tagged.add(identity["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Other users' unrelated processes are not this job's family.
            # Readable ancestry still identifies our descendants without env;
            # unowned members of the claimed group become unknown below.
            if identity is None:
                unknown.append(int(entry.name))
    anchor = identities.get(receipt["pid"])
    if receipt.get("supervision") == "subreaper" and anchor and anchor["start_ticks"] == receipt["start_ticks"]:
        family = {anchor["pid"]}
        while True:
            descendants = {pid for pid, row in identities.items() if row["ppid"] in family}
            if descendants.issubset(family):
                break
            family.update(descendants)
        tagged.update(family)
    unknown.extend(pid for pid, row in identities.items() if row["pgid"] == receipt["pgid"] and pid not in tagged)
    return [identities[pid] for pid in sorted(tagged)], unknown


def job_status(directory):
    receipt = read_json(directory / "receipt.json")
    if receipt is None:
        return {"state": "uncertain", "reason": "launch intent exists without a process receipt", "quiet": False}
    result = read_json(directory / "result.json")
    # A verified subreaper's completion is a stronger fact than another /proc
    # walk. It cannot publish this receipt until all descendants are reaped.
    if (result and result.get("descendants_drained") and
            receipt.get("supervision") == "subreaper" and receipt["boot_id"] == boot_id()):
        anchor = process_identity(receipt["pid"])
        processes = ([anchor] if anchor and anchor["state"] != "Z" and
                     anchor["start_ticks"] == receipt["start_ticks"] else [])
        unknown = []
    else:
        processes, unknown = owned_processes(receipt)
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
    # Interactive sessions: callers write to the on-disk FIFO through the
    # "stdin" control action; this proxy forwards bytes into the child's
    # pipe so stdin EOF stays under worker control (stdin-eof.json). A plain
    # background job keeps DEVNULL stdin. The reader is attached before the
    # readiness marker: prepare only returns after supervisor-ready.json, so a
    # stdin write after prepare/go always finds a reader (opening a FIFO
    # O_WRONLY|O_NONBLOCK without a reader fails with ENXIO).
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
        time.sleep(0.1)
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
    with (directory / "stdout.log").open("ab") as stdout, (directory / "stderr.log").open("ab") as stderr:
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
                                     stdin=pipe_r if interactive else subprocess.DEVNULL, stdout=stdout, stderr=stderr)
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
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break  # last slave closed
                    if not chunk:
                        break
                    stdout.write(chunk)
                    stdout.flush()
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
                force = bool(stop and stop.get("force")) or time.monotonic() >= stopping_at + 2
                signal_processes(processes, signal.SIGKILL if force else signal.SIGTERM, exclude=os.getpid())
            # This kernel list includes zombies and adopted clean-env children.
            # A process cannot fork again after it has been reaped; only this
            # empty-child boundary allows the supervisor to publish completion.
            children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().strip()
            if code is not None and not children:
                break
            time.sleep(0.05)
        if pipe_w is not None:
            os.close(pipe_w)
        if fifo_fd is not None:
            os.close(fifo_fd)
        command_finished = time.monotonic()
        shell_exit_observed = shell_exit_observed if shell_exit_observed is not None else command_finished
        result = {"state": terminal or ("succeeded" if code == 0 else "failed"),
                  "exit_code": code, "descendants_drained": True, "finished_at": time.time(),
                  "timings": {"clock_domain": "remote-supervisor:" + boot_id() + ":" + str(os.getpid()),
                              "spawn_ms": round((spawned_at - command_started) * 1000, 3),
                              "shell_ms": round((shell_exit_observed - spawned_at) * 1000, 3),
                              "descendant_drain_ms": round((command_finished - shell_exit_observed) * 1000, 3)}}
        atomic_json(directory / "result.json", result)


def signal_processes(processes, sig, *, exclude=None):
    for process in processes:
        if process["pid"] == exclude:
            continue
        current = process_identity(process["pid"])
        if current and current["start_ticks"] == process["start_ticks"]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process["pid"], sig)


def cancel_and_drain(request, source):
    status = control_job({**request, "action": "stop"}, source)
    deadline = time.monotonic() + 5
    while not status.get("quiet") and not status.get("unknown") and time.monotonic() < deadline:
        time.sleep(0.02)
        status = control_job({**request, "action": "status"}, source)
    return status


def control_job(request, source, cancel_event=None):
    """One process authority for gated launches and native execute/poll calls.

    launch and exchange combine existing actions on the remote side. They do
    not hold the job mutation lock while waiting, so stop can interrupt them.
    """
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
        if request.get("data") or request.get("eof"):
            reply = control_job({**request, "action": "stdin"}, source)
        root = Path(request["root"]).resolve(strict=True)
        identifier = request["job_id"]
        if not JOB_ID_RE.fullmatch(identifier):
            raise ValueError("invalid job id")
        directory = root / JOBS_DIRNAME / "jobs" / identifier
        if root not in directory.resolve().parents:
            raise ValueError("job directory escapes the runtime root")
        wait_ms = max(0, min(300000, int(request.get("yield_time_ms") or 0)))
        deadline = time.monotonic() + wait_ms / 1000
        while reply.get("accepted") and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                break
            if (directory / "result.json").exists() or not directory.exists():
                break
            available = False
            for stream in ("stdout", "stderr"):
                path = directory / (stream + ".log")
                if path.exists() and path.stat().st_size > int(request.get(stream + "_offset") or 0):
                    available = True
                    break
            # Synchronous SDK capture already waits for completion. Returning
            # on early output only adds a second round trip to observe exit.
            if available and not request.get("wait_for_exit"):
                break
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        if cancel_event is not None and cancel_event.is_set():
            cancel_and_drain(request, source)
            reply["cancellation_requested"] = True
        observation = control_job({**request, "action": "tail"}, source)
        return {**reply, **observation}
    root = Path(request["root"]).resolve(strict=True)
    identifier = request["job_id"]
    if not JOB_ID_RE.fullmatch(identifier):
        raise ValueError("invalid job id")
    directory = root / JOBS_DIRNAME / "jobs" / identifier
    if root not in directory.resolve().parents:
        raise ValueError("job directory escapes the runtime root")
    action = request["action"]
    if not directory.exists() and action != "prepare":
        return {"state": "absent", "quiet": True}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as lock:
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
            script.write_text(source)
            os.chmod(script, 0o600)
            marker = uuid.uuid4().hex
            with (directory / "supervisor.log").open("ab") as supervisor_log:
                process = subprocess.Popen(
                    [sys.executable, str(script), "--worker", str(directory)],
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
                    time.sleep(0.02)
            except BaseException as error:
                # The job mutation lock still excludes go: no command has
                # been activated. Reclaim our exact waiting child on failure,
                # including failure to persist its receipt, without any TTL.
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
                return job_status(directory)
            status = job_status(directory)
            if status["quiet"] or status["unknown"]:
                return status
            processes = status["processes"]
            atomic_json(directory / "stop.json", {"at": time.time(), "force": bool(request.get("force"))})
            # Signal only PIDs whose marker and start ticks were just observed.
            # Do not infer ownership from a PID alone or kill a whole container.
            # Keep the subreaper alive while it drains the family and publishes
            # completion. Killing it first would orphan clean-env descendants.
            signal_processes(processes, signal.SIGKILL if request.get("force") else signal.SIGTERM,
                             exclude=receipt["pid"] if receipt.get("supervision") == "subreaper" else None)
        elif action == "tail":
            lines = min(200, max(1, int(request.get("lines", 60))))
            result = job_status(directory)
            max_bytes = min(32768, max(1, int(request.get("max_bytes") or 32768)))
            shared_budget = bool(request.get("shared_budget"))
            remaining_budget = max_bytes
            live = result.get("state") in LIVE_JOB_STATES
            for name in ("stdout", "stderr"):
                path = directory / (name + ".log")
                if path.exists():
                    offset_key = name + "_offset"
                    if request.get(offset_key) is not None:
                        # Incremental read for interactive sessions: return only
                        # bytes after the caller's cursor, plus the new cursor.
                        # Repeated polls never replay earlier output.
                        start_offset = max(0, int(request[offset_key]))
                        size = path.stat().st_size
                        with path.open("rb") as stream:
                            stream.seek(start_offset)
                            chunk = stream.read(remaining_budget if shared_budget else max_bytes)
                        hold = _utf8_incomplete_tail(chunk)
                        if hold == len(chunk) and not shared_budget:
                            # Progress guarantee: a page must always advance the
                            # cursor, even for budgets smaller than one UTF-8
                            # character; the partial bytes flush as U+FFFD.
                            hold = 0
                        elif not live and start_offset + len(chunk) >= size:
                            # Terminal job at end of file: no later write can
                            # complete the sequence, so flush the tail now.
                            hold = 0
                        if hold:
                            # Hold back a UTF-8 character split by the byte
                            # budget; the next poll re-reads and completes it
                            # instead of corrupting the stream into U+FFFD. The
                            # cursor advances past returned bytes only, so the
                            # held bytes stay counted in bytes_remaining.
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
                        result[offset_key] = start_offset + len(chunk)
                        result[name + "_bytes_remaining"] = max(0, size - result[offset_key])
                    else:
                        with path.open("rb") as stream:
                            stream.seek(max(0, path.stat().st_size - 32000))
                            result[name] = "\n".join(stream.read().decode(errors="replace").splitlines()[-lines:])
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
                # Bounded partial acceptance: every write is at most
                # STDIN_WRITE_CHUNK (<= PIPE_BUF) and ends on a UTF-8 character
                # boundary, so a nonblocking write either lands completely or
                # raises EAGAIN. The accepted prefix maps to an exact character
                # count (written_chars) for retrying the remainder — a byte
                # count cannot slice a Python/JSON string at character level.
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
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
        return job_status(directory)


def run_supervisor(directory):
    """Retain an internal failure and drain children before publishing it.

    The supervisor's own evidence is separate from the command's stdout/stderr.
    Failure to observe/reclaim descendants is uncertainty, never clean exit.
    """
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
                atomic_json(directory / "result.json", {"state": "failed", "reason": "supervisor failed",
                                                       "descendants_drained": True, "supervisor_error": detail})
        except BaseException:
            # The original exception is already durable in supervisor.log.
            # Append cleanup/record failure without replacing the primary.
            sys.stderr.write("Supervisor cleanup/recording also failed:\n" + traceback.format_exc()[-8000:])
            sys.stderr.flush()
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        run_supervisor(Path(sys.argv[2]))
    else:
        worker_source = globals().get("WORKER_SOURCE")
        if worker_source is None:
            raise SystemExit(
                "WORKER_SOURCE is undefined: invoke via remote_dev.processes.control"
            )
        print(json.dumps(control_job(json.loads(sys.argv[1]), worker_source)))
