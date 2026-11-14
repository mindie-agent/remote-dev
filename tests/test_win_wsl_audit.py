"""Windows PowerShell / CP936, extended-path, and WSL2 DrvFS/PE unshadowing tests."""
from __future__ import annotations

import os
from pathlib import Path
import time
from unittest import mock

import pytest

from remote_dev.auth import access
from remote_dev.core import _host_exec, media_core, ssh_transport
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.processes import probe


def _fake_pe_bytes() -> bytes:
    buf = bytearray(128)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = (64).to_bytes(4, "little")
    buf[64:68] = b"PE\x00\x00"
    return bytes(buf)


def test_strip_windows_extended_prefix_handles_standard_and_long_paths(monkeypatch):
    monkeypatch.setattr(_host_exec.os, "name", "nt")
    short = r"\\?\C:\Users\maoxx241\AppData\Local\Programs\Git\usr\bin\ssh.exe"
    assert _host_exec.strip_windows_extended_prefix(short) == r"C:\Users\maoxx241\AppData\Local\Programs\Git\usr\bin\ssh.exe"
    unc = r"\\?\UNC\server\share\Git\usr\bin\ssh.exe"
    assert _host_exec.strip_windows_extended_prefix(unc) == r"\\server\share\Git\usr\bin\ssh.exe"
    very_long = "\\\\?\\C:\\" + ("a" * 270) + "\\ssh.exe"
    assert _host_exec.strip_windows_extended_prefix(very_long) == very_long
    assert probe._normalize_git_path(short) == r"C:\Users\maoxx241\AppData\Local\Programs\Git\usr\bin\ssh.exe"


def test_windows_ssh_executable_supports_libexec_git_core_and_stat_timeout(tmp_path):
    for sub in ("mingw64/libexec/git-core", "ucrt64/libexec/git-core", "libexec/git-core"):
        root = tmp_path / sub.replace("/", "_")
        ssh = root / "usr" / "bin" / "ssh.exe"
        git = root / sub / "git.exe"
        ssh.parent.mkdir(parents=True, exist_ok=True)
        git.parent.mkdir(parents=True, exist_ok=True)
        ssh.write_bytes(b"ssh")
        git.write_bytes(b"git")
        with mock.patch.object(ssh_transport.shutil, "which", return_value=str(git)):
            assert ssh_transport._windows_ssh_executable() == _host_exec.strip_windows_extended_prefix(str(ssh.resolve()))

    git_any = tmp_path / "libexec_git-core" / "libexec" / "git-core" / "git.exe"
    with mock.patch.object(ssh_transport.shutil, "which", return_value=str(git_any)), mock.patch.object(
        Path, "stat", side_effect=TimeoutError("ssh_stat_deadline")
    ):
        with pytest.raises(TimeoutError, match="ssh_stat_deadline"):
            ssh_transport._windows_ssh_executable()


@pytest.mark.skipif(os.name == "nt", reason="POSIX / WSL2 DrvFS and PE unshadowing tests")
def test_wsl_posix_unshadowed_command_skips_drvfs_and_pe_shims_and_fails_closed(tmp_path, monkeypatch):
    drvfs_bin = tmp_path / "mnt_c" / "Program Files (x86)" / "OpenSSH"
    pe_bin = tmp_path / "pe_shims"
    native_bin = tmp_path / "usr_bin"
    drvfs_bin.mkdir(parents=True)
    pe_bin.mkdir(parents=True)
    native_bin.mkdir(parents=True)

    escaped_mount = str((tmp_path / "mnt_c").resolve()).replace(" ", "\\040")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"22 1 8:2 / / rw,relatime - ext4 /dev/sdb rw\n"
        f"88 22 0:49 / {escaped_mount} rw,noatime - 9p C:\\134 rw,dirsync,aname=drvfs;path=C:\\134;uid=1000;gid=1000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_host_exec, "_MOUNTINFO_PATH", str(mountinfo))

    drvfs_ssh = drvfs_bin / "ssh"
    drvfs_ssh.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    drvfs_ssh.chmod(0o755)

    pe_ssh = pe_bin / "ssh"
    pe_ssh.write_bytes(_fake_pe_bytes())
    pe_ssh.chmod(0o755)
    pe_keygen = pe_bin / "ssh-keygen"
    pe_keygen.write_bytes(_fake_pe_bytes())
    pe_keygen.chmod(0o755)

    # Only Windows PE / DrvFS shims on PATH -> fails closed with configuration RemoteExecutionError
    shadow_only = os.pathsep.join([str(drvfs_bin), str(pe_bin)])
    monkeypatch.setenv("PATH", shadow_only)
    with pytest.raises(RemoteExecutionError) as exc_info:
        ssh_transport._ssh_executable()
    assert exc_info.value.category == "configuration"
    assert exc_info.value.submission_state == "not_sent"
    assert media_core._which("ssh") is None

    # Native Linux binary after DrvFS + PE shadows -> selects full native path without calling shutil.which
    native_ssh = native_bin / "ssh"
    native_ssh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    native_ssh.chmod(0o755)
    native_keygen = native_bin / "ssh-keygen"
    native_keygen.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = "-f" ]; then f="$2"; shift 2; else shift; fi; done\n'
        'printf "PRIVATE\\n" > "$f"\nchmod 600 "$f"\nprintf "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test@wsl\\n" > "$f.pub"\n',
        encoding="utf-8",
    )
    native_keygen.chmod(0o755)

    monkeypatch.setenv("PATH", os.pathsep.join([shadow_only, str(native_bin)]))
    with mock.patch.object(ssh_transport.shutil, "which") as mock_which:
        assert ssh_transport._ssh_executable() == str(native_ssh)
        mock_which.assert_not_called()

    # ensure_key uses native ssh-keygen rather than PE shim
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    key_path, pub_line = access.ensure_key()
    assert Path(key_path).is_file()
    assert pub_line.startswith("ssh-ed25519 ")

    # Unshadowed native PATH preserves bare "ssh"
    monkeypatch.setenv("PATH", str(native_bin))
    assert ssh_transport._ssh_executable() == "ssh"


@pytest.mark.skipif(os.name == "nt", reason="POSIX stat/lstat TimeoutError propagation")
def test_host_exec_propagates_stat_and_lstat_timeout_error(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("22 1 8:2 / / rw,relatime - ext4 /dev/sdb rw\n", encoding="utf-8")
    monkeypatch.setattr(_host_exec, "_MOUNTINFO_PATH", str(mountinfo))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_exec.os, "stat", lambda p: (_ for _ in ()).throw(TimeoutError("stat_timeout")))
        with pytest.raises(TimeoutError, match="stat_timeout"):
            _host_exec.which_native("ssh", path=str(tmp_path))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_host_exec.os, "lstat", lambda p, *a, **kw: (_ for _ in ()).throw(TimeoutError("lstat_timeout")))
        with pytest.raises(TimeoutError, match="lstat_timeout"):
            _host_exec.is_wsl_drvfs(tmp_path / "link" / "ssh")


def test_probe_git_fact_decodes_utf8_and_propagates_budget_timeout(tmp_path):
    with pytest.raises(TimeoutError, match="probe Git budget exhausted"):
        probe.git_fact(str(tmp_path), "status", deadline=time.monotonic() - 0.1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink + dotdot DrvFS resolution")
def test_symlink_plus_dotdot_does_not_bypass_drvfs_guard(tmp_path, monkeypatch):
    drvfs_root = tmp_path / "mnt_c"
    drvfs_sub = drvfs_root / "tools" / "subdir"
    drvfs_sub.mkdir(parents=True)
    shim = drvfs_root / "tools" / "ssh"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    (safe_dir / "link").symlink_to(drvfs_sub)
    native_dir = tmp_path / "native"
    native_dir.mkdir()
    native_ssh = native_dir / "ssh"
    native_ssh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    native_ssh.chmod(0o755)

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"22 1 8:2 / / rw,relatime - ext4 /dev/sdb rw\n"
        f"99 22 0:42 / {drvfs_root.resolve()} rw,noatime - 9p C:\\134 rw,aname=drvfs;metadata\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_host_exec, "_MOUNTINFO_PATH", str(mountinfo))
    tricky_entry = f"{safe_dir}/link/.."
    assert _host_exec.is_wsl_drvfs(f"{tricky_entry}/ssh") is True
    assert _host_exec.which_native("ssh", path=f"{tricky_entry}:{native_dir}") == str(native_ssh)

