"""Orthogonal multi-thread and precedence tests for ContextVar-backed ``session_scope``."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import threading
from unittest import mock
import pytest

from remote_dev.auth.store import CredentialStore, auth_root
from remote_dev.core import job_records, rpc_transport
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.locking import record_lock
import remote_dev.core.state_store as state_store
from remote_dev.core.state_store import (
    LEDGER_NO_CONTEXT_SCOPE,
    LEDGER_SCOPE_ENV_VARS,
    atomic_write_json,
    endpoint_state_dir,
    job_record_path,
    load_read_ledger,
    load_write_ledger_guard,
    maybe_prune_endpoint_state,
    resolve_ledger_scope,
    scoped_auth_dir,
    session_scope,
    state_root,
    write_read_ledger,
)
from remote_dev.mcp.catalog_refresh import serve_selected


@pytest.fixture(autouse=True)
def _isolated_host_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve() / "host-env"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(root / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "localappdata"))
    monkeypatch.setenv("UV_CACHE_DIR", str(root / "uv-cache"))
    monkeypatch.setenv("TMPDIR", str(root / "tmp"))
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(root / "default-auth"))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    for key in LEDGER_SCOPE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_concurrent_multithread_session_scope_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline_state = tmp_path / "baseline-state"
    baseline_auth = tmp_path / "baseline-auth"
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(baseline_state))
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(baseline_auth))
    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)

    env_keys = ("REMOTE_DEV_SESSION_ID", "REMOTE_DEV_STATE_DIR", "REMOTE_DEV_AUTH_DIR")
    env_snapshot = {key: os.environ.get(key) for key in env_keys}

    worker_count = 8
    enter_barrier = threading.Barrier(worker_count, timeout=10.0)
    write_barrier = threading.Barrier(worker_count, timeout=10.0)
    endpoint = Endpoint(host="192.0.2.10", port=2222, root="/vllm-workspace")
    file_path = "/vllm-workspace/model.py"

    def run_worker(idx: int) -> tuple[str, Path, Path]:
        thread_state = tmp_path / f"state-{idx}"
        thread_auth = tmp_path / f"auth-{idx}"
        expected_sha = f"{idx + 1:064x}"
        with session_scope(f"session-{idx}", state_dir=thread_state, auth_dir=thread_auth):
            enter_barrier.wait()
            assert state_root() == thread_state
            assert scoped_auth_dir() == thread_auth
            assert auth_root() == thread_auth.resolve()
            assert {key: os.environ.get(key) for key in env_keys} == env_snapshot

            cred_ref = CredentialStore().put(f"secret-{idx}")
            assert CredentialStore().get(cred_ref) == f"secret-{idx}"

            ledger_path = write_read_ledger(
                endpoint,
                {
                    "path": file_path,
                    "sha256": expected_sha,
                    "size": 100 + idx,
                    "mtime_ns": 1000 + idx,
                },
            )
            write_barrier.wait()

            loaded = load_read_ledger(endpoint, file_path)
            guard = load_write_ledger_guard(endpoint, file_path)
            expected_scope = resolve_ledger_scope(f"session-{idx}")
            assert expected_scope.startswith("id-")
            assert loaded is not None
            assert loaded["sha256"] == expected_sha
            assert loaded["ledger_scope"] == expected_scope
            assert guard.scope == expected_scope
            assert guard.ledger is not None
            assert guard.ledger["sha256"] == expected_sha
            assert ledger_path.is_relative_to(thread_state)
            assert {key: os.environ.get(key) for key in env_keys} == env_snapshot

        assert scoped_auth_dir() is None
        assert state_root() == baseline_state
        assert auth_root() == baseline_auth.resolve()
        return expected_scope, thread_state, thread_auth

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(run_worker, i) for i in range(worker_count)]
        results = [future.result(timeout=15.0) for future in futures]

    scopes = [scope for scope, _, _ in results]
    assert len(set(scopes)) == worker_count
    assert {key: os.environ.get(key) for key in env_keys} == env_snapshot
    assert not baseline_state.exists()
    assert not baseline_auth.exists()

    for idx, (scope, thread_state, thread_auth) in enumerate(results):
        reads_dir = thread_state / "endpoints" / endpoint.endpoint_id / "reads"
        scope_dirs = [entry.name for entry in reads_dir.iterdir() if entry.is_dir()]
        assert scope_dirs == [scope]
        creds = json.loads((thread_auth / "credentials.json").read_text(encoding="utf-8"))
        assert list(creds.values()) == [f"secret-{idx}"]


def test_concurrent_multithread_shared_state_dir_isolates_read_ledgers(tmp_path: Path) -> None:
    shared_state = tmp_path / "shared-state"
    worker_count = 8
    barrier = threading.Barrier(worker_count, timeout=10.0)
    endpoint = Endpoint(host="192.0.2.11", port=2222, root="/vllm-workspace")
    file_path = "/vllm-workspace/shared.py"

    def run_worker(idx: int) -> str:
        expected_sha = f"{idx + 100:064x}"
        with session_scope(f"shared-sess-{idx}", state_dir=shared_state):
            barrier.wait()
            write_read_ledger(
                endpoint,
                {"path": file_path, "sha256": expected_sha, "size": idx + 1, "mtime_ns": idx + 10},
            )
            barrier.wait()
            guard = load_write_ledger_guard(endpoint, file_path)
            assert guard.ledger is not None
            assert guard.ledger["sha256"] == expected_sha
            return guard.scope

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        scopes = [future.result(timeout=15.0) for future in [pool.submit(run_worker, i) for i in range(worker_count)]]

    assert len(set(scopes)) == worker_count


def test_session_scope_precedence_nesting_and_exception_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    substrate = tmp_path / "substrate"
    monkeypatch.setattr(state_store, "substrate_root", lambda: substrate)
    monkeypatch.delenv("REMOTE_DEV_STATE_DIR", raising=False)
    monkeypatch.delenv("REMOTE_DEV_AUTH_DIR", raising=False)

    # 1. Baseline without env or scope -> "default", <substrate>/state, ~/.mindie/ssh
    assert resolve_ledger_scope() == LEDGER_NO_CONTEXT_SCOPE
    assert state_root() == substrate / "state"
    assert scoped_auth_dir() is None
    assert auth_root() == (Path.home() / ".mindie" / "ssh").resolve()

    # 2. Environment level -> REMOTE_DEV_SESSION_ID / REMOTE_DEV_STATE_DIR / REMOTE_DEV_AUTH_DIR
    env_state = tmp_path / "env-state"
    env_auth = tmp_path / "env-auth"
    monkeypatch.setenv("REMOTE_DEV_SESSION_ID", "env-session")
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(env_state))
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(env_auth))
    assert resolve_ledger_scope() == resolve_ledger_scope("env-session")
    assert state_root() == env_state
    assert auth_root() == env_auth.resolve()

    outer_state = tmp_path / "outer-state"
    outer_auth = tmp_path / "outer-auth"
    inner_state = tmp_path / "inner-state"
    inner_auth = tmp_path / "inner-auth"
    endpoint = Endpoint(host="192.0.2.20", port=22, root="/vllm-workspace")

    # 3. Outer session_scope overrides env; explicit client_context_id overrides session_scope
    with session_scope("outer-session", state_dir=outer_state, auth_dir=outer_auth):
        assert resolve_ledger_scope() == resolve_ledger_scope("outer-session")
        assert resolve_ledger_scope("explicit-client") == (
            "id-" + hashlib.sha256(b"explicit-client").hexdigest()
        )
        assert resolve_ledger_scope("explicit-client") != resolve_ledger_scope()
        assert state_root() == outer_state
        assert scoped_auth_dir() == outer_auth
        assert auth_root() == outer_auth.resolve()
        assert os.environ["REMOTE_DEV_SESSION_ID"] == "env-session"

        write_read_ledger(
            endpoint,
            {"path": "/vllm-workspace/a.py", "sha256": "1" * 64, "size": 1, "mtime_ns": 1},
        )
        write_read_ledger(
            endpoint,
            {"path": "/vllm-workspace/a.py", "sha256": "2" * 64, "size": 2, "mtime_ns": 2},
            client_context_id="explicit-client",
        )
        assert load_write_ledger_guard(endpoint, "/vllm-workspace/a.py").ledger["sha256"] == "1" * 64
        assert (
            load_write_ledger_guard(
                endpoint, "/vllm-workspace/a.py", client_context_id="explicit-client"
            ).ledger["sha256"]
            == "2" * 64
        )

        # 4. Nested session_scope without state_dir/auth_dir inherits outer directories
        with session_scope("inner-inherited"):
            assert resolve_ledger_scope() == resolve_ledger_scope("inner-inherited")
            assert state_root() == outer_state
            assert scoped_auth_dir() == outer_auth
            assert auth_root() == outer_auth.resolve()
            assert load_read_ledger(endpoint, "/vllm-workspace/a.py") is None

        assert resolve_ledger_scope() == resolve_ledger_scope("outer-session")
        assert state_root() == outer_state
        assert scoped_auth_dir() == outer_auth

        # 5. Nested session_scope with explicit overrides + exception still restores outer ContextVars
        with pytest.raises(RuntimeError, match="simulated inner failure"):
            with session_scope("inner-override", state_dir=str(inner_state), auth_dir=str(inner_auth)):
                assert resolve_ledger_scope() == resolve_ledger_scope("inner-override")
                assert state_root() == inner_state
                assert scoped_auth_dir() == inner_state.parent / "inner-auth"
                assert auth_root() == inner_auth.resolve()
                raise RuntimeError("simulated inner failure")

        assert resolve_ledger_scope() == resolve_ledger_scope("outer-session")
        assert state_root() == outer_state
        assert scoped_auth_dir() == outer_auth
        assert auth_root() == outer_auth.resolve()

    # 6. Exiting outer scope (including via exception) restores baseline env state
    with pytest.raises(ValueError, match="simulated outer failure"):
        with session_scope("transient-outer", state_dir="~/scoped-state", auth_dir="~/scoped-auth"):
            assert state_root() == Path.home() / "scoped-state"
            assert auth_root() == (Path.home() / "scoped-auth").resolve()
            raise ValueError("simulated outer failure")

    assert resolve_ledger_scope() == resolve_ledger_scope("env-session")
    assert state_root() == env_state
    assert scoped_auth_dir() is None
    assert auth_root() == env_auth.resolve()


def test_session_scope_supports_deep_state_dir_past_260_chars(tmp_path: Path) -> None:
    deep_state = tmp_path / ("segment-" + "x" * 60) / ("nested-" + "y" * 60) / ("leaf-" + "z" * 60)
    endpoint = Endpoint(host="192.0.2.30", port=22, root="/vllm-workspace")
    with session_scope("deep-path-session", state_dir=deep_state):
        written = write_read_ledger(
            endpoint,
            {"path": "/vllm-workspace/deep.py", "sha256": "a" * 64, "size": 42, "mtime_ns": 99},
        )
        assert len(os.path.abspath(written)) > 260
        loaded = load_read_ledger(endpoint, "/vllm-workspace/deep.py")
        assert loaded is not None
        assert loaded["sha256"] == "a" * 64

        job_id = "job-deep-001"
        rec_path = job_record_path(endpoint, job_id)
        record = {
            "schema_version": "remote-dev.job.v1",
            "job_id": job_id,
            "state": "running",
            "target": endpoint.to_result_target(),
        }
        atomic_write_json(rec_path, record)
        with record_lock(rec_path, timeout_seconds=2.0):
            job_records.save_output(
                record,
                rec_path,
                {"state": "running", "stdout": "deep log\n", "stderr": "", "stdout_offset": 9, "stderr_offset": 0},
            )
        _, reloaded, _ = job_records.load_record(endpoint, job_id)
        assert reloaded["local_output_offsets"]["stdout"] == 9
        assert job_records.persist_terminal_state(
            rec_path, reloaded, {"state": "succeeded", "quiet": True, "result": {"exit_code": 0}}
        ) is None

        prune_stats = maybe_prune_endpoint_state(endpoint, force=True)
        assert "error" not in prune_stats
        assert prune_stats["base"] == str(endpoint_state_dir(endpoint))


def test_serve_selected_propagates_session_scope_to_worker_and_control_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw_state = tmp_path / "gw-state"
    gw_auth = tmp_path / "gw-auth"
    env_keys = ("REMOTE_DEV_SESSION_ID", "REMOTE_DEV_STATE_DIR", "REMOTE_DEV_AUTH_DIR")
    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)
    env_snapshot = {key: os.environ.get(key) for key in env_keys}
    caller_thread = threading.current_thread()
    observed: dict[str, dict[str, object]] = {}
    observed_lock = threading.Lock()

    def in_process_handler(message: dict[str, object], _cancel: threading.Event | None) -> dict[str, object]:
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        tool_name = str(params.get("name") or method)
        with observed_lock:
            observed[tool_name] = {
                "thread_name": threading.current_thread().name,
                "different_thread": threading.current_thread() is not caller_thread,
                "scope": resolve_ledger_scope(),
                "state_root": state_root(),
                "auth_root": auth_root(),
                "env": {key: os.environ.get(key) for key in env_keys},
            }
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"ok": True}}

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "remote_bash", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "remote_read", "arguments": {}}},
    ]
    reader = io.StringIO("".join(json.dumps(req) + "\n" for req in requests))
    writer = io.StringIO()

    with session_scope("gw-sess", state_dir=gw_state, auth_dir=gw_auth):
        rc = serve_selected(
            lambda: (in_process_handler, {}),
            reader=reader,
            writer=writer,
            handler=in_process_handler,
        )

    assert rc == 0
    expected_scope = "id-" + hashlib.sha256(b"gw-sess").hexdigest()
    for tool_name, expected_prefix in (("remote_bash", "mindie-mcp-work"), ("remote_read", "mindie-mcp-ctrl")):
        entry = observed[tool_name]
        assert entry["different_thread"] is True
        assert str(entry["thread_name"]).startswith(expected_prefix)
        assert entry["scope"] == expected_scope
        assert entry["state_root"] == gw_state
        assert entry["auth_root"] == gw_auth.resolve()
        assert entry["env"] == env_snapshot
    assert {key: os.environ.get(key) for key in env_keys} == env_snapshot


def test_rpc_pool_key_isolates_scoped_auth_roots(tmp_path: Path) -> None:
    auth_a = tmp_path / "auth-a"
    auth_b = tmp_path / "auth-b"
    endpoint = Endpoint(host="192.0.2.40", port=22, root="/vllm-workspace")
    created_roots: list[Path] = []

    class _FakeConnection:
        def __init__(self, ep: Endpoint, *, timeout_ms: int | None = None) -> None:
            self.endpoint = ep
            self.closed = False
            self.proc = mock.Mock()
            self.proc.poll.return_value = None
            created_roots.append(auth_root())

        def request(self, _kind: str, _source: str, _payload: dict[str, object], _timeout_ms: int | None) -> dict[str, object]:
            return {"transport": {}, "auth_root": str(auth_root())}

        def close(self) -> None:
            self.closed = True

    rpc_transport.close_connections()
    try:
        with mock.patch.object(rpc_transport, "RpcConnection", side_effect=_FakeConnection):
            with session_scope("sess-a", auth_dir=auth_a):
                res_a1 = rpc_transport.request(endpoint, "python", "pass", {})
                res_a2 = rpc_transport.request(endpoint, "python", "pass", {})
            with session_scope("sess-b", auth_dir=auth_b):
                res_b = rpc_transport.request(endpoint, "python", "pass", {})
            assert len(created_roots) == 2
            assert created_roots == [auth_a.resolve(), auth_b.resolve()]
            assert res_a1["auth_root"] == str(auth_a.resolve())
            assert res_a2["auth_root"] == str(auth_a.resolve())
            assert res_b["auth_root"] == str(auth_b.resolve())
            assert len(rpc_transport._pool) == 2
    finally:
        rpc_transport.close_connections()


