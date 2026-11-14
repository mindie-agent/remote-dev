"""
[Role] Cross-platform Windows extended-path normalization and WSL2 native executable lookup (`_host_exec.py`).
[Invariants]
- On POSIX, `which_native` and `posix_unshadowed_command` never call `shutil.which` and never select Windows PE (`MZ..PE\\0\\0`) or WSL DrvFS (`/proc/self/mountinfo` `drvfs`/`9p`) binaries appended on `$PATH`.
- `os.stat()` is used directly (never `os.path.isfile` / `Path.is_file`) so `TimeoutError` is never swallowed as a missing file.
- `strip_windows_extended_prefix` removes `\\\\?\\` on standard-length Windows paths (`< 260` chars) for MSYS2/Git for Windows tools (`ssh.exe`, `ssh-keygen.exe`, `git.exe`).
[Timeouts & Locks]
- Bounded in-process `stat`, `mountinfo`, and 4 KiB PE header reads; acquires no locks and spawns no subprocesses.
[Side-Effect Policy]
- Read-only filesystem inspection only; never mutates `os.environ` or files.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .errors import RemoteExecutionError

_WIN_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")
_MOUNTINFO_PATH = "/proc/self/mountinfo"


def strip_windows_extended_prefix(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if os.name == "nt":
        if raw.startswith("\\\\?\\UNC\\"):
            unc = "\\" + raw[7:]
            if len(unc) < 260:
                return unc
        elif raw.startswith("\\\\?\\") and len(raw) - 4 < 260:
            return raw[4:]
    return raw


def _decode_mount_field(field: str) -> str:
    for seq, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(seq, ch)
    return field


def _strict_realpath(raw_text: str) -> str | None:
    probe = raw_text if os.path.isabs(raw_text) else os.path.join(os.getcwd(), raw_text)
    suffix: list[str] = []
    while True:
        try:
            resolved = os.path.realpath(probe, strict=True)
            return os.path.normpath(os.path.join(resolved, *reversed(suffix))) if suffix else resolved
        except TimeoutError:
            raise
        except (FileNotFoundError, NotADirectoryError):
            parent, name = os.path.split(probe.rstrip("/"))
            if not name or parent == probe:
                return None
            suffix.append(name)
            probe = parent
        except OSError:
            return None


def is_wsl_drvfs(value: str | os.PathLike[str]) -> bool:
    if os.name == "nt":
        return False
    try:
        raw_mountinfo = Path(_MOUNTINFO_PATH).read_text(encoding="utf-8", errors="replace")
    except TimeoutError:
        raise
    except OSError:
        return False
    raw_text = os.fspath(value)
    abs_target = os.path.abspath(raw_text)
    real = _strict_realpath(raw_text)
    targets = [abs_target] if not real or real == abs_target else [abs_target, real]
    mounts: list[tuple[str, bool]] = []
    for line in raw_mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lfields = left.split()
        rfields = right.split()
        if len(lfields) < 5 or len(rfields) < 3:
            continue
        mp = _decode_mount_field(lfields[4])
        fstype, opts = rfields[0], rfields[2]
        on_drvfs = fstype == "drvfs" or (fstype == "9p" and "aname=drvfs" in opts)
        mounts.append((mp, on_drvfs))
    for target in targets:
        best_len = -1
        best_drvfs = False
        for mp, on_drvfs in mounts:
            prefix = "/" if mp == "/" else mp.rstrip("/")
            if target == prefix or (prefix == "/" and target.startswith("/")) or target.startswith(prefix + "/"):
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best_drvfs = on_drvfs
        if best_drvfs:
            return True
    return False


def is_pe_executable(value: str | os.PathLike[str]) -> bool:
    try:
        with Path(value).open("rb") as handle:
            head = handle.read(4096)
    except TimeoutError:
        raise
    except OSError:
        return False
    if len(head) < 64 or head[:2] != b"MZ":
        return False
    pe_offset = int.from_bytes(head[0x3C:0x40], "little", signed=False)
    return 0 <= pe_offset <= len(head) - 4 and head[pe_offset : pe_offset + 4] == b"PE\x00\x00"


def _is_exec_regular_file(path_str: str) -> bool:
    try:
        st = os.stat(path_str)
        return stat.S_ISREG(st.st_mode) and os.access(path_str, os.X_OK)
    except TimeoutError:
        raise
    except OSError:
        return False


def which_native(command: str, *, path: str | None = None) -> str | None:
    if not command:
        return None
    if os.name == "nt":
        return shutil.which(command, path=path)
    if "/" in command or (os.path.altsep and os.path.altsep in command):
        if _is_exec_regular_file(command) and not is_pe_executable(command) and not is_wsl_drvfs(command):
            return command
        return None
    if command.lower().endswith(_WIN_SUFFIXES):
        return None
    raw_path = os.environ.get("PATH", "") if path is None else path
    for entry in raw_path.split(os.pathsep):
        candidate = os.path.join(entry or ".", command)
        if _is_exec_regular_file(candidate):
            if is_pe_executable(candidate) or is_wsl_drvfs(candidate):
                continue
            return candidate
    return None


def posix_unshadowed_command(command: str, *, path: str | None = None) -> str:
    """Return bare `command` when unshadowed, or the native POSIX path past any DrvFS/PE shadow."""
    raw_path = os.environ.get("PATH", "") if path is None else path
    saw_shadow = False
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        candidate = os.path.join(entry, command)
        if not _is_exec_regular_file(candidate):
            continue
        if is_pe_executable(candidate) or is_wsl_drvfs(candidate):
            saw_shadow = True
            continue
        return candidate if saw_shadow else command
    if saw_shadow:
        raise RemoteExecutionError(
            f"POSIX {command!r} resolved only to a Windows PE / WSL DrvFS shim on PATH; "
            f"install native Linux {command!r} (for example openssh-client)",
            category="configuration",
            submission_state="not_sent",
        )
    return command
