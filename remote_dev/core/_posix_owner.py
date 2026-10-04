"""Private POSIX argv supervisor; caller pipe EOF ends the owned group.

Run as an isolated interpreter, never imported into a threaded caller. The
guardian is forked here before the target starts and stays in the same group
after the target exits. Detached sessions deliberately have separate owners.
"""
from __future__ import annotations

import errno
import json
import os
import select
import signal
import subprocess
import sys


def main() -> int:
    owner_pipe, notification = int(sys.argv[1]), int(sys.argv[2])
    inherited = tuple(json.loads(sys.argv[3]))
    command = sys.argv[4:]
    group = os.getpid()
    if not command or os.getpgrp() != group:
        return 125
    if os.fork() == 0:
        os.close(notification)
        # A graceful stop must not disarm the guardian before all children
        # drain; caller death during that drain still closes the owner pipe.
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        for descriptor in (0, 1, 2):
            if descriptor != owner_pipe:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
        try:
            # The wrapper is also a real process health boundary. Killing
            # its exposed Popen PID must not leave the SSH target alive, and
            # normal target exit must drain inherited descendants promptly.
            while os.getppid() == group:
                readable, _, _ = select.select([owner_pipe], [], [], .05)
                if readable and not os.read(owner_pipe, 1):
                    break
        finally:
            os.killpg(group, signal.SIGKILL)
        os._exit(125)
    os.close(owner_pipe)
    try:
        child = subprocess.Popen(command, pass_fds=inherited)
    except OSError as exc:
        os.write(notification, json.dumps({"started": False, "errno": exc.errno}).encode("ascii"))
        os.close(notification)
        return 127
    os.write(notification, b'{"started":true}')
    os.close(notification)
    child.wait()
    if child.returncode < 0:
        if -child.returncode not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-child.returncode, signal.SIG_DFL)
        os.kill(os.getpid(), -child.returncode)
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
