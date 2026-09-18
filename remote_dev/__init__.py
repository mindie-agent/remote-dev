"""Remote development substrate for SSH-backed agent tools."""

from __future__ import annotations

from importlib.metadata import version


def package_version() -> str:
    return version("remote-dev")


__version__ = package_version()

__all__ = ["__version__", "package_version"]
