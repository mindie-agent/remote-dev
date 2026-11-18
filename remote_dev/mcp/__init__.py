"""[Role] MCP server and tool-surface package boundary.
[Invariants] Submodules stay importable without side effects; the server entry point owns stdio.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free package marker.
"""
