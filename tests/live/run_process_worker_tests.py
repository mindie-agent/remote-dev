#!/usr/bin/env python3
"""Run the Linux ProcessWorkerTests as isolated parallel subprocesses.

The live suite uploads this helper into its test-owned remote sandbox. It
discovers every test in ``tests.test_process_worker`` and runs each test id in
its own Python process. The suite's process-global mocks (``os.environ``,
``Path.iterdir``, signals) therefore cannot leak across tests, while the same
test bodies and assertions still execute. The lost-supervisor test runs in a
private PID/proc namespace so its intentional full-/proc fallback does not walk
the standing host's unrelated processes; every other test keeps the host's
normal /proc, pty and signal semantics. Only scheduling and that one isolation
boundary differ from a plain ``python -m unittest`` run.

Environment:
    REMOTE_DEV_WORKER_JOBS  concurrent test processes (default 6)
"""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The lost-supervisor assertion intentionally exercises the worker's full
# /proc fallback after the supervisor is killed. On a busy standing host that
# fallback walks >11k entries and dominates the whole live file. A private
# PID/proc namespace keeps exactly the same worker assertions while making the
# fallback observe only this test's own process family. All other tests keep
# the host's normal /proc, pty and signal semantics.
_NAMESPACED_SUFFIXES = (".test_lost_supervisor_cannot_report_quiet",)


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def discover_test_ids() -> list[str]:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_process_worker")
    return [case.id() for case in _iter_cases(suite)]


def run_one(test_id: str) -> tuple[str, int, float, str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("REMOTE_DEV_STOP_GRACE_SECONDS", "0.1")
    runner = (
        "import sys, unittest\n"
        "suite = unittest.TestLoader().loadTestsFromName(sys.argv[1])\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "if not result.wasSuccessful():\n"
        "    raise SystemExit(1)\n"
        "if result.skipped:\n"
        "    raise SystemExit(2)\n"
        "raise SystemExit(0)\n"
    )
    started = time.monotonic()
    command = [sys.executable, "-c", runner, test_id]
    if any(test_id.endswith(suffix) for suffix in _NAMESPACED_SUFFIXES):
        command = ["unshare", "--pid", "--fork", "--mount-proc", *command]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        return test_id, completed.returncode, time.monotonic() - started, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return test_id, 124, time.monotonic() - started, stdout, stderr + "\nTIMEOUT"
    except TimeoutError:
        raise
    except OSError as exc:
        return test_id, 127, time.monotonic() - started, "", f"{type(exc).__name__}: {exc}"


def main() -> int:
    test_ids = discover_test_ids()
    if not test_ids:
        print("SUMMARY total=0 passed=0 failed=0 wall=0.000")
        return 1
    jobs = max(1, int(os.environ.get("REMOTE_DEV_WORKER_JOBS", "6")))
    started = time.monotonic()
    results: list[tuple[str, int, float, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(run_one, test_id): test_id for test_id in test_ids}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            test_id, code, elapsed, stdout, stderr = result
            label = "PASS" if code == 0 else "SKIP" if code == 2 else "FAIL"
            print(f"{label} {test_id} {elapsed:.3f}s", flush=True)
            if code != 0:
                print(stdout, flush=True)
                print(stderr, file=sys.stderr, flush=True)
    failed = [result for result in results if result[1] not in (0, 2)]
    skipped = [result for result in results if result[1] == 2]
    passed = len(results) - len(failed) - len(skipped)
    wall = time.monotonic() - started
    print(
        f"SUMMARY total={len(results)} passed={passed} failed={len(failed)} "
        f"skipped={len(skipped)} wall={wall:.3f}"
    )
    return 1 if failed or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
