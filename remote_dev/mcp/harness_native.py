"""Compatibility entry point for native tool dispatch."""
from .native_handlers import *  # noqa: F401,F403
from .native_runtime import dispatch_native
from .native_common import *  # noqa: F401,F403
