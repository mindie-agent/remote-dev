"""[Role] Measure concurrent updates through the bounded record-lock contract.
[Invariants] All synthetic increments must survive; protected writes cannot overlap.
[Timeouts & Locks] Two bounded workers use 5s lock budgets; no network or sleeps.
[Side-Effect Policy] Temporary synthetic JSON and the explicit result path only.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from remote_dev.core.locking import record_lock  # noqa: E402


def main():
    iterations = 200
    with tempfile.TemporaryDirectory(prefix="record-benchmark-") as directory:
        record = Path(directory) / "record.json"
        record.write_text('{"completed":0}')

        def increment():
            for _ in range(iterations // 2):
                with record_lock(record, timeout_seconds=5):
                    value = json.loads(record.read_text())
                    value["completed"] += 1
                    record.write_text(json.dumps(value))

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(increment) for _ in range(2)]
            for task in tasks:
                task.result(timeout=10)
        elapsed = time.perf_counter() - start
        assert json.loads(record.read_text())["completed"] == iterations
    Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(json.dumps({
        "status": "pass", "metrics": [{
            "name": "two_thread_record_increment_latency", "unit": "us/op",
            "value": elapsed * 1e6 / iterations, "iterations": iterations,
        }],
    }) + "\n")


if __name__ == "__main__":
    main()
