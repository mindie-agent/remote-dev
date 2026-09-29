"""Atomic local publication, including transient Windows file sharing locks."""
from __future__ import annotations

import errno
import os
import time


def replace_file(source, destination):
    # A scanner/indexer may briefly deny delete sharing after a file read.
    # Retrying the same prepared replacement is safe; never delete the target.
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def publish_file(source, destination, *, overwrite=False):
    """Publish a prepared file without replacing a concurrent writer by default."""
    if overwrite:
        replace_file(source, destination)
        return
    # The temporary file is in destination.parent, so a hard link is on the
    # same filesystem. Unlike os.rename on POSIX, link fails if another writer
    # created the destination after the caller's preflight check.
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise FileExistsError(errno.EEXIST, "local artifact already exists", os.fspath(destination)) from exc
    os.unlink(source)
