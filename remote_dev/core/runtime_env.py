"""
[Role] Optional remote runtime environment preamble (`runtime_env.py`).
[Invariants]
- Emits no preamble unless `runtime_env_file` is configured and enabled.
- Sources the configured file in the current Bash so functions, variables, and shell options survive; a missing or failed script aborts command execution.
[Timeouts & Locks]
- Pure argv/script construction; no locks and no process launch.
[Side-Effect Policy]
- Side-effect-free string construction; the caller executes the returned lines remotely.
"""

from __future__ import annotations

import shlex

from .endpoint import Endpoint


def runtime_env_lines(endpoint: Endpoint, enabled: bool) -> list[str]:
    if not enabled or not endpoint.runtime_env_file:
        return []
    path = shlex.quote(endpoint.runtime_env_file)
    # Do not source inside `if`/`||`: Bash would suppress errexit throughout the
    # script. Do not alter nounset or start a child Bash that loses functions.
    return [f". {path}", "__remote_dev_runtime_status=$?",
            'if [ "$__remote_dev_runtime_status" -ne 0 ]; then exit "$__remote_dev_runtime_status"; fi',
            "unset __remote_dev_runtime_status"]
