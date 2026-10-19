"""Live integration tests against real A2 (173.125.1.2) and A3 (192.168.13.165) NPU hosts & containers.

Opt-in via REMOTE_DEV_LIVE_NPU=1 so offline unit test runs remain hermetic.
All remote artifacts and job directories are confined to /tmp/remote-dev-live-test-<uuid>
and removed during teardown. Never connects to 192.168.13.153.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import pytest

from remote_dev.core import rpc_transport, ssh_transport
from remote_dev.core.endpoint import Endpoint, resolve_endpoint
from remote_dev.mcp.tools import call_tool

LIVE_ENABLED = os.environ.get("REMOTE_DEV_LIVE_NPU") == "1"
pytestmark = pytest.mark.live_npu

LIVE_TARGETS = (
    {
        "alias": "a2",
        "host": "173.125.1.2",
        "port": 22,
        "user": "root",
        "container": "mindie-bench-a2-core-20261017",
    },
    {
        "alias": "a3",
        "host": "192.168.13.165",
        "port": 22,
        "user": "root",
        "container": "vaws-offload-pd165-20261016",
    },
)


@unittest.skipUnless(LIVE_ENABLED, "set REMOTE_DEV_LIVE_NPU=1 to run live A2/A3 NPU integration tests")
class LiveNpuEndpointsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state_tmp = tempfile.TemporaryDirectory()
        cls.env_patcher = mock.patch.dict(
            os.environ,
            {
                "REMOTE_DEV_STATE_DIR": str(Path(cls.state_tmp.name) / "state"),
                "REMOTE_DEV_STOP_GRACE_SECONDS": "0.2",
            },
        )
        cls.env_patcher.start()
        cls.run_id = uuid.uuid4().hex[:12]
        cls.remote_dir = f"/tmp/remote-dev-live-test-{cls.run_id}"
        for target in LIVE_TARGETS:
            host_ep = Endpoint(
                host=target["host"],
                port=target["port"],
                user=target["user"],
                root="/tmp",
                cwd="/tmp",
                runtime_env=False,
            )
            ssh_transport.run_script(host_ep, f"mkdir -p {cls.remote_dir}")
            container_ep = Endpoint(
                host=target["host"],
                port=target["port"],
                user=target["user"],
                container=target["container"],
                root="/tmp",
                cwd="/tmp",
                runtime_env=False,
            )
            ssh_transport.run_script(container_ep, f"mkdir -p {cls.remote_dir}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            rpc_transport.close_connections()
            for target in LIVE_TARGETS:
                for container in (None, target["container"]):
                    ep = Endpoint(
                        host=target["host"],
                        port=target["port"],
                        user=target["user"],
                        container=container,
                        root="/tmp",
                        cwd="/tmp",
                        runtime_env=False,
                    )
                    try:
                        ssh_transport.run_script(ep, f"rm -rf {cls.remote_dir}", timeout_ms=10000)
                    except Exception:
                        pass
        finally:
            rpc_transport.close_connections()
            cls.env_patcher.stop()
            cls.state_tmp.cleanup()

    def _container_args(self, target: dict[str, object], subdir: str = "") -> dict[str, object]:
        work = f"{self.remote_dir}/{subdir}" if subdir else self.remote_dir
        return {
            "host": target["host"],
            "port": target["port"],
            "user": target["user"],
            "container": target["container"],
            "root": self.remote_dir,
            "cwd": work,
            "runtime_env": False,
        }

    def test_1_container_id_pinning_and_file_edit_search_patch_chain(self) -> None:
        for target in LIVE_TARGETS:
            with self.subTest(target=target["alias"]):
                base = self._container_args(target)
                mod_path = f"{self.remote_dir}/kernel_{target['alias']}.py"
                initial_code = (
                    "class NpuRunner:\n"
                    "    def forward(self, x):\n"
                    "        debug_line = 1\n"
                    "        return x + 1\n"
                )
                w_res = call_tool(
                    "remote.write",
                    {**base, "file_path": mod_path, "content": initial_code, "overwrite": True},
                )["result"]
                self.assertEqual(w_res["outcome"], "success", w_res)
                # Container selector is pinned to a full 64-hex container ID
                self.assertRegex(w_res["target"]["container"], r"^[0-9a-f]{64}$")
                self.assertEqual(w_res["target"]["container_selector"], target["container"])

                # Read with AST outline and node_paths
                outline_res = call_tool(
                    "remote.read",
                    {**base, "file_path": mod_path, "outline": True},
                )["result"]
                self.assertEqual(outline_res["outcome"], "success", outline_res)
                symbols = [item["qualified_name"] for item in outline_res["file"]["outline"]]
                self.assertIn("NpuRunner.forward", symbols)

                node_res = call_tool(
                    "remote.read",
                    {**base, "file_path": mod_path, "node_paths": ["NpuRunner.forward"]},
                )["result"]
                self.assertEqual(node_res["outcome"], "success", node_res)
                self.assertIn("return x + 1", node_res["preview"]["content"])

                # P0-1 verification on real container: delete line with new_string="" across aliases
                e_res = call_tool(
                    "remote_replace_file_content",
                    {
                        **base,
                        "TargetFile": mod_path,
                        "TargetContent": "        debug_line = 1\n",
                        "ReplacementContent": "",
                    },
                )["result"]
                self.assertEqual(e_res["outcome"], "success", e_res)

                r_res = call_tool("remote.read", {**base, "file_path": mod_path})["result"]
                self.assertNotIn("debug_line", r_res["preview"]["content"])
                self.assertIn("return x + 1", r_res["preview"]["content"])

                # Glob & Grep inside container
                glob_res = call_tool("remote.glob", {**base, "path": self.remote_dir, "pattern": "*.py"})["result"]
                self.assertEqual(glob_res["outcome"], "success", glob_res)
                self.assertTrue(any(mod_path in str(m) for m in glob_res["matches"]))

                grep_res = call_tool(
                    "remote.grep",
                    {**base, "path": self.remote_dir, "pattern": "NpuRunner", "output_mode": "content"},
                )["result"]
                self.assertEqual(grep_res["outcome"], "success", grep_res)
                self.assertTrue(grep_res["matches"])

                # Apply patch inside container
                patch_text = (
                    f"*** Begin Patch\n"
                    f"*** Update File: {mod_path}\n"
                    f"@@\n"
                    f" class NpuRunner:\n"
                    f"     def forward(self, x):\n"
                    f"-        return x + 1\n"
                    f"+        return x * 2\n"
                    f"*** End Patch\n"
                )
                p_res = call_tool("remote.apply_patch", {**base, "patch": patch_text})["result"]
                self.assertEqual(p_res["outcome"], "success", p_res)
                after_patch = call_tool("remote.read", {**base, "file_path": mod_path})["result"]
                self.assertIn("return x * 2", after_patch["preview"]["content"])

    def test_2_remote_cwd_stdlib_shadowing_immunity(self) -> None:
        for target in LIVE_TARGETS:
            with self.subTest(target=target["alias"]):
                shadow_dir = f"{self.remote_dir}/shadow_{target['alias']}"
                base = self._container_args(target, subdir=f"shadow_{target['alias']}")
                # Create shadow_dir and poison files platform.py, uuid.py, json.py
                for mod_name in ("platform.py", "uuid.py", "json.py"):
                    res = call_tool(
                        "remote.write",
                        {
                            **base,
                            "file_path": f"{shadow_dir}/{mod_name}",
                            "content": "raise RuntimeError('STDLIB_SHADOWED_IN_CWD')\n",
                            "create_dirs": True,
                            "overwrite": True,
                        },
                    )["result"]
                    self.assertEqual(res["outcome"], "success", res)

                # 1. rpc_worker + file_ops in poisoned cwd
                read_res = call_tool(
                    "remote.read",
                    {**base, "file_path": f"{shadow_dir}/platform.py"},
                )["result"]
                self.assertEqual(read_res["outcome"], "success", read_res)

                # 2. worker.py supervisor + remote.bash in poisoned cwd
                bash_res = call_tool(
                    "remote.bash",
                    {**base, "command": "pwd && printf shadow-ok", "yield_time_ms": 5000},
                )["result"]
                self.assertEqual(bash_res["outcome"], "success", bash_res)
                self.assertIn("shadow-ok", bash_res["preview"]["stdout"])

                # 3. probe with diagnose_connection=True (diagnostics.py CONNECTION_PROBE_SCRIPT)
                probe_res = call_tool(
                    "remote.probe",
                    {**base, "diagnose_connection": True},
                )["result"]
                self.assertEqual(probe_res["outcome"], "success", probe_res)
                self.assertEqual(probe_res["connection"]["probes"][0]["facts"]["system"], "Linux")

    def test_3_linux_subreaper_descendant_drain_and_process_worker_suite(self) -> None:
        for target in LIVE_TARGETS:
            with self.subTest(target=target["alias"]):
                base = self._container_args(target)
                pid_file = f"{self.remote_dir}/detached_{target['alias']}.pid"
                cmd = (
                    "setsid env -u REMOTE_DEV_JOB_TOKEN python3 -I -c "
                    f"\"import os,pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path('{pid_file}').write_text(str(os.getpid())); time.sleep(60)\" "
                    "& sleep 60"
                )
                started = call_tool(
                    "remote.bash",
                    {**base, "command": cmd, "run_in_background": True},
                )["result"]
                job_id = started["job_id"]
                try:
                    # Wait briefly for detached child to write its PID
                    deadline = time.monotonic() + 5.0
                    detached_pid = None
                    while time.monotonic() < deadline:
                        r = call_tool("remote.read", {**base, "file_path": pid_file})["result"]
                        content = r.get("preview", {}).get("content", "")
                        if r["outcome"] == "success" and content.strip():
                            m = re.search(r"\|\s*(\d+)", content)
                            if m:
                                detached_pid = int(m.group(1))
                                break
                        time.sleep(0.1)
                    self.assertIsNotNone(detached_pid, "detached setsid child did not write pid file")

                    stopped = call_tool("remote.job_stop", {**base, "job_id": job_id, "force": False})["result"]
                    self.assertEqual(stopped["outcome"], "cancelled", stopped)
                    self.assertEqual(stopped["status"], "cancelled", stopped)
                    self.assertTrue(stopped["quiet"], stopped)
                    self.assertTrue(stopped["descendants_drained"], stopped)

                    # Verify /proc/<detached_pid> is completely gone inside the container
                    check = call_tool(
                        "remote.bash",
                        {
                            **base,
                            "command": f"test ! -e /proc/{detached_pid} && printf GONE || printf ALIVE",
                            "yield_time_ms": 5000,
                        },
                    )["result"]
                    self.assertIn("GONE", check["preview"]["stdout"])
                finally:
                    call_tool("remote.job_stop", {**base, "job_id": job_id, "force": True})

        # Also run the full 18-test ProcessWorkerTests suite on real Linux (A2 host)
        # so the 18 Linux-only /proc + prctl subreaper tests skipped on macOS are verified on real Linux.
        a2_host = Endpoint(host="173.125.1.2", port=22, user="root", root=self.remote_dir, cwd=self.remote_dir, runtime_env=False)
        repo_root = Path(__file__).resolve().parents[1]
        remote_pkg = f"{self.remote_dir}/pkg"
        call_tool(
            "remote.artifact_push",
            {
                "host": "173.125.1.2",
                "port": 22,
                "user": "root",
                "root": self.remote_dir,
                "cwd": self.remote_dir,
                "runtime_env": False,
                "local_path": str(repo_root / "remote_dev" / "processes" / "worker.py"),
                "remote_path": f"{remote_pkg}/remote_dev/processes/worker.py",
            },
        )
        call_tool(
            "remote.write",
            {
                "host": "173.125.1.2",
                "port": 22,
                "user": "root",
                "root": self.remote_dir,
                "cwd": self.remote_dir,
                "runtime_env": False,
                "file_path": f"{remote_pkg}/remote_dev/__init__.py",
                "content": "",
                "create_dirs": True,
                "overwrite": True,
            },
        )
        call_tool(
            "remote.write",
            {
                "host": "173.125.1.2",
                "port": 22,
                "user": "root",
                "root": self.remote_dir,
                "cwd": self.remote_dir,
                "runtime_env": False,
                "file_path": f"{remote_pkg}/remote_dev/processes/__init__.py",
                "content": (
                    "from pathlib import Path\n"
                    "def worker_source() -> str:\n"
                    "    return Path(__file__).with_name('worker.py').read_text(encoding='utf-8')\n"
                ),
                "create_dirs": True,
                "overwrite": True,
            },
        )
        call_tool(
            "remote.artifact_push",
            {
                "host": "173.125.1.2",
                "port": 22,
                "user": "root",
                "root": self.remote_dir,
                "cwd": self.remote_dir,
                "runtime_env": False,
                "local_path": str(repo_root / "tests" / "test_process_worker.py"),
                "remote_path": f"{remote_pkg}/tests/test_process_worker.py",
            },
        )
        completed = ssh_transport.run_script(
            a2_host,
            f"PYTHONPATH={remote_pkg} REMOTE_DEV_STOP_GRACE_SECONDS=0.1 python3 {remote_pkg}/tests/test_process_worker.py -v",
            timeout_ms=30000,
        )
        self.assertEqual(completed.returncode, 0, f"Linux ProcessWorkerTests failed:\n{completed.stdout}\n{completed.stderr}")
        self.assertIn("OK", completed.stderr)

    def test_4_probe_context_snapshot_artifact_roundtrip_and_zero_arg_default_alias(self) -> None:
        with tempfile.TemporaryDirectory() as local_tmp:
            raw_payload = os.urandom(64 * 1024)
            expected_sha = hashlib.sha256(raw_payload).hexdigest()
            local_src = Path(local_tmp) / "weights.bin"
            local_src.write_bytes(raw_payload)

            for target in LIVE_TARGETS:
                with self.subTest(target=target["alias"]):
                    base = self._container_args(target)
                    snap = call_tool("remote.context_snapshot", {**base, "live_probe": True})["result"]
                    self.assertEqual(snap["outcome"], "success", snap)

                    remote_bin = f"{self.remote_dir}/weights_{target['alias']}.bin"
                    pushed = call_tool(
                        "remote.artifact_push",
                        {**base, "local_path": str(local_src), "remote_path": remote_bin},
                    )["result"]
                    self.assertEqual(pushed["outcome"], "success", pushed)

                    pull_dir = Path(local_tmp) / f"pulled_{target['alias']}"
                    pulled = call_tool(
                        "remote.artifact_pull",
                        {**base, "remote_path": remote_bin, "local_dir": str(pull_dir)},
                    )["result"]
                    self.assertEqual(pulled["outcome"], "success", pulled)
                    files = [p for p in pull_dir.rglob("*") if p.is_file() and p.name != "manifest.json"]
                    self.assertEqual(len(files), 1)
                    self.assertEqual(hashlib.sha256(files[0].read_bytes()).hexdigest(), expected_sha)

            # Verify zero-arg endpoint resolution from coordinator-style endpoints.json
            endpoints_file = Path(local_tmp) / "endpoints.json"
            endpoints_file.write_text(
                json.dumps(
                    {
                        "default_alias": "a2",
                        "endpoints": {
                            "a2": {
                                "host": "173.125.1.2",
                                "port": 22,
                                "user": "root",
                                "runtime_env": False,
                                "container": {
                                    "name": "mindie-bench-a2-core-20261017",
                                    "id": "b2bb20466bf7",
                                    "user": "root",
                                    "workdir": self.remote_dir,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"REMOTE_DEV_ENDPOINTS_FILE": str(endpoints_file)}):
                resolved = resolve_endpoint({})
                self.assertEqual(resolved.alias, "a2")
                self.assertEqual(resolved.root, self.remote_dir)
                self.assertEqual(resolved.effective_cwd, self.remote_dir)
                zero_arg_probe = call_tool("remote_probe", {})["result"]
                self.assertEqual(zero_arg_probe["outcome"], "success", zero_arg_probe)
                self.assertEqual(zero_arg_probe["target"]["alias"], "a2")
                self.assertRegex(zero_arg_probe["target"]["container"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
