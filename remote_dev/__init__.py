"""[Role] Public package version for the remote-dev control plane.
[Invariants] Prefer installed distribution metadata and fall back to the release version when running from a source tree.
[Timeouts & Locks] No I/O waits or locks; metadata lookup is local and bounded.
[Side-Effect Policy] Side-effect free.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    try:
        return version("remote-dev")
    except PackageNotFoundError:
        return "0.11.15"


__version__ = package_version()

__all__ = ["__version__", "package_version"]
