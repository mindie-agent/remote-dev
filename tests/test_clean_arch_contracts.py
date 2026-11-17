"""Orthogonal clean-architecture behavioral contracts for remote-dev (AD-02, AD-03, AD-07, P14, P18)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from remote_dev.core import artifact_ops, job_ops, retention, rpc_transport
from remote_dev.core.endpoint import Endpoint
from remote_dev.mcp import catalog_refresh, tools
from remote_dev.result import make_result, recording_failure


REMOTE_WORKER_PATHS = (
    "remote_dev/core/remote_file_worker.py",
    "remote_dev/core/remote_file_text.py",
    "remote_dev/core/search_worker.py",
    "remote_dev/core/patch_worker.py",
    "remote_dev/processes/worker_runtime.py",
    "remote_dev/processes/worker_gc.py",
    "remote_dev/processes/worker.py",
    "remote_dev/processes/artifact_worker.py",
    "remote_dev/processes/python_inventory.py",
    "remote_dev/processes/mutation.py",
    "remote_dev/processes/probe.py",
    "remote_dev/processes/rpc_worker.py",
)

ALLOWED_SIBLING_IMPORTS = {
    ("remote_dev/core/remote_file_worker.py", "remote_file_text"),
    ("remote_dev/processes/worker.py", "worker_gc"),
    ("remote_dev/processes/worker.py", "worker_runtime"),
    ("remote_dev/processes/probe.py", "python_inventory"),
}


def test_remote_workers_are_pure_stdlib_and_windows_bootstrap_under_8191_chars():
    repo_root = Path(__file__).resolve().parents[1]
    stdlib_names = set(sys.stdlib_module_names)
    for relpath in REMOTE_WORKER_PATHS:
        path = repo_root / relpath
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relpath)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in stdlib_names, f"{relpath} imports non-stdlib module {alias.name!r}"
            elif isinstance(node, ast.ImportFrom):
                if node.level == 1 and (relpath, node.module) in ALLOWED_SIBLING_IMPORTS:
                    continue
                assert node.level == 0, f"{relpath} uses disallowed relative import {node.module!r}"
                top = (node.module or "").split(".")[0]
                assert top in stdlib_names, f"{relpath} imports non-stdlib module {node.module!r}"

    rpc_source = (repo_root / "remote_dev/processes/rpc_worker.py").read_text(encoding="utf-8")
    mutation_source = (repo_root / "remote_dev/processes/mutation.py").read_text(encoding="utf-8")
    merged = rpc_source.replace("# REMOTE_DEV_MUTATION_LOCK", mutation_source)
    with mock.patch("remote_dev.core.ssh_transport._native_windows_ssh_client", return_value=True):
        cmd = rpc_transport._worker_command(merged)
    assert len(cmd) < 8191, f"Windows SSH bootstrap length {len(cmd)} >= 8191"


def test_cross_session_explicit_job_id_visible_for_subagent_and_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REMOTE_DEV_HOST", "192.0.2.50")
    monkeypatch.setenv("REMOTE_DEV_PORT", "22")
    monkeypatch.setenv("REMOTE_DEV_ROOT", "/srv/work")

    stopped: list[str] = []

    def fake_control(ep, identifier, action, **params):
        if action == "launch":
            return {
                "state": "running",
                "quiet": False,
                "result": None,
                "accepted": True,
                "written": 0,
                "written_chars": 0,
                "eof": False,
                "stdout": "booting\n",
                "stderr": "",
                "stdout_offset": 8,
                "stderr_offset": 0,
                "stdout_bytes_remaining": 0,
                "stderr_bytes_remaining": 0,
            }
        if action == "stop":
            stopped.append(identifier)
            return {"state": "cancelled", "quiet": True, "unknown": []}
        return {
            "state": "running",
            "quiet": False,
            "stdout": "tick\n",
            "stderr": "",
            "stdout_offset": 5,
            "stderr_offset": 0,
            "stdout_bytes_remaining": 0,
            "stderr_bytes_remaining": 0,
        }

    ep_args = {"host": "192.0.2.50", "port": 22, "root": "/srv/work"}
    with mock.patch.object(job_ops, "control", fake_control):
        monkeypatch.setenv("REMOTE_DEV_SESSION_ID", "session-parent")
        launched = tools.call_tool(
            "remote_bash",
            {**ep_args, "command": "python train.py", "description": "Start training job", "run_in_background": True},
            profile="dsh",
        )
        assert launched["result"]["kind"] == "background"
        job_id = launched["result"]["jobId"]
        assert job_id

        # Switch to a different session ID (Subagent / reconnected MCP session)
        monkeypatch.setenv("REMOTE_DEV_SESSION_ID", "session-subagent")
        dsh_list = tools.call_tool("remote_job_list", dict(ep_args), profile="dsh")
        assert job_id in dsh_list["text"]

        kimi_list = tools.call_tool("remote_TaskList", {**ep_args, "active_only": False}, profile="kimi")
        assert job_id in kimi_list["text"]

        dsh_out = tools.call_tool("remote_job_output", {"job_id": job_id, "wait": False}, profile="dsh")
        assert dsh_out["result"].get("outcome") != "failed"

        kimi_out = tools.call_tool("remote_TaskOutput", {"task_id": job_id}, profile="kimi")
        assert kimi_out["result"].get("outcome") != "failed"

        kill_resp = tools.call_tool("remote_job_kill", {"job_id": job_id, "reason": "subagent cleanup"}, profile="dsh")
        assert kill_resp["result"].get("outcome") in {"cancellation-requested", "success"}
        assert stopped == [job_id]


def test_dsh_bash_background_launch_surfaces_failed_or_uncertain_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "state"))

    def fake_uncertain_bash(*_args, **_kwargs):
        return {
            "text": "SSH connection dropped after command dispatch",
            "result": {
                "outcome": "unknown",
                "status": "submission_uncertain",
                "state": "uncertain",
                "job_id": None,
                "summary": "SSH connection dropped after command dispatch",
                "error_details": {"category": "transport", "submission_state": "uncertain"},
            },
        }

    with mock.patch.object(tools, "remote_bash", fake_uncertain_bash):
        resp = tools.call_tool(
            "remote_bash",
            {"host": "192.0.2.50", "port": 22, "root": "/srv/work", "command": "python serve.py", "description": "Launch server", "run_in_background": True},
            profile="dsh",
        )
    assert resp["result"]["outcome"] == "unknown"
    assert resp["result"].get("kind") != "background"
    assert "Started background job" not in resp["text"]


def test_recording_failure_preserves_primary_success_with_receipt_warning():
    primary = make_result(
        tool="remote.write",
        target={"host": "192.0.2.10", "port": 22, "root": "/srv", "cwd": "/srv"},
        outcome="success",
        status="written",
        summary="Wrote /srv/config.json.",
    )
    wrapped = recording_failure(
        {"text": "Wrote /srv/config.json.", "result": primary},
        OSError(28, "No space left on device"),
        stage="read_ledger",
        operation_completed=True,
    )
    res = wrapped["result"]
    assert res["outcome"] == "success"
    assert res["status"] == "local_recording_failed"
    assert res["receipt_unconfirmed"] is True
    assert "No space left on device" in res["receipt_warning"]
    assert res["local_recording"]["stage"] == "read_ledger"
    assert res["operation_completed"] is True

    # Conversely, if the primary operation failed, outcome MUST remain "failed"
    failed_primary = make_result(
        tool="remote.bash",
        target={"host": "192.0.2.10", "port": 22, "root": "/srv", "cwd": "/srv"},
        outcome="failed",
        status="nonzero_exit",
        summary="Exited 7.",
    )
    wrapped_failed = recording_failure(
        {"text": "Exited 7.", "result": failed_primary},
        OSError(28, "No space left on device"),
        stage="job_record",
        operation_completed=True,
    )
    assert wrapped_failed["result"]["outcome"] == "failed"
    assert "receipt_unconfirmed" not in wrapped_failed["result"]


def test_serve_selected_executes_concurrent_tools_and_reuses_warm_workers(tmp_path):
    helper_script = tmp_path / "mcp_server.py"
    helper_script.write_text(
        """from __future__ import annotations
import json, os, sys, time

def serve(surface: str = "canonical") -> int:
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": surface, "version": "1.0"}}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "slow_bash":
                time.sleep(float(args.get("sleep_s", 0.35)))
            res = {"content": [{"type": "text", "text": json.dumps({"tool": name, "pid": os.getpid()})}], "isError": False}
        else:
            res = {}
        if mid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
            sys.stdout.flush()
    return 0

if __name__ == "__main__":
    raise SystemExit(serve(sys.argv[1] if len(sys.argv) > 1 else "canonical"))
""",
        encoding="utf-8",
    )

    c2s_r, c2s_w = os.pipe()
    s2c_r, s2c_w = os.pipe()
    repo_root = str(Path(__file__).resolve().parents[1])

    def server_thread_fn():
        with os.fdopen(c2s_r, "r", encoding="utf-8", closefd=True) as sin, os.fdopen(s2c_w, "w", encoding="utf-8", closefd=True) as sout:
            catalog_refresh.serve_selected(
                lambda: (
                    [sys.executable, "-I", str(helper_script), "canonical", "--once"],
                    {"PYTHONPATH": repo_root},
                ),
                reader=sin,
                writer=sout,
            )

    thread = threading.Thread(target=server_thread_fn, daemon=True)
    thread.start()

    responses: dict[int, tuple[float, dict]] = {}
    got_all = threading.Event()

    def reader_fn():
        with os.fdopen(s2c_r, "rb", closefd=True) as reader:
            for raw in reader:
                msg = json.loads(raw)
                if "id" in msg:
                    responses[msg["id"]] = (time.monotonic(), msg)
                    if {1, 2, 3, 4}.issubset(responses):
                        got_all.set()

    reader_thread = threading.Thread(target=reader_fn, daemon=True)
    reader_thread.start()

    t0 = time.monotonic()
    with os.fdopen(c2s_w, "w", encoding="utf-8", closefd=True) as writer:
        writer.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}) + "\n")
        writer.flush()
        while 1 not in responses and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)
        # Dispatch slow_bash (id=2, 0.40s) and fast_status (id=3) back-to-back!
        writer.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "slow_bash", "arguments": {"sleep_s": 0.40}}}) + "\n")
        writer.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fast_status", "arguments": {}}}) + "\n")
        writer.flush()
        while 3 not in responses and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)
        while 2 not in responses and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)
        # Send sequential fast_status (id=4) to verify warm helper PID reuse!
        writer.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "fast_status", "arguments": {}}}) + "\n")
        writer.flush()
        assert got_all.wait(timeout=5.0)

    thread.join(timeout=5.0)
    reader_thread.join(timeout=2.0)

    for req_id in (2, 3, 4):
        assert "error" not in responses[req_id][1], f"Unexpected JSON-RPC error on {req_id}: {responses[req_id][1]}"
        assert responses[req_id][1]["result"].get("isError") is not True

    # fast_status (id=3) MUST finish strictly before slow_bash (id=2)!
    assert responses[3][0] < responses[2][0]
    pid_2 = json.loads(responses[2][1]["result"]["content"][0]["text"])["pid"]
    pid_3 = json.loads(responses[3][1]["result"]["content"][0]["text"])["pid"]
    pid_4 = json.loads(responses[4][1]["result"]["content"][0]["text"])["pid"]
    # Sequential call id=4 reuses one of the already-warm helper processes!
    assert pid_4 in {pid_2, pid_3}


def test_streaming_sha256_and_in_root_symlinks_and_git_worktree(tmp_path):
    root = tmp_path / "workspace"
    blobs = root / "hf-cache" / "blobs"
    snaps = root / "hf-cache" / "snapshots" / "main"
    blobs.mkdir(parents=True)
    snaps.mkdir(parents=True)

    # 200 KiB payload (> 3 * 64 KiB chunks) + linked git worktree `.git` file
    payload = (b"Ascend910B-HBM-stream-chunk-" * 8000)[:204800]
    expected_sha = hashlib.sha256(payload).hexdigest()
    blob_file = blobs / expected_sha
    blob_file.write_bytes(payload)

    model_link = snaps / "model.safetensors"
    model_link.symlink_to(os.path.relpath(blob_file, snaps))
    (root / ".git").write_text("gitdir: /srv/git/worktrees/feature-a\n", encoding="utf-8")

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    escape_link = snaps / "escape.txt"
    escape_link.symlink_to(outside)

    dangling_link = snaps / "dangling.bin"
    dangling_link.symlink_to("missing-target.bin")

    worker_script = Path(__file__).resolve().parents[1] / "remote_dev" / "core" / "remote_file_worker.py"

    def run_file_worker(req: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, "-I", str(worker_script)],
            input=json.dumps(req).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    # Valid symlink inside root resolves cleanly and streams exact SHA-256
    stat_ok = run_file_worker({"op": "stat", "root": str(root), "file_path": str(model_link)})
    assert stat_ok["status"] == "ok"
    assert stat_ok["exists"] is True
    assert stat_ok["is_symlink"] is True
    assert stat_ok["size"] == len(payload)
    assert stat_ok["sha256"] == expected_sha

    # Linked git worktree `.git` file is readable/stat-able inside root
    git_stat = run_file_worker({"op": "stat", "root": str(root), "file_path": str(root / ".git")})
    assert git_stat["status"] == "ok"
    assert git_stat["exists"] is True

    # Dangling symlink inside root reports `dangling_symlink` honestly
    stat_dangling = run_file_worker({"op": "stat", "root": str(root), "file_path": str(dangling_link)})
    assert stat_dangling["status"] == "dangling_symlink"
    assert stat_dangling["exists"] is False
    assert stat_dangling["is_symlink"] is True

    # Escaping symlink outside root is STILL rejected
    stat_escape = run_file_worker({"op": "stat", "root": str(root), "file_path": str(escape_link)})
    assert stat_escape["status"] == "path_outside_root"

    # Local artifact manifest rejects dangling AND escaping symlinks, then follows valid in-root symlinks with 64 KiB streaming
    with pytest.raises(ValueError, match="dangling local artifact symlink"):
        artifact_ops._local_manifest(root / "hf-cache")
    dangling_link.unlink()
    with pytest.raises(ValueError, match="escapes source root"):
        artifact_ops._local_manifest(root / "hf-cache")
    escape_link.unlink()
    manifest = artifact_ops._local_manifest(root / "hf-cache")
    by_rel = {row["relpath"]: row for row in manifest["files"]}
    assert by_rel["snapshots/main/model.safetensors"]["sha256"] == expected_sha
    assert by_rel["snapshots/main/model.safetensors"]["size"] == len(payload)


def test_windows_session_lock_liveness_and_endpoint_context_artifacts_patches_gc(tmp_path):
    now = time.time()
    old = (now - 48 * 3600, now - 48 * 3600)
    sessions = tmp_path / "sessions"

    free_dir = sessions / "session-4242-free"
    free_dir.mkdir(parents=True)
    (free_dir / "pid").write_text("4242", encoding="utf-8")
    (free_dir / "session.lock").write_bytes(b"\0")
    os.utime(free_dir, old)

    held_dir = sessions / "session-4243-held"
    held_dir.mkdir(parents=True)
    (held_dir / "pid").write_text("4243", encoding="utf-8")
    (held_dir / "session.lock").write_bytes(b"\0")
    os.utime(held_dir, old)

    class DeadWinProc:
        def open_process(self, pid):
            return None

        def last_error(self):
            return 87  # ERROR_INVALID_PARAMETER (exited)

        def exit_code(self, handle):
            return 0

        def close(self, handle):
            return 1

    class SelectiveShareApi:
        def open_exclusive(self, path):
            if "4243-held" in str(path):
                return None
            return object()

        def last_error(self):
            return 32  # ERROR_SHARING_VIOLATION

        def close(self, handle):
            return 1

    class FreeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, fileno, mode, length):
            return None

    stats = retention.prune_stale_session_runtime_dirs(
        sessions,
        now=now,
        platform="nt",
        winapi=DeadWinProc(),
        msvcrt_module=FreeMsvcrt(),
        share_api=SelectiveShareApi(),
    )
    assert stats["removed"] == ["session-4242-free"]
    assert not free_dir.exists()
    assert held_dir.exists()
    assert stats["skipped"]["locked_or_unverifiable"] == 1

    # Endpoint context/, artifacts/, and patches/ pruning
    ep = tmp_path / "endpoints" / "ep-gc"
    for sub in ("reads", "logs", "jobs", "context", "artifacts", "patches"):
        (ep / sub).mkdir(parents=True)

    # Active latest.json + its referenced context-20261117T000000Z.json must be protected
    active_ctx = ep / "context" / "context-20261117T000000Z.json"
    active_ctx.write_text("{}", encoding="utf-8")
    os.utime(active_ctx, (now - 30 * 86400, now - 30 * 86400))
    (ep / "context" / "latest.json").write_text(
        json.dumps({"created_at": "2026-11-17T00:00:00Z", "refs": {"full_probe": str(active_ctx)}}),
        encoding="utf-8",
    )
    stale_ctx = ep / "context" / "context-20260101T000000Z.json"
    stale_ctx.write_text("{}", encoding="utf-8")
    os.utime(stale_ctx, (now - 30 * 86400, now - 30 * 86400))

    # Old artifact dir is pruned; fresh (<60s) in-flight artifact dir is protected
    old_art = ep / "artifacts" / "art-expired"
    old_art.mkdir()
    (old_art / "manifest.json").write_text("{}", encoding="utf-8")
    os.utime(old_art, (now - 30 * 86400, now - 30 * 86400))

    fresh_art = ep / "artifacts" / "art-inflight"
    fresh_art.mkdir()
    (fresh_art / "part.bin").write_bytes(b"1234")
    os.utime(fresh_art, (now - 5, now - 5))

    # Old patch receipt is pruned
    old_patch = ep / "patches" / "inv-expired.json"
    old_patch.write_text("{}", encoding="utf-8")
    os.utime(old_patch, (now - 30 * 86400, now - 30 * 86400))

    prune_stats = retention.prune_endpoint_state(
        ep,
        now=now,
        force=True,
        artifacts_max_entries=0,  # Even with max_entries=0, <60s in-flight artifact stays!
    )
    assert prune_stats["context"]["removed"] == 1
    assert active_ctx.exists()
    assert (ep / "context" / "latest.json").exists()
    assert not stale_ctx.exists()
    assert prune_stats["artifacts"]["removed"] == 1
    assert not old_art.exists()
    assert fresh_art.exists()
    assert prune_stats["patches"]["removed"] == 1
    assert not old_patch.exists()


def test_control_plane_pr_checklist_validator():
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_pr_checklist.py"
    spec = importlib.util.spec_from_file_location("check_pr_checklist", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    valid_body = """## 变更摘要 (Summary)
- Concurrent resident MCP gateway, cross-session job_id visibility, and 64 KiB streaming SHA-256.

## 设计原则 Review (Pre-PR Checklist)
- [x] **CTRL-LEASE-OR-AUTH**: Shared SSH credential profiles and read-ledger hashes preserved.
- [x] **CTRL-HANDLE**: Concurrent stdio dispatch and cross-session explicit job_id verified.
- [x] **CTRL-STREAM-IO**: 12 pure-stdlib workers, <8191-char bootstrap, and 64 KiB streaming verified.
- [x] **CTRL-RECEIPT**: dsh_bash launch outcome and recording_failure Receipt Honesty verified.

- **CTRL-LEASE-OR-AUTH**: Verified in test_auth_access.py and test_patch_ops.py.
- **CTRL-HANDLE**: Verified in test_clean_arch_contracts.py concurrent stdio and subagent tests.
- **CTRL-STREAM-IO**: Verified Windows bootstrap length and 200 KiB chunked SHA-256 + symlink tests.
- **CTRL-RECEIPT**: Verified ENOSPC local recording failure preserves outcome=success.

## 验证结果 (Verification)
- Ran isolated pytest -q (all tests passed).
"""
    assert mod.validate_pr_body(valid_body, profile="control-plane") == []
    unchecked = valid_body.replace("- [x] **CTRL-HANDLE**", "- [ ] **CTRL-HANDLE**")
    assert mod.validate_pr_body(unchecked, profile="control-plane") != []


def test_nudge_supervisor_skips_pre_upgrade_supervisor_and_wakes_advertised_supervisor(tmp_path):
    from _worker_source import load_worker_module

    pytest.importorskip("fcntl")
    worker = load_worker_module()

    job_dir = tmp_path / "job-legacy"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(
        json.dumps({"pid": 424242, "start_ticks": "99", "boot_id": "b1", "marker": "m" * 32}),
        encoding="utf-8",
    )
    # Legacy supervisor-ready.json without wakeup_sigusr1
    (job_dir / "supervisor-ready.json").write_text(json.dumps({"pid": 424242}), encoding="utf-8")

    with mock.patch.object(worker, "owned_processes", return_value=([{"pid": 424242, "start_ticks": "99"}], [])), \
         mock.patch.object(worker.os, "kill") as mock_kill:
        worker.nudge_supervisor(job_dir)
        mock_kill.assert_not_called()

        # Upgraded supervisor advertising wakeup_sigusr1=True
        (job_dir / "supervisor-ready.json").write_text(
            json.dumps({"pid": 424242, "wakeup_sigusr1": True}), encoding="utf-8"
        )
        worker.nudge_supervisor(job_dir)
        mock_kill.assert_called_once()


def test_artifact_gc_preserves_long_running_locked_pull_directory(tmp_path):
    from remote_dev.core.locking import record_lock

    now = time.time()
    ep = tmp_path / "endpoints" / "ep-art-lock"
    for sub in ("reads", "logs", "jobs", "context", "artifacts", "patches"):
        (ep / sub).mkdir(parents=True)

    active_pull = ep / "artifacts" / "long-pull-active"
    active_pull.mkdir()
    (active_pull / "large.safetensors").write_bytes(b"x" * 4096)
    # Simulate a download that started 2 hours ago (past both 60s grace and 0-byte cap)
    os.utime(active_pull, (now - 7200, now - 7200))

    finished_pull = ep / "artifacts" / "old-pull-done"
    finished_pull.mkdir()
    (finished_pull / "done.bin").write_bytes(b"y" * 4096)
    os.utime(finished_pull, (now - 7200, now - 7200))

    held = threading.Event()
    release = threading.Event()

    def hold_pull_lock():
        with record_lock(active_pull, timeout_seconds=5.0):
            held.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_pull_lock, daemon=True)
    holder.start()
    assert held.wait(timeout=2.0)
    try:
        stats = retention.prune_endpoint_state(
            ep, now=now, force=True, artifacts_max_entries=0, artifacts_max_bytes=0
        )
    finally:
        release.set()
        holder.join(timeout=2.0)

    assert active_pull.exists()
    assert (active_pull / "large.safetensors").exists()
    assert not finished_pull.exists()
    assert stats["artifacts"]["removed"] == 1
    assert stats["artifacts"]["skipped_locked"] == 1


def test_pre_cancelled_request_never_writes_to_warm_helper_and_empty_local_dir_locks(tmp_path):
    import inspect

    # 1. Empty string local_dir="" still acquires record_lock on managed artifacts/<uuid>
    fn = inspect.unwrap(artifact_ops.remote_artifact_pull)
    manifest = {"result": {"outcome": "success", "manifest": {"status": "ok", "files": []}}}
    endpoint = Endpoint(host="192.0.2.10", port=22, root="/srv", cwd="/srv", runtime_env=False)
    with mock.patch.object(artifact_ops, "remote_artifact_manifest", return_value=manifest), \
         mock.patch.object(artifact_ops, "ensure_endpoint_state", return_value=tmp_path / "ep"), \
         mock.patch.object(artifact_ops, "record_lock") as mock_lock:
        mock_lock.return_value.__enter__.return_value = None
        mock_lock.return_value.__exit__.return_value = False
        fn(endpoint, remote_path="/srv/item", local_dir="")
        assert mock_lock.call_count == 1

    # 2. Already-cancelled request in _ResidentPool.call never writes to helper stdin
    marker = tmp_path / "executed_marker.txt"
    helper_script = tmp_path / "mcp_server.py"
    helper_script.write_text(
        f"""from __future__ import annotations
import json, pathlib, sys
def serve(surface: str = "canonical") -> int:
    for raw in sys.stdin.buffer:
        msg = json.loads(raw)
        if msg.get("method") == "tools/call":
            pathlib.Path({str(marker)!r}).write_text("MUTATED", encoding="utf-8")
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": msg.get("id"), "result": {{}}}}) + "\\n")
        sys.stdout.flush()
    return 0
""",
        encoding="utf-8",
    )
    pool = catalog_refresh._ResidentPool()
    cmd = [sys.executable, "-I", str(helper_script), "canonical", "--once"]
    try:
        pool.call(cmd, {}, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, timeout=10.0, cancel=None)
        pre_cancelled = threading.Event()
        pre_cancelled.set()
        with pytest.raises(InterruptedError, match="cancelled before dispatch"):
            pool.call(cmd, {}, {"jsonrpc": "2.0", "id": 2, "method": "tools/call"}, timeout=10.0, cancel=pre_cancelled)
        assert not marker.exists()
    finally:
        pool.close()
