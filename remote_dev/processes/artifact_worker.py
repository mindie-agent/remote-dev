"""One SSH stream, many hash-checked files, bounded memory on either side."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

CHUNK_SIZE = 1024 * 1024

# REMOTE_DEV_MUTATION_LOCK


def send(value):
    sys.stdout.buffer.write((json.dumps(value) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def receive():
    line = sys.stdin.buffer.readline(1024 * 1024)
    if not line.endswith(b"\n"):
        raise ValueError("incomplete artifact request")
    return json.loads(line)


def checked_path(root, raw, *, writing=False):
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("artifact paths must be absolute")
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("artifact path escapes root")
    # Do not write through even an in-root symlink or change its referent.
    probe = path
    while probe != root and probe != probe.parent:
        if probe.is_symlink():
            raise ValueError("artifact symlinks are not allowed")
        probe = probe.parent
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("artifact is not a regular file")
    if writing:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def transfer(source, destination, size):
    digest = hashlib.sha256()
    while size:
        chunk = source.read(min(size, CHUNK_SIZE))
        if not chunk:
            raise EOFError("artifact stream ended before the declared size")
        destination.write(chunk)
        digest.update(chunk)
        size -= len(chunk)
    return digest.hexdigest()


def main():
    request = receive()
    # SSH owns connection liveness (ServerAlive/SIGHUP/EOF). A healthy long
    # transfer has no deadline. Only an explicitly supplied execution limit
    # arms the worker's timer.
    if os.name == "posix":
        import signal
        def interrupted(signum, _frame):
            raise TimeoutError("artifact transfer interrupted or timed out")
        signal.signal(signal.SIGALRM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        timeout = request.get("timeout_ms")
        if timeout is not None:
            if type(timeout) is not int or timeout <= 0:
                raise ValueError("timeout_ms must be a positive integer")
            signal.setitimer(signal.ITIMER_REAL, timeout / 1000)
    root = Path(request["root"]).resolve(strict=True)
    operation = request["operation"]
    if operation not in {"pull", "push"}:
        raise ValueError("unsupported artifact operation")
    count = int(request["count"])
    if not 0 <= count <= 100000:
        raise ValueError("invalid artifact count")
    for _ in range(count):
        item = receive()
        size = int(item["size"])
        if size < 0:
            raise ValueError("invalid artifact size")
        path = checked_path(root, item["path"], writing=operation == "push")
        if operation == "pull":
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise ValueError("artifact changed since manifest")
                send({"status": "ready", "size": size})
                digest = transfer(stream, sys.stdout.buffer, size)
            send({"status": "ok" if digest == item["sha256"] else "hash_mismatch", "sha256": digest})
        else:
            fd, temporary = tempfile.mkstemp(prefix=".remote-dev-", dir=str(path.parent))
            try:
                send({"status": "ready", "size": size})
                with os.fdopen(fd, "wb") as stream:
                    digest = transfer(sys.stdin.buffer, stream, size)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest != item["sha256"]:
                    send({"status": "hash_mismatch", "sha256": digest})
                    return
                with remote_mutation_lock():
                    checked_path(root, str(path), writing=True)
                    os.replace(temporary, path)
                send({"status": "ok", "sha256": digest})
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # After a mid-file failure the receiver sees a short stream/hash error;
        # stderr remains bounded and the existing destination stays untouched.
        print(str(exc)[:4000], file=sys.stderr)
        raise SystemExit(1)
