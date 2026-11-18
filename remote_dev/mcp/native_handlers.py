"""[Role] Import native host handler modules so their decorators register once.
[Invariants] Import order is fixed; duplicate registration is idempotent and never dispatches work.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free registration imports.
"""
from . import native_handlers_codex  # noqa: F401
from . import native_handlers_claude  # noqa: F401
from . import native_handlers_kimi  # noqa: F401
from . import native_handlers_dsh  # noqa: F401
from . import native_handlers_wb  # noqa: F401
