"""Live integration tests against real A2 (173.125.1.2) and A3 (192.168.13.165) NPU hosts & containers.

Opt-in via REMOTE_DEV_LIVE_NPU=1 so offline unit test runs remain hermetic.
All remote artifacts and job directories are confined to
/tmp/mindie-npu-test-<uuid> and removed during teardown. Never connects to
192.168.13.153.

Tier-2 runtime shape:
- one session-scoped sandbox warmup per host/container;
- one task-local SSH ControlMaster directory under /tmp/mindie-npu-test-<uuid>;
- one pinned 64-hex container id per target, reused by every operation so the
  RPC pool is keyed on the same endpoint for the whole session;
- one child directory per test under the session sandbox;
- the Linux ProcessWorkerTests run in parallel isolated subprocesses on the
  selected Linux host while the other live cases execute; only the
  lost-supervisor case runs in a private PID/proc namespace so its intentional
  full-/proc fallback does not walk the host's unrelated process table.

REMOTE_DEV_LIVE_NPU_TARGETS=a2,a3 can narrow the selected targets for a
targeted debugging run (default: all standing targets). The Linux worker suite
uses REMOTE_DEV_LIVE_NPU_LINUX_HOST when set, otherwise A2 when selected,
otherwise the first selected target.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import warnings
from pathlib import Path
from unittest import mock

import pytest

from remote_dev.core import rpc_transport, ssh_transport
from remote_dev.core.container_endpoint import pin_container_endpoint
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

CASE_DIRS = (
    "test_1_container_chain",
    "test_2_stdlib_shadowing",
    "test_3_process_drain",
    "test_4_artifact_roundtrip",
)
SETUP_TIMEOUT_MS = 10000
WORKER_SUITE_TIMEOUT_MS = 60000
WORKER_SUITE_JOBS = "6"
WORKER_PACKAGE = "linux-worker"


def _selected_targets() -> tuple[dict[str, object], ...]:
    raw = os.environ.get("REMOTE_DEV_LIVE_NPU_TARGETS", "").strip()
    known = {str(target["alias"]) for target in LIVE_TARGETS}
    if not raw:
        return tuple(dict(target) for target in LIVE_TARGETS)
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(requested) - known)
    if unknown:
        raise RuntimeError(
            "REMOTE_DEV_LIVE_NPU_TARGETS contains unknown alias(es): " + ", ".join(unknown)
        )
    selected = tuple(
        dict(target) for target in LIVE_TARGETS if str(target["alias"]) in set(requested)
    )
    if not selected:
        raise RuntimeError("REMOTE_DEV_LIVE_NPU_TARGETS selected no standing targets")
    return selected


def _target_args(
    parent: str,
    target: dict[str, object],
    subdir: str | None = None,
    *,
    selector: bool = False,
    container: bool = True,
) -> dict[str, object]:
    cwd = f"{parent}/{subdir}" if subdir else parent
    args: dict[str, object] = {
        "host": target["host"],
        "port": target["port"],
        "user": target["user"],
        "root": parent,
        "cwd": cwd,
        "runtime_env": False,
    }
    if container:
        args["container"] = (
            target["container"] if selector else target.get("container_id", target["container"])
        )
    return args


class _WorkerSuiteRunner:
    """Run the uploaded Linux worker suite once, concurrently with live cases."""

    def __init__(self, endpoint: Endpoint, package_dir: str) -> None:
        self.endpoint = endpoint
        self.package_dir = package_dir
        self.result = None
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="live-linux-process-worker-suite",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            command = (
                f"cd {shlex.quote(self.package_dir)} && "
                f"PYTHONPATH={shlex.quote(self.package_dir)} "
                "PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "
                f"REMOTE_DEV_STOP_GRACE_SECONDS=0.1 REMOTE_DEV_WORKER_JOBS={WORKER_SUITE_JOBS} "
                f"nice -n 10 python3 "
                f"{shlex.quote(self.package_dir + '/tests/run_process_worker_tests.py')}"
            )
            self.result = ssh_transport.run_script(
                self.endpoint, command, timeout_ms=WORKER_SUITE_TIMEOUT_MS
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by join()
            self.error = exc

    def join(self, timeout: float = WORKER_SUITE_TIMEOUT_MS / 1000 + 15) -> object:
        if self._thread is None:
            raise AssertionError("Linux worker suite was never started")
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError(
                f"Linux worker suite still running after {timeout:.1f}s on "
                f"{self.endpoint.host}"
            )
        if self.error is not None:
            raise AssertionError(
                f"Linux worker suite failed on {self.endpoint.host}: {self.error!r}"
            ) from self.error
        return self.result


class _LiveSession:
    """One warm sandbox, RPC pool and ControlMaster for the whole live file."""

    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex[:12]
        self.local_root = Path(f"/tmp/mindie-npu-test-{self.run_id}")
        self.local_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.mux_dir = self.local_root / "mux"
        self.mux_dir.mkdir(mode=0o700)
        self.state_dir = self.local_root / "state"
        self.remote_root = f"/tmp/mindie-npu-test-{self.run_id}"
        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "REMOTE_DEV_STATE_DIR": str(self.state_dir),
                "REMOTE_DEV_STOP_GRACE_SECONDS": "0.2",
                "REMOTE_DEV_SSH_MUX": "1",
                "REMOTE_DEV_SSH_MUX_DIR": str(self.mux_dir),
            },
        )
        self.env_patcher.start()
        self._original_mux_dir = ssh_transport._MUX_DIR
        self._original_mux_ready = ssh_transport._MUX_READY
        # The module reads _MUX_DIR at import time; rebind it before any SSH.
        ssh_transport._MUX_DIR = self.mux_dir
        ssh_transport._MUX_READY = None
        self.targets: tuple[dict[str, object], ...] = ()
        self.worker_suite: _WorkerSuiteRunner | None = None
        self.worker_expected = 0
        self.worker_methods: tuple[str, ...] = ()
        self._closed = False

    def start(self) -> None:
        selected = _selected_targets()
        self.targets = selected
        for target in selected:
            self._prepare_target(target)
        self._prepare_worker_suite()
        assert self.worker_suite is not None
        self.worker_suite.start()

    def _host_args(self, target: dict[str, object]) -> dict[str, object]:
        return _target_args(self.remote_root, target, container=False)

    def _prepare_target(self, target: dict[str, object]) -> None:
        alias = str(target["alias"])
        host = Endpoint(
            host=str(target["host"]),
            port=int(target["port"]),
            user=str(target["user"]),
            root=self.remote_root,
            cwd=self.remote_root,
            runtime_env=False,
        )
        target["host_endpoint"] = host
        try:
            bootstrap = ssh_transport.run_script(
                host, "printf sandbox-ready", timeout_ms=SETUP_TIMEOUT_MS
            )
        except BaseException as exc:  # noqa: BLE001 - exact host/stage below
            raise RuntimeError(
                f"live setup failed for {alias}: host bootstrap ({target['host']})"
            ) from exc
        if bootstrap.returncode != 0 or "sandbox-ready" not in bootstrap.stdout:
            raise RuntimeError(
                f"live setup failed for {alias}: host bootstrap ({target['host']}) "
                f"rc={bootstrap.returncode} timed_out={bootstrap.timed_out} "
                f"stderr={bootstrap.stderr[-1000:]!r}"
            )

        selector_endpoint = Endpoint(
            host=str(target["host"]),
            port=int(target["port"]),
            user=str(target["user"]),
            container=str(target["container"]),
            root=self.remote_root,
            cwd=self.remote_root,
            runtime_env=False,
        )
        try:
            pinned = pin_container_endpoint(
                selector_endpoint, timeout_ms=SETUP_TIMEOUT_MS
            )
        except BaseException as exc:  # noqa: BLE001 - exact host/stage below
            raise RuntimeError(
                f"live setup failed for {alias}: container pin "
                f"({target['host']}, {target['container']})"
            ) from exc
        if pinned.container is None or not re.fullmatch(r"[0-9a-f]{64}", pinned.container):
            raise RuntimeError(
                f"live setup failed for {alias}: container pin returned "
                f"non-64-hex id {pinned.container!r}"
            )
        target["container_id"] = pinned.container
        container = Endpoint(
            host=str(target["host"]),
            port=int(target["port"]),
            user=str(target["user"]),
            container=pinned.container,
            root=self.remote_root,
            cwd=self.remote_root,
            runtime_env=False,
        )
        target["container_endpoint"] = container
        # Best-effort cleanup may run even if a later setup stage fails.
        target["sandbox_ready"] = True

        directories = " ".join(
            shlex.quote(f"{self.remote_root}/{name}") for name in CASE_DIRS
        )
        sandbox_script = f"mkdir -p {shlex.quote(self.remote_root)} {directories}"
        for endpoint, stage in ((host, "host sandbox"), (container, "container sandbox")):
            try:
                completed = ssh_transport.run_script(
                    endpoint, sandbox_script, timeout_ms=SETUP_TIMEOUT_MS
                )
            except BaseException as exc:  # noqa: BLE001 - exact host/stage below
                raise RuntimeError(
                    f"live setup failed for {alias}: {stage} ({target['host']})"
                ) from exc
            if completed.returncode != 0:
                raise RuntimeError(
                    f"live setup failed for {alias}: {stage} ({target['host']}) "
                    f"rc={completed.returncode} timed_out={completed.timed_out} "
                    f"stderr={completed.stderr[-1000:]!r}"
                )

        for endpoint, stage in ((host, "host RPC"), (container, "container RPC")):
            try:
                row = rpc_transport.request(
                    endpoint,
                    "python",
                    "import sys; sys.stdout.write('rpc-warm')",
                    {},
                    timeout_ms=SETUP_TIMEOUT_MS,
                )
            except BaseException as exc:  # noqa: BLE001 - exact host/stage below
                raise RuntimeError(
                    f"live setup failed for {alias}: {stage} ({target['host']})"
                ) from exc
            if row.get("returncode") != 0 or "rpc-warm" not in str(row.get("stdout", "")):
                raise RuntimeError(
                    f"live setup failed for {alias}: {stage} ({target['host']}) "
                    f"rc={row.get('returncode')} stderr={str(row.get('stderr', ''))[-1000:]!r}"
                )

    def _linux_target(self) -> dict[str, object]:
        requested = os.environ.get("REMOTE_DEV_LIVE_NPU_LINUX_HOST", "").strip()
        if requested:
            for target in self.targets:
                if str(target["alias"]) == requested:
                    return target
            raise RuntimeError(
                f"REMOTE_DEV_LIVE_NPU_LINUX_HOST={requested!r} is not a selected target"
            )
        for target in self.targets:
            if str(target["alias"]) == "a2":
                return target
        return self.targets[0]

    def _prepare_worker_suite(self) -> None:
        linux_target = self._linux_target()
        package_dir = f"{self.remote_root}/{WORKER_PACKAGE}"
        local_root = Path(__file__).resolve().parents[1]
        local_test = (local_root / "tests" / "test_process_worker.py").read_text(encoding="utf-8")
        helper = (
            Path(__file__).resolve().parent / "live" / "run_process_worker_tests.py"
        ).read_text(encoding="utf-8")
        worker_init = (
            "from pathlib import Path\n"
            "def worker_source() -> str:\n"
            "    return Path(__file__).with_name('worker.py').read_text(encoding='utf-8')\n"
        )
        files = (
            ("remote_dev/processes/worker.py", (local_root / "remote_dev" / "processes" / "worker.py").read_text(encoding="utf-8")),
            ("remote_dev/__init__.py", ""),
            ("remote_dev/processes/__init__.py", worker_init),
            ("tests/__init__.py", ""),
            ("tests/test_process_worker.py", local_test),
            ("tests/run_process_worker_tests.py", helper),
        )
        host_args = self._host_args(linux_target)
        for relpath, content in files:
            result = call_tool(
                "remote.write",
                {
                    **host_args,
                    "file_path": f"{package_dir}/{relpath}",
                    "content": content,
                    "create_dirs": True,
                    "overwrite": True,
                    "timeout_ms": SETUP_TIMEOUT_MS,
                },
            )["result"]
            if result.get("outcome") != "success":
                raise RuntimeError(
                    f"live setup failed for {linux_target['alias']}: worker upload "
                    f"{relpath}: {result.get('summary')}"
                )
            changed = result.get("changed_files") or []
            expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if not changed or changed[0].get("after_sha256") != expected_sha:
                raise RuntimeError(
                    f"live setup failed for {linux_target['alias']}: worker upload "
                    f"{relpath} sha mismatch"
                )
        self.worker_methods = tuple(
            re.findall(r"^    def (test_\w+)", local_test, flags=re.MULTILINE)
        )
        if not self.worker_methods:
            raise RuntimeError("live setup failed: no Linux worker test methods found")
        self.worker_expected = len(self.worker_methods)
        self.worker_suite = _WorkerSuiteRunner(
            linux_target["host_endpoint"], package_dir  # type: ignore[index]
        )

    def _shutdown_mux(self) -> None:
        for target in self.targets:
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ControlPath={self.mux_dir}/%C",
                "-O",
                "exit",
                "-p",
                str(target["port"]),
                f"{target['user']}@{target['host']}",
            ]
            try:
                subprocess.run(command, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.worker_suite is not None:
            try:
                self.worker_suite.join()
            except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                warnings.warn(f"Linux worker suite did not finish cleanly: {exc}", RuntimeWarning)
        try:
            rpc_transport.close_connections()
        except BaseException as exc:  # noqa: BLE001 - cleanup must continue
            warnings.warn(f"SSH RPC pool cleanup failed: {exc}", RuntimeWarning)
        for target in self.targets:
            if not target.get("sandbox_ready"):
                continue
            for endpoint in (
                target.get("host_endpoint"),
                target.get("container_endpoint"),
            ):
                if endpoint is None:
                    continue
                try:
                    ssh_transport.run_script(
                        endpoint,  # type: ignore[arg-type]
                        f"rm -rf {shlex.quote(self.remote_root)}",
                        timeout_ms=8000,
                    )
                except BaseException as exc:  # noqa: BLE001 - cleanup must continue
                    warnings.warn(
                        f"Remote sandbox cleanup failed on {target['alias']}: {exc}",
                        RuntimeWarning,
                    )
        self._shutdown_mux()
        ssh_transport._MUX_DIR = self._original_mux_dir
        ssh_transport._MUX_READY = self._original_mux_ready
        self.env_patcher.stop()
        shutil.rmtree(self.local_root, ignore_errors=True)


@unittest.skipUnless(LIVE_ENABLED, "set REMOTE_DEV_LIVE_NPU=1 to run live A2/A3 NPU integration tests")
class LiveNpuEndpointsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.session = _LiveSession()
        try:
            cls.session.start()
        except BaseException:
            cls.session.close()
            raise
        cls.targets = cls.session.targets
        cls.remote_dir = cls.session.remote_root

    @classmethod
    def tearDownClass(cls) -> None:
        cls.session.close()

    def _args(
        self,
        target: dict[str, object],
        subdir: str | None = None,
        *,
        selector: bool = False,
        container: bool = True,
    ) -> dict[str, object]:
        return _target_args(
            self.remote_dir, target, subdir, selector=selector, container=container
        )

    def test_1_container_id_pinning_and_file_edit_search_patch_chain(self) -> None:
        case = "test_1_container_chain"
        for target in self.targets:
            with self.subTest(target=target["alias"]):
                mod_path = f"{self.remote_dir}/{case}/kernel_{target['alias']}.py"
                initial_code = (
                    "class NpuRunner:\n"
                    "    def forward(self, x):\n"
                    "        debug_line = 1\n"
                    "        return x + 1\n"
                )
                # One named-selector write keeps the pinning assertion; every
                # subsequent operation reuses the cached 64-hex container id.
                w_res = call_tool(
                    "remote.write",
                    {
                        **self._args(target, selector=True),
                        "file_path": mod_path,
                        "content": initial_code,
                        "overwrite": True,
                        "timeout_ms": SETUP_TIMEOUT_MS,
                    },
                )["result"]
                self.assertEqual(w_res["outcome"], "success", w_res)
                # Container selector is pinned to a full 64-hex container ID
                self.assertRegex(w_res["target"]["container"], r"^[0-9a-f]{64}$")
                self.assertEqual(w_res["target"]["container_selector"], target["container"])
                self.assertEqual(w_res["target"]["container"], target["container_id"])
                self.assertEqual(
                    w_res["changed_files"][0]["after_sha256"],
                    hashlib.sha256(initial_code.encode("utf-8")).hexdigest(),
                )
                base = self._args(target, case)

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
                glob_res = call_tool(
                    "remote.glob",
                    {**base, "path": f"{self.remote_dir}/{case}", "pattern": "*.py"},
                )["result"]
                self.assertEqual(glob_res["outcome"], "success", glob_res)
                self.assertTrue(any(mod_path in str(m) for m in glob_res["matches"]))

                grep_res = call_tool(
                    "remote.grep",
                    {
                        **base,
                        "path": f"{self.remote_dir}/{case}",
                        "pattern": "NpuRunner",
                        "output_mode": "content",
                    },
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
        case = "test_2_stdlib_shadowing"
        for target in self.targets:
            with self.subTest(target=target["alias"]):
                shadow_dir = f"{self.remote_dir}/{case}/shadow_{target['alias']}"
                case_base = self._args(target, case)
                # Create shadow_dir and poison files platform.py, uuid.py, json.py
                for mod_name in ("platform.py", "uuid.py", "json.py"):
                    res = call_tool(
                        "remote.write",
                        {
                            **case_base,
                            "file_path": f"{shadow_dir}/{mod_name}",
                            "content": "raise RuntimeError('STDLIB_SHADOWED_IN_CWD')\n",
                            "create_dirs": True,
                            "overwrite": True,
                            "timeout_ms": SETUP_TIMEOUT_MS,
                        },
                    )["result"]
                    self.assertEqual(res["outcome"], "success", res)

                base = self._args(target, f"{case}/shadow_{target['alias']}")
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
        case = "test_3_process_drain"
        for target in self.targets:
            with self.subTest(target=target["alias"]):
                base = self._args(target, case)
                pid_file = f"{self.remote_dir}/{case}/detached_{target['alias']}.pid"
                cmd = (
                    "setsid env -u REMOTE_DEV_JOB_TOKEN python3 -I -c "
                    f"\"import os,pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path('{pid_file}').write_text(str(os.getpid())); time.sleep(60)\" "
                    "& sleep 60"
                )
                started = call_tool(
                    "remote.bash",
                    {**base, "command": cmd, "run_in_background": True},
                )["result"]
                self.assertEqual(started["outcome"], "success", started)
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
                    self.assertIsNotNone(
                        detached_pid, "detached setsid child did not write pid file"
                    )

                    stopped = call_tool(
                        "remote.job_stop", {**base, "job_id": job_id, "force": False}
                    )["result"]
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

        # The 26-test ProcessWorkerTests suite (26 = 24 Linux-only /proc +
        # prctl tests plus the 2 entrypoint tests) runs in isolated parallel
        # subprocesses on real Linux. Every test id is still executed and every
        # original assertion still applies; only scheduling is parallel.
        runner = self.session.worker_suite
        self.assertIsNotNone(runner, "Linux worker suite was not prepared")
        completed = runner.join()  # type: ignore[union-attr]
        self.assertIsNotNone(completed, "Linux worker suite produced no result")
        self.assertFalse(
            completed.timed_out,
            f"Linux worker suite timed out: {completed.stderr[-2000:]}",
        )
        self.assertEqual(
            completed.returncode,
            0,
            "Linux ProcessWorkerTests failed:\n"
            f"stdout:\n{completed.stdout[-12000:]}\n"
            f"stderr:\n{completed.stderr[-6000:]}",
        )
        output = completed.stdout + "\n" + completed.stderr
        summary = re.search(
            r"SUMMARY total=(\d+) passed=(\d+) failed=(\d+) skipped=(\d+) wall=([0-9.]+)",
            output,
        )
        self.assertIsNotNone(summary, f"missing worker summary:\n{output[-6000:]}")
        total, passed, failed, skipped, _wall = summary.groups()  # type: ignore[union-attr]
        self.assertEqual(int(failed), 0, f"worker failures:\n{output[-12000:]}")
        self.assertEqual(int(skipped), 0, f"worker skips are not passes:\n{output[-12000:]}")
        self.assertEqual(int(passed), self.session.worker_expected, output[-12000:])
        self.assertEqual(int(total), self.session.worker_expected, output[-12000:])
        for method in self.session.worker_methods:
            self.assertIn(method, output, f"worker test {method} did not report")

    def test_4_probe_context_snapshot_artifact_roundtrip_and_zero_arg_default_alias(self) -> None:
        case = "test_4_artifact_roundtrip"
        with tempfile.TemporaryDirectory() as local_tmp:
            raw_payload = os.urandom(64 * 1024)
            expected_sha = hashlib.sha256(raw_payload).hexdigest()
            local_src = Path(local_tmp) / "weights.bin"
            local_src.write_bytes(raw_payload)

            for target in self.targets:
                with self.subTest(target=target["alias"]):
                    base = self._args(target, case)
                    snap = call_tool("remote.context_snapshot", {**base, "live_probe": True})[
                        "result"
                    ]
                    self.assertEqual(snap["outcome"], "success", snap)

                    remote_bin = f"{self.remote_dir}/{case}/weights_{target['alias']}.bin"
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
                    files = [
                        p
                        for p in pull_dir.rglob("*")
                        if p.is_file() and p.name != "manifest.json"
                    ]
                    self.assertEqual(len(files), 1)
                    self.assertEqual(hashlib.sha256(files[0].read_bytes()).hexdigest(), expected_sha)

            # Verify zero-arg endpoint resolution from coordinator-style endpoints.json
            first = self.targets[0]
            endpoints_file = Path(local_tmp) / "endpoints.json"
            endpoints_file.write_text(
                json.dumps(
                    {
                        "default_alias": first["alias"],
                        "endpoints": {
                            first["alias"]: {
                                "host": first["host"],
                                "port": first["port"],
                                "user": first["user"],
                                "runtime_env": False,
                                "container": {
                                    "name": first["container"],
                                    "id": str(first["container_id"])[:12],
                                    "user": first["user"],
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
                self.assertEqual(resolved.alias, first["alias"])
                self.assertEqual(resolved.root, self.remote_dir)
                self.assertEqual(resolved.effective_cwd, self.remote_dir)
                zero_arg_probe = call_tool("remote_probe", {})["result"]
                self.assertEqual(zero_arg_probe["outcome"], "success", zero_arg_probe)
                self.assertEqual(zero_arg_probe["target"]["alias"], first["alias"])
                self.assertRegex(zero_arg_probe["target"]["container"], r"^[0-9a-f]{64}$")
                self.assertEqual(
                    zero_arg_probe["target"]["container"], first["container_id"]
                )


if __name__ == "__main__":
    unittest.main()
