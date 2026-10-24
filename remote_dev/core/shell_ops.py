"""
[Role] Entry point for `remote.bash` command execution over the durable job supervisor (`shell_ops.py`).
[Invariants]
- Validates that `cwd` is an absolute path under `endpoint.root` before delegating to `start_remote_job`, returning an actionable `endpoint_patch` hint when blocked.
[Timeouts & Locks]
- Forwards `timeout_ms` and `yield_time_ms` to `start_remote_job`.
[Side-Effect Policy]
- Launches a supervised remote job via `job_ops.start_remote_job`.
"""
from __future__ import annotations

from remote_dev.observability import observed_tool

import shlex
from typing import Any

from .endpoint import Endpoint
from .container_endpoint import pinned_endpoint
from .errors import PathPolicyError
from .job_ops import start_remote_job
from .path_policy import assert_under_root
from remote_dev.result import make_result


def _cwd_outside_root_next(cwd: str) -> dict[str, Any]:
    if not cwd.startswith("/"):
        return {
            "suggested_action": "rerun_with_absolute_cwd",
            "message": "Remote cwd must be absolute and inside the endpoint root.",
        }
    return {
        "suggested_action": "rerun_with_endpoint_root",
        "message": (
            "The requested cwd is outside the endpoint root. If this path is "
            "intentional and trusted, rerun with root set to cwd or one of "
            "its trusted ancestor directories."
        ),
        "endpoint_patch": {"root": cwd, "cwd": cwd},
    }


@observed_tool("remote.bash")
@pinned_endpoint
def remote_bash(
    endpoint: Endpoint,
    *,
    command: str,
    cwd: str | None = None,
    description: str | None = None,
    timeout_ms: int | None = None,
    runtime_env: bool | None = None,
    env: dict[str, str] | None = None,
    yield_time_ms: int | None = None,
    max_output_tokens: int | None = None,
    tty: bool = False,
    wait: bool = False,
    job_id: str | None = None,
) -> dict[str, Any]:
    env = env or {}
    runtime_enabled = endpoint.runtime_env if runtime_env is None else runtime_env
    try:
        cwd = assert_under_root(cwd or endpoint.effective_cwd, endpoint.root)
    except PathPolicyError as exc:
        result = make_result(
            tool="remote.bash",
            target=endpoint.to_result_target(),
            outcome="blocked",
            status="cwd_outside_root",
            summary="RemoteBash blocked because cwd is outside root.",
            preview={"stderr": str(exc)},
            next=_cwd_outside_root_next(cwd or endpoint.effective_cwd),
            extra={"error": str(exc), "command_preview": command[:500]},
        )
        text = (
            result["summary"]
            + "\n"
            + str(exc)
            + "\n"
            + f"Next: rerun with --root {shlex.quote(cwd or endpoint.effective_cwd)} "
            + f"--cwd {shlex.quote(cwd or endpoint.effective_cwd)} if that path is trusted.\n"
        )
        return {"text": text, "result": result}
    return start_remote_job(
        endpoint, command=command, cwd=cwd, env=env, timeout_ms=timeout_ms,
        runtime_env=runtime_enabled, description=description, yield_time_ms=yield_time_ms,
        max_output_tokens=max_output_tokens, tty=tty, wait=wait, job_id=job_id,
    )
