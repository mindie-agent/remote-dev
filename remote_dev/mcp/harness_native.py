"""[Role] Compatibility import surface for native dispatch.
[Invariants] Imports host handler modules exactly once and re-exports the dispatch registry.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free import aggregation.
"""
from .native_handlers import *  # noqa: F401,F403
from .native_runtime import dispatch_native
from .native_common import *  # noqa: F401,F403
