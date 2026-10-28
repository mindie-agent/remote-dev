"""Remote development substrate for SSH-backed agent tools."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    try:
        return version("remote-dev")
    except PackageNotFoundError:
        return "0.11.1"


__version__ = package_version()

__all__ = ["__version__", "package_version"]
