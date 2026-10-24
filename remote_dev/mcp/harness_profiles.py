"""Trusted profile catalog: re-export core primitives and host native descriptors."""
from __future__ import annotations

from .native_spec import (
    CANONICAL_PROFILE, HarnessProfileError, NATIVE_PROFILES, PROFILE_ENV, PROFILES,
    NativeToolSpec, native_input_schema, resolve_profile, validate_native_arguments,
    wire_name,
)
from .profiles_codex import SPECS as CODEX_SPECS
from .profiles_claude import SPECS as CLAUDE_SPECS
from .profiles_kimi import SPECS as KIMI_SPECS
from .profiles_dsh import SPECS as DSH_SPECS

NATIVE_TOOLS: dict[str, dict[str, NativeToolSpec]] = {
    "codex": CODEX_SPECS,
    "claude": CLAUDE_SPECS,
    "kimi": KIMI_SPECS,
    "dsh": DSH_SPECS,
}


def specs_for(profile: str) -> dict[str, NativeToolSpec]:
    return NATIVE_TOOLS.get(profile, {})


def spec_by_wire(profile: str, name: str) -> NativeToolSpec | None:
    for spec in specs_for(profile).values():
        if spec.wire == name:
            return spec
    return None


def spec_by_wire_any_profile(name: str) -> tuple[str, NativeToolSpec] | None:
    for profile in NATIVE_PROFILES:
        spec = spec_by_wire(profile, name)
        if spec is not None:
            return profile, spec
    return None
