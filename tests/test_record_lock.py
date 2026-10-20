"""A contended record deadline leaves the owner and later callers usable."""
from concurrent.futures import ThreadPoolExecutor
import time
from threading import Event

import pytest

from remote_dev.core.locking import record_lock


def test_record_deadline_does_not_enter_mutation_or_poison_lock(tmp_path):
    record = tmp_path / "inventory.json"
    held, release = Event(), Event()

    def owner():
        with record_lock(record):
            held.set()
            assert release.wait(2)
            record.write_text("owner completed")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner)
        assert held.wait(1)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="waiting for record lock"):
                with record_lock(record, timeout_seconds=0.02):
                    record.write_text("must not run")
            assert time.monotonic() - started < 0.3
            assert not record.exists()
        finally:
            release.set()
        future.result(timeout=1)
    with record_lock(record, timeout_seconds=0.1):
        assert record.read_text() == "owner completed"
        record.write_text("next caller completed")
    assert record.read_text() == "next caller completed"
