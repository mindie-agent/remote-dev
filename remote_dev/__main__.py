"""[Role] Module entry point for `python -m remote_dev`.
[Invariants] Delegates every argument to the shared CLI parser and exit-code contract.
[Timeouts & Locks] No local waits or locks; the CLI owns deadlines.
[Side-Effect Policy] Delegates only to the selected CLI command.
"""
from __future__ import annotations

from remote_dev.cli import main

raise SystemExit(main())
