"""
[Role] Example consumer resolver plugin mapping a lab name to an endpoint (`resolver_plugin.py`).
[Invariants]
- Resolver callbacks return an `Endpoint` or fail explicitly; a missing lab file is a configuration error, never an implicit host default.
- Consumer knowledge stays in this plugin; remote-dev only invokes the registered resolver.
[Timeouts & Locks]
- In-memory JSON lookup with no locks; callers own remote connection deadlines.
[Side-Effect Policy]
- Read-only local file lookup; no network calls, cache writes, or remote execution.

Load it into the MCP server (or any CLI wrapper) with:

    REMOTE_DEV_RESOLVERS=/absolute/path/to/examples/resolver_plugin.py:setup

Replace the tiny JSON lookup with a machine inventory, session registry, or
coordinator API; remote-dev only calls back into the registered resolver.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from remote_dev.core.endpoint import EndpointError, register_resolver, resolver_setup

LAB_FILE_ENV = "EXAMPLE_LAB_FILE"


def _labs() -> dict:
    path = os.environ.get(LAB_FILE_ENV)
    if not path:
        return {}
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def resolve_lab(payload: dict):
    """Return an endpoint payload for ``lab``; decline anything else."""
    lab = payload.get("lab")
    if not lab:
        return None  # not ours: let remote-dev try the next resolver
    labs = _labs()
    if lab not in labs:
        raise EndpointError(f"unknown lab {lab!r}; configured labs: {sorted(labs) or 'none'}")
    entry = labs[lab]
    return {
        "host": entry["host"],
        "port": int(entry["port"]),
        "user": entry.get("user", "root"),
        "cwd": entry.get("cwd"),
        "runtime_env_file": entry.get("runtime_env_file"),
        "kind": "example-lab",
        "source": {"lab": lab},
    }


@resolver_setup
def setup() -> None:
    register_resolver(resolve_lab, name="example-labs", fields=("lab",), description="Resolve `lab` names from EXAMPLE_LAB_FILE.")
