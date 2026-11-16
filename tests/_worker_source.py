"""Load the production composed worker source as an importable module.

`remote_dev.processes.worker_source()` ships `worker_runtime.py` + `worker_gc.py` + `worker.py` as one
self-contained script to the remote host. Local tests must exercise that exact
composition, not a bare `worker.py` import that lacks the runtime companion.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from types import ModuleType


@lru_cache(maxsize=1)
def load_worker_module() -> ModuleType:
    os.environ.setdefault("REMOTE_DEV_STOP_GRACE_SECONDS", "0.1")
    from remote_dev.processes import worker_source

    module = ModuleType("remote_dev_process_worker")
    module.__file__ = str(
        Path(__file__).resolve().parents[1] / "remote_dev" / "processes" / "worker.py"
    )
    namespace = module.__dict__
    namespace["__name__"] = "remote_dev_process_worker"
    exec(compile(worker_source(), "<remote-dev-composed-worker>", "exec"), namespace)
    return module
