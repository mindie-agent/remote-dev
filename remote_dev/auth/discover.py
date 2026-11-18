"""One bounded read-only SSH host discovery for onboarding receipts.

[Role]
After verified SSH setup (or on status), collect host OS, physical Ascend NPU
presence and running Docker containers through a single read-only SSH command.
[Invariants]
- Runs at most one command with `min(10000, remaining_ms)` timeout under a
  verified `AuthPlan` prepared inside `read_only()`.
- Discovery is receipt metadata only: callers keep `outcome="success"` when the
  primary authentication operation already completed.
[Timeouts & Locks]
One bounded SSH capture; authentication profile repair is disabled by the
read-only policy and no local mutation locks are taken.
[Side-Effect Policy]
Read-only remote commands (`uname`, `npu-smi info -l`, `docker ps`) only.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Mapping

from remote_dev.auth.access import prepare_auth
from remote_dev.auth.policy import read_only
from remote_dev.core.ssh_transport import SshLaunch, _ssh_cmd, capture_launch

MAX_DISCOVERY_MS = 10000
_OS_MARKER = "__RD_OS__"
_NPU_MARKER = "__RD_NPU__"
_DOCKER_MARKER = "__RD_DOCKER__"
_NPU_RC = "__NPU_RC:"
_DOCKER_RC = "__DOCKER_RC:"
_NPU_MISSING = "__NPU_MISSING__"
_DOCKER_MISSING = "__DOCKER_MISSING__"
_MISSING = "__RD_MISSING__"
_DISCOVERY_SCRIPT = (
    f"printf '%s\\n' {_OS_MARKER}; uname -srm 2>&1 || true; "
    f"printf '%s\\n' {_NPU_MARKER}; "
    "if command -v npu-smi >/dev/null 2>&1; then npu-smi info -l; "
    "echo \"__NPU_RC:$?\"; "
    f"else echo '{_NPU_MISSING}'; fi; "
    f"printf '%s\\n' {_DOCKER_MARKER}; "
    "if command -v docker >/dev/null 2>&1; then "
    "out=\"$(docker ps --format '{{.Names}}\\t{{.Image}}' 2>&1)\"; rc=$?; "
    "printf '%s\\n' \"$out\" | head -n 32; echo \"__DOCKER_RC:$rc\"; "
    f"else echo '{_DOCKER_MISSING}'; fi"
)
_ASCEND_HINTS = ("ascend", "mindie", "vllm", "cann", "torch_npu")


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line in {_OS_MARKER, _NPU_MARKER, _DOCKER_MARKER}:
            current = line
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {marker: "\n".join(lines).strip() for marker, lines in sections.items()}


def _permission_denied(text: str) -> bool:
    return "permission denied" in text.casefold()


def _rc_and_body(text: str, prefix: str) -> tuple[int | None, str]:
    returncode = None
    kept: list[str] = []
    for line in text.splitlines():
        if line.startswith(prefix):
            try:
                returncode = int(line[len(prefix):].strip())
            except ValueError:
                kept.append(line)
        else:
            kept.append(line)
    return returncode, "\n".join(kept).strip()


def _parse_npu(text: str) -> dict[str, Any]:
    returncode, text = _rc_and_body(text, _NPU_RC)
    if _NPU_MISSING in text or _MISSING in text:
        return {"npu_status": "not_present", "npu_count": 0, "npu_chips": []}
    if returncode is not None and returncode != 0:
        return {"npu_status": "error", "npu_count": 0, "npu_chips": []}
    if returncode is None and not text:
        return {"npu_status": "not_present", "npu_count": 0, "npu_chips": []}
    count_match = re.search(r"Total\s+Count\s*:\s*(\d+)", text, re.IGNORECASE)
    chips = sorted({match.strip() for match in re.findall(r"Chip\s+Name\s*:\s*(.+)", text, re.IGNORECASE)})
    count = int(count_match.group(1)) if count_match else len(chips)
    if returncode == 0:
        return {"npu_status": "ok", "npu_count": count, "npu_chips": chips}
    if _permission_denied(text):
        return {"npu_status": "permission_denied", "npu_count": 0, "npu_chips": []}
    if count == 0 and not chips:
        return {"npu_status": "not_present", "npu_count": 0, "npu_chips": []}
    return {"npu_status": "ok", "npu_count": count, "npu_chips": chips}


def _parse_docker(text: str) -> dict[str, Any]:
    returncode, text = _rc_and_body(text, _DOCKER_RC)
    if _DOCKER_MISSING in text or _MISSING in text:
        return {"docker_status": "not_installed", "containers": []}
    if returncode is not None and returncode != 0:
        return {"docker_status": "unavailable", "containers": []}
    if returncode is None and _permission_denied(text):
        return {"docker_status": "permission_denied", "containers": []}
    if not text.strip():
        return {"docker_status": "ok", "containers": []}
    containers: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            identifier, name, image, status = (part.strip() for part in parts[:4])
        elif len(parts) >= 2:
            identifier, status = "", ""
            name, image = (part.strip() for part in parts[:2])
        else:
            continue
        haystack = f"{name} {image}".casefold()
        containers.append({
            "id": identifier,
            "name": name,
            "image": image,
            "status": status,
            "is_ascend_candidate": any(hint in haystack for hint in _ASCEND_HINTS),
        })
    return {"docker_status": "ok", "containers": containers}


def parse_discovery(text: str) -> dict[str, Any]:
    """Parse the fixed marker-delimited output without trusting extra prose."""
    sections = _sections(text)
    if _OS_MARKER not in sections:
        return {"status": "unavailable", "reason": "no discovery output"}
    os_text = sections.get(_OS_MARKER, "")
    npu = _parse_npu(sections.get(_NPU_MARKER, ""))
    docker = _parse_docker(sections.get(_DOCKER_MARKER, ""))
    if _DOCKER_MARKER not in sections:
        docker = {"docker_status": "not_installed", "containers": []}
    missing = [marker for marker in (_NPU_MARKER, _DOCKER_MARKER) if marker not in sections]
    status = "partial" if missing else "ok"
    result: dict[str, Any] = {
        "status": status,
        "os": os_text.splitlines()[0] if os_text else "",
        **npu,
        **docker,
    }
    if missing:
        result["missing_sections"] = [marker.strip("_") for marker in missing]
    return result


def _launch(endpoint: Any, *, timeout_ms: int) -> tuple[SshLaunch, int]:
    started = time.monotonic()
    with read_only():
        plan = prepare_auth(endpoint, timeout_ms=timeout_ms)
    remaining = max(1, timeout_ms - int((time.monotonic() - started) * 1000))
    argv = _ssh_cmd(plan.endpoint)
    argv[1:1] = [
        *plan.options,
        "-o", f"ConnectTimeout={max(1, remaining // 1000)}",
        "-o", "ConnectionAttempts=1",
    ]
    environment = None if plan.env is None else {**os.environ, **plan.env}
    return SshLaunch([*argv, _DISCOVERY_SCRIPT], environment, plan.identity), remaining


def discover_endpoint(endpoint: Any, *, timeout_ms: int | None = None) -> Mapping[str, Any]:
    """Return discovery facts, or an `unavailable` receipt; never raise for I/O."""
    budget = (MAX_DISCOVERY_MS if timeout_ms is None
              else max(1, min(MAX_DISCOVERY_MS, int(timeout_ms))))
    try:
        launch, remaining = _launch(endpoint, timeout_ms=budget)
    except Exception as exc:  # noqa: BLE001 - discovery is receipt metadata
        return {"status": "unavailable", "reason": "authentication_plan",
                "error_type": type(exc).__name__}
    try:
        completed = capture_launch(launch, timeout_ms=remaining)
    except Exception as exc:  # noqa: BLE001 - discovery is receipt metadata
        return {"status": "unavailable", "reason": "ssh_capture",
                "error_type": type(exc).__name__}
    if completed.timed_out:
        return {"status": "unavailable", "reason": "timeout"}
    if completed.cancelled:
        return {"status": "unavailable", "reason": "cancelled"}
    stdout = completed.stdout.decode("utf-8", "replace")
    parsed = dict(parse_discovery(stdout))
    if completed.returncode not in (0, None) and parsed.get("status") == "unavailable":
        parsed["returncode"] = completed.returncode
    return parsed
