"""Receipt-preserving post-setup onboarding enrichment.

[Role]
Attach non-secret endpoint persistence and bounded host discovery to a verified
`remote.auth` result without changing the primary authentication outcome.
[Invariants]
- A completed SSH setup stays `success` even when alias persistence or
  discovery fails; failures are explicit receipt metadata, never exceptions.
- Every independent warning is preserved in `recording_warnings`; a failed
  `profile_save` never becomes a claimed saved credential profile.
- Passwords, credential references and SSH stderr never enter the receipt.
[Timeouts & Locks]
Endpoint persistence takes the alias-file `record_lock`; discovery owns one
bounded read-only SSH capture. Both consume one monotonic deadline derived by
the caller and are skipped unless explicitly requested.
[Side-Effect Policy]
Writes only the selected endpoint alias file and runs read-only discovery.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .discover import discover_endpoint
from .endpoint_store import save_endpoint_record

DISCOVERY_MIN_BUDGET_SECONDS = 0.5


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _remaining_milliseconds(deadline: float | None) -> int | None:
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return None
    return max(1, int(remaining * 1000))


def _record_warning(facts: dict[str, Any], warning: Mapping[str, Any]) -> None:
    """Keep the primary and every secondary warning instead of overwriting."""
    warnings = facts.get("recording_warnings")
    if not isinstance(warnings, list):
        warnings = []
    existing = facts.get("receipt_warning")
    if isinstance(existing, dict) and existing not in warnings:
        warnings.append(dict(existing))
    item = dict(warning)
    if item not in warnings:
        warnings.append(item)
    facts["recording_warnings"] = warnings
    if not isinstance(facts.get("receipt_warning"), dict):
        facts["receipt_warning"] = item


def finalize_auth_result(endpoint: Any, facts: dict[str, Any], *, action: str,
                         save_default: bool = False, save_alias: str | None = None,
                         discover: bool = False,
                         timeout_ms: int | None = None,
                         deadline: float | None = None) -> dict[str, Any]:
    """Mutate and return the public facts object with honest receipts."""
    if deadline is None and timeout_ms is not None:
        deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
    existing_warning = facts.get("receipt_warning")
    if isinstance(existing_warning, dict):
        facts["recording_warnings"] = [dict(existing_warning)]
    if action == "setup" and (save_default or (save_alias or "").strip()):
        try:
            facts["endpoint_save"] = save_endpoint_record(
                endpoint, facts, save_default=save_default, save_alias=save_alias,
                timeout_seconds=_remaining_seconds(deadline),
            )
        except Exception as exc:  # noqa: BLE001 - the SSH setup already completed
            facts["endpoint_save"] = {
                "saved": False,
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
            facts["receipt_unconfirmed"] = True
            _record_warning(facts, {"stage": "endpoint_save",
                                    "error_type": type(exc).__name__})
    if discover:
        remaining = _remaining_seconds(deadline)
        if remaining is not None and remaining < DISCOVERY_MIN_BUDGET_SECONDS:
            facts["discovery"] = {"status": "skipped_budget_exhausted"}
            return facts
        try:
            facts["discovery"] = dict(discover_endpoint(
                endpoint, timeout_ms=_remaining_milliseconds(deadline),
            ))
        except TimeoutError:
            facts["discovery"] = {"status": "unavailable", "reason": "timeout"}
        except Exception as exc:  # noqa: BLE001 - discovery is receipt metadata
            facts["discovery"] = {
                "status": "unavailable",
                "reason": "internal",
                "error_type": type(exc).__name__,
            }
    return facts
