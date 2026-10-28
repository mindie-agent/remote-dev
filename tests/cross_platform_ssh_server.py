"""[Role] Disposable Linux OpenSSH service for native Windows and actual WSL acceptance.
[Invariants] A random test account, host key and two listeners belong to one state directory.
[Timeouts & Locks] Every utility and listener readiness wait is bounded; CI owns one instance.
[Side-Effect Policy] Creates/deletes only its test user and state; never edits global sshd config.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import signal
import socket
import subprocess
import time
import uuid


def run(argv, *, data=None):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): " +
                           result.stderr.decode("utf-8", "replace"))
    return result.stdout


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def save(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)


def start(root):
    if os.geteuid() != 0:
        raise RuntimeError("the disposable sshd fixture needs root inside its test Linux environment")
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    user = "rdauth" + uuid.uuid4().hex[:10]
    home = root / "test-user"
    # Deliberately UTF-8 and shell metacharacters; supplied through stdin, never argv.
    password = "ssh-\u5bc6\u7801-\u00e9-" + secrets.token_urlsafe(20) + " $'\""
    state = {"user": user, "home": str(home), "root": str(root),
             "work": str(home / "work with spaces"), "password": password,
             "key_port": free_port(), "password_port": free_port(), "pids": []}
    save(root / "manifest.json", state)
    try:
        run(["useradd", "--create-home", "--home-dir", str(home),
             "--shell", "/bin/bash", user])
        run(["chpasswd"], data=(user + ":" + password + "\n").encode("utf-8"))
        account = pwd.getpwnam(user)
        work = Path(state["work"])
        work.mkdir(mode=0o700)
        os.chown(work, account.pw_uid, account.pw_gid)
        host_key = root / "host_key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)])
        Path("/run/sshd").mkdir(exist_ok=True)
        for method, keys in (("key", "yes"), ("password", "no")):
            port = state[method + "_port"]
            config = root / (method + ".conf")
            config.write_text(
                f"Port {port}\nListenAddress 0.0.0.0\nHostKey {host_key}\n"
                f"PidFile {root / (method + '.pid')}\nAllowUsers {user}\n"
                "AuthorizedKeysFile .ssh/authorized_keys\n"
                f"PubkeyAuthentication {keys}\nPasswordAuthentication yes\n"
                "KbdInteractiveAuthentication no\nUsePAM no\nStrictModes yes\n"
                "AllowTcpForwarding yes\nLogLevel VERBOSE\n",
                encoding="utf-8",
            )
            run(["/usr/sbin/sshd", "-t", "-f", str(config)])
            with (root / (method + ".log")).open("wb") as log:
                process = subprocess.Popen(
                    ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            state["pids"].append(process.pid)
            (root / "manifest.json").write_text(json.dumps(state), encoding="utf-8")
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise RuntimeError((root / (method + ".log")).read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("disposable sshd failed to listen")
                    time.sleep(.05)
        return {"status": "ready", "user": user, "ports": [state["key_port"], state["password_port"]]}
    except BaseException:
        stop(root)
        raise


def stop(root):
    if not (root / "manifest.json").exists():
        return {"status": "absent"}
    state = json.loads((root / "manifest.json").read_text())
    for pid in state["pids"]:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        pwd.getpwnam(state["user"])
    except KeyError:
        pass
    else:
        # All sessions in this account are disposable acceptance processes.
        killed = subprocess.run(["pkill", "-u", state["user"]], capture_output=True, timeout=5)
        if killed.returncode not in (0, 1):
            raise RuntimeError("could not stop disposable SSH sessions")
        run(["userdel", state["user"]])
    shutil.rmtree(root)
    return {"status": "removed"}


def operate(root, action):
    state = json.loads((root / "manifest.json").read_text())
    keys = Path(state["home"]) / ".ssh" / "authorized_keys"
    counter = Path(state["work"]) / "recovery-count"
    if action in ("reset", "revoke"):
        keys.unlink(missing_ok=True)
        counter.unlink(missing_ok=True)
    return {
        "authorized_keys": keys.read_text().splitlines() if keys.exists() else [],
        "counter": counter.read_text() if counter.exists() else "",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "reset", "revoke", "inspect"))
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    result = (start(args.state) if args.action == "start" else stop(args.state)
              if args.action == "stop" else operate(args.state, args.action))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
