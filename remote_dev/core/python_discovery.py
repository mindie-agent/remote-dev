"""Shared source-backed rendering for interpreter metadata discovery.

[Role] Supply one inventory worker to remote probes and coordinator preparation.
[Invariants] Control Python is only a launcher; candidates are never installed.
[Timeouts & Locks] The worker shares the supplied deadline; no locks or I/O
apart from reading the packaged worker sources.
[Side-Effect Policy] Returns a shell command; execution belongs to the caller.
"""
from functools import lru_cache
import base64
import json
from pathlib import Path
import shlex
import zlib


@lru_cache(maxsize=2)
def worker_source(name):
    return (Path(__file__).parents[1] / "processes" / name).read_text(encoding="utf-8")


def inventory_command(payload, *, compact=True):
    request = {**payload, "probe_source": worker_source("probe.py")}
    if not compact:
        return shlex.join(["python3", "-I", "-c", worker_source("python_inventory.py"), json.dumps(request)])
    # SSH command arguments are limited on native Windows. Send maintained
    # worker sources as one compressed envelope rather than expanding nested
    # Python and JSON quoting into the remote command line.
    packet = json.dumps({"source": worker_source("python_inventory.py"), "request": request})
    encoded = base64.b64encode(zlib.compress(packet.encode("utf-8"), 9)).decode("ascii")
    bootstrap = (
        "import base64,json,sys,zlib;"
        "packet=json.loads(zlib.decompress(base64.b64decode(sys.argv[1])));"
        "sys.argv=[sys.argv[0],json.dumps(packet['request']),*sys.argv[2:]];"
        "exec(compile(packet['source'],'<python-inventory>','exec'))"
    )
    return shlex.join(["python3", "-I", "-c", bootstrap, encoded])


def selection_command(required_distributions, *, timeout_ms=20000, compact=True):
    return inventory_command({"root": "/", "cwd": "/", "output": "path",
                              "required_distributions": required_distributions,
                              "timeout_ms": timeout_ms}, compact=compact)


def compact_shell_script(script):
    """Carry a maintained script below SSH command-line limits, preserving argv."""
    encoded = base64.b85encode(zlib.compress(script.encode("utf-8"), 9)).decode("ascii")
    bootstrap = (
        "import base64,os,sys,zlib;"
        "script=zlib.decompress(base64.b85decode(sys.argv[1])).decode('utf-8');"
        "os.execvp('bash',['bash','-c',script,'--',*sys.argv[2:]])"
    )
    return "exec " + shlex.join(["python3", "-I", "-c", bootstrap, encoded]) + ' "$@"\n'
