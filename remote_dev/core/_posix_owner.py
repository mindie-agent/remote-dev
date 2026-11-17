"""Private POSIX argv supervisor; caller or wrapper pipe EOF ends the owned group.

[Role] Isolated POSIX process-group guardian (`_posix_owner.py`).
[Invariants]
- Run as an isolated interpreter, never imported into a threaded caller.
- Forks the guardian before the target starts and stays in the same process group until
  caller pipe EOF or wrapper process exit (observed via kernel-closed lifeline pipe `parent_w`).
- Never closes `parent_w` in userspace so `parent_r` sees EOF strictly when the kernel tears down
  the wrapper process (`group`), preserving the wrapper's exact exit code or signal status.
- A configured cgroup is joined before either fork or target launch; descendants inherit memory.max.
[Timeouts & Locks]
- Event-driven wait via `selectors.DefaultSelector` on `[owner_pipe, parent_r]` with zero
  steady-state polling; escalates owned process group (`-group`) to `SIGKILL` on EOF.
[Side-Effect Policy]
- Signals only its owned process group (`-group`) and closes inherited file descriptors.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


def main() -> int:
    owner_pipe, notification = int(sys.argv[1]), int(sys.argv[2])
    ownership = json.loads(sys.argv[3])
    inherited = tuple(ownership["pass_fds"] if isinstance(ownership, dict) else ownership)
    memory_cgroup = ownership.get("memory_cgroup") if isinstance(ownership, dict) else None
    command = sys.argv[4:]
    group = os.getpid()
    if not command or os.getpgrp() != group:
        return 125
    if memory_cgroup:
        try:
            with (Path(memory_cgroup) / "cgroup.procs").open("w") as members:
                members.write(str(group))
        except TimeoutError:
            raise
        except OSError as exc:
            os.write(notification, json.dumps({"started": False, "memory_error": str(exc)}).encode())
            os.close(notification)
            return 125
    # Kernel-closed lifeline pipe: `parent_w` stays open in `group` until the OS kernel
    # closes descriptors during process exit, so normal return codes are never clobbered.
    parent_r, parent_w = os.pipe()
    os.set_inheritable(parent_r, False)
    os.set_inheritable(parent_w, False)
    if os.fork() == 0:
        os.close(notification)
        os.close(parent_w)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        for descriptor in (0, 1, 2):
            if descriptor not in (owner_pipe, parent_r):
                try:
                    os.close(descriptor)
                except TimeoutError:
                    raise
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
        owner_eof = False
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(owner_pipe, selectors.EVENT_READ)
                sel.register(parent_r, selectors.EVENT_READ)
                while os.getppid() == group:
                    events = sel.select(timeout=5.0)
                    eof = False
                    for key, _mask in events:
                        try:
                            if not os.read(key.fd, 1):
                                eof = True
                                if key.fd == owner_pipe:
                                    owner_eof = True
                                break
                        except OSError:
                            eof = True
                            if key.fd == owner_pipe:
                                owner_eof = True
                            break
                    if eof:
                        break
            if not owner_eof:
                # `parent_r` hit EOF inside `group`'s kernel exit path (`fdfree`). Let `group`
                # finish transitioning to zombie before signalling `-group` so XNU/Linux never
                # replaces `group`'s committed returncode with SIGKILL.
                deadline = time.monotonic() + 0.05
                while os.getppid() == group and time.monotonic() < deadline:
                    time.sleep(0.001)
        finally:
            with contextlib.suppress(OSError):
                os.close(parent_r)
            os.killpg(group, signal.SIGKILL)
        os._exit(125)
    os.close(owner_pipe)
    os.close(parent_r)
    try:
        child = subprocess.Popen(command, pass_fds=inherited)
    except TimeoutError:
        raise
    except OSError as exc:
        os.write(notification, json.dumps({"started": False, "errno": exc.errno}).encode("ascii"))
        os.close(notification)
        return 127
    receipt = {"started": True}
    if memory_cgroup:
        receipt["memory_cgroup"] = memory_cgroup
    os.write(notification, json.dumps(receipt).encode())
    os.close(notification)
    child.wait()
    if child.returncode < 0:
        if -child.returncode not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-child.returncode, signal.SIG_DFL)
        os.kill(os.getpid(), -child.returncode)
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
