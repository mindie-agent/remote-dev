"""[Role] Generic remote process supervision package boundary.
[Invariants] Public boundary is `control`; ordinary jobs and coordinator executions share the same implementation, and the Linux worker is package data.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free package marker.
"""

from .client import control, worker_source

__all__ = ["control", "worker_source"]
