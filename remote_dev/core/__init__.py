"""[Role] Core operations package boundary (endpoint, transport, files, jobs, state).
[Invariants] Submodules stay importable without side effects; no eager network or filesystem work.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free package marker.
"""
