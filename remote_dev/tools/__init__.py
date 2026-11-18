"""[Role] CLI helper wrappers and the live/local validator package boundary.
[Invariants] Wrapper modules delegate to the shared CLI; the validator owns its own bounded probes.
[Timeouts & Locks] No local waits or locks at import time.
[Side-Effect Policy] Side-effect free package marker.
"""
