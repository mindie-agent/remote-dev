"""Linux cgroup v2 capacity and owned process-tree memory enforcement.

[Role] Discover the current unified cgroup and own one memory-limited child.
[Invariants] Report enforcement only after memory.max is written and the owner joins;
the target and every descendant inherit that cgroup before executing user code.
[Timeouts & Locks] Read bounded kernel files; removal waits at most the supplied timeout.
[Side-Effect Policy] Create/remove only a unique child of an already delegated cgroup;
never enable controllers or change another process's limits.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import time
import uuid

from .errors import record_cleanup_failure


def _mount_path(value: str) -> str:
    for escaped, character in ((r"\040", " "), (r"\011", "\t"),
                               (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(escaped, character)
    return value


def current_cgroup(*, membership=Path("/proc/self/cgroup"),
                   mounts=Path("/proc/self/mountinfo")) -> tuple[Path, Path] | None:
    """Resolve a cgroup namespace or a mount rooted below the host hierarchy."""
    try:
        groups = membership.read_text().splitlines()
        mount_rows = mounts.read_text().splitlines()
    except FileNotFoundError:
        return None
    relative = next((line[3:] for line in groups if line.startswith("0::")), None)
    if relative is None:
        return None
    for line in mount_rows:
        left, separator, right = line.partition(" - ")
        if not separator or right.split()[:1] != ["cgroup2"]:
            continue
        fields = left.split()
        root, mount = Path(_mount_path(fields[3])), Path(_mount_path(fields[4]))
        try:
            suffix = Path(relative).relative_to(root)
        except ValueError:
            continue
        return mount / suffix, mount
    return None


def capacity_limits(location: tuple[Path, Path] | None = None) -> tuple[float | None, int | None]:
    """Ancestors can constrain a child even when its own limits say 'max'."""
    location = current_cgroup() if location is None else location
    if location is None:
        return None, None
    current, root = location
    cpu_limit, memory_limit = None, None
    for directory in (current, *current.parents):
        try:
            quota, period = (directory / "cpu.max").read_text().split()
            if quota != "max":
                value = int(quota) / int(period)
                cpu_limit = value if cpu_limit is None else min(cpu_limit, value)
        except FileNotFoundError:
            pass
        try:
            maximum = (directory / "memory.max").read_text().strip()
        except FileNotFoundError:
            maximum = "max"
        if maximum != "max":
            used = int((directory / "memory.current").read_text().strip())
            remaining = max(0, int(maximum) - used) // (1024 * 1024)
            memory_limit = remaining if memory_limit is None else min(memory_limit, remaining)
        if directory == root:
            break
    return cpu_limit, memory_limit


def memory_limit_report(max_memory_mb, *, mechanism=None, reason=None):
    return {"mode": "hard-limit" if mechanism else "observed-only",
            "requested_mb": max_memory_mb,
            "enforced_mb": max_memory_mb if mechanism else None,
            "mechanism": mechanism, "reason": reason}


class MemoryCgroup:
    def __init__(self, path: Path, max_memory_mb: int):
        self.path = path
        self.max_memory_mb = max_memory_mb

    @classmethod
    def create(cls, max_memory_mb: int):
        try:
            location = current_cgroup()
        except PermissionError as exc:
            return None, f"cgroup v2 membership is not readable: {exc}"
        if location is None:
            return None, "cgroup v2 is unavailable"
        path = location[0] / ("mindie-process-" + uuid.uuid4().hex)
        created = False
        try:
            path.mkdir()
            created = True
            # The kernel creates memory.max only when the parent delegated
            # the memory controller. Never create a lookalike regular file.
            with (path / "memory.max").open("r+") as limit:
                limit.write(str(max_memory_mb * 1024 * 1024))
            observed = int((path / "memory.max").read_text().strip())
            if observed != max_memory_mb * 1024 * 1024:
                raise RuntimeError("cgroup memory.max did not retain the requested limit")
            return cls(path, max_memory_mb), None
        except BaseException as exc:
            if created:
                try:
                    path.rmdir()
                except Exception as cleanup_error:
                    record_cleanup_failure(exc, cleanup_error)
                    raise exc from cleanup_error
            if (isinstance(exc, TimeoutError) or not isinstance(exc, OSError)
                    or exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS,
                                         errno.ENOENT, errno.ENODEV, errno.EOPNOTSUPP}):
                raise
            return None, f"cgroup memory controller is not delegated/writable: {exc}"

    def close(self, *, timeout: float = 5.0):
        """The process owner drains first; remove only our empty kernel group."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.path.rmdir()
                return
            except FileNotFoundError:
                return
            except TimeoutError:
                raise
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned memory cgroup did not become empty") from exc
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
