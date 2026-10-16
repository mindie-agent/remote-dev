"""Pool contention contracts; fake transports expose the blocking boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
from unittest import mock

import pytest

from remote_dev.core import rpc_transport as rpc
from remote_dev.core.cancellation import request_context
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError


class Connection:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.closed = False
        self.proc = mock.Mock()
        self.proc.poll.return_value = None

    def request(self, kind, source, payload, timeout_ms):
        if payload.get("entered"):
            payload["entered"].set()
            assert payload["release"].wait(5)
        return {"transport": {}, "host": self.endpoint.host}

    def close(self):
        self.closed = True


@pytest.fixture
def pool():
    rpc.close_connections()
    with mock.patch.object(rpc, "RpcConnection", side_effect=Connection) as factory:
        yield factory
        rpc.close_connections()


def invoke(endpoint, payload=None, **kwargs):
    return rpc.request(endpoint, "control", "source", payload or {}, **kwargs)


def test_same_endpoint_coalesces_without_blocking_other_endpoint(pool):
    endpoint = Endpoint(host="slow.example", port=22)
    opening, release = threading.Event(), threading.Event()
    def construct(target):
        if target.host == endpoint.host:
            opening.set()
            assert release.wait(5)
        return Connection(target)
    pool.side_effect = construct
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = executor.submit(invoke, endpoint)
        assert opening.wait(2)
        second = executor.submit(invoke, replace(endpoint, root="/sibling"))
        fast = executor.submit(invoke, replace(endpoint, host="fast.example"))
        try:
            assert fast.result(timeout=2)["host"] == "fast.example"
        finally:
            release.set()
        assert first.result(timeout=2)["host"] == endpoint.host
        assert second.result(timeout=2)["host"] == endpoint.host
    assert pool.call_count == 2


def test_idle_lru_is_evicted_without_interrupting_active_requests(pool):
    endpoint = Endpoint(host="active.example", port=22)
    entered, release = threading.Event(), threading.Event()
    with mock.patch.object(rpc, "_POOL_LIMIT", 2), ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(invoke, endpoint, {"entered": entered, "release": release})
        assert entered.wait(2)
        # Completing a sibling-root call must not make the shared transport
        # idle while the first root's request still owns its active reference.
        invoke(replace(endpoint, root="/sibling"))
        active_connection = next(iter(rpc._pool.values())).connection
        idle = replace(endpoint, host="idle.example")
        invoke(idle)
        idle_connection = next(item.connection for item in rpc._pool.values() if item.connection.endpoint == idle)
        try:
            for index in range(40):
                invoke(replace(endpoint, host=f"new-{index}.example"))
            assert len(rpc._pool) == 2
            assert idle_connection.closed
            assert not active_connection.closed
            assert not active.done()
        finally:
            release.set()
        active.result(timeout=2)


def test_known_full_pool_fails_before_send_and_preserves_existing_work(pool):
    endpoint = Endpoint(host="busy.example", port=22)
    other = replace(endpoint, host="next.example")
    entered, release = threading.Event(), threading.Event()
    with mock.patch.object(rpc, "_POOL_LIMIT", 1), ThreadPoolExecutor(max_workers=2) as executor:
        busy = executor.submit(invoke, endpoint, {"entered": entered, "release": release})
        assert entered.wait(2)
        try:
            with pytest.raises(RemoteExecutionError, match="capacity is in use.*not sent"):
                invoke(other, timeout_ms=30)
            event = threading.Event()
            event.set()
            with request_context(event), pytest.raises(RemoteExecutionError, match="cancelled.*not sent"):
                invoke(other)
            assert not busy.done()
        finally:
            release.set()
        busy.result(timeout=2)
        assert invoke(other)["host"] == other.host


def test_slow_close_does_not_block_unrelated_connection(pool):
    endpoint = Endpoint(host="old.example", port=22)
    invoke(endpoint)
    old = next(iter(rpc._pool.values())).connection
    closing, release = threading.Event(), threading.Event()
    def close():
        closing.set()
        assert release.wait(5)
        old.closed = True
    old.close = close
    with mock.patch.object(rpc, "_POOL_LIMIT", 2), ThreadPoolExecutor(max_workers=2) as executor:
        fast_endpoint = replace(endpoint, host="fast.example")
        invoke(fast_endpoint)
        evict = executor.submit(invoke, replace(endpoint, host="replace.example"))
        assert closing.wait(2)
        try:
            assert executor.submit(invoke, fast_endpoint).result(timeout=2)["host"] == fast_endpoint.host
        finally:
            release.set()
        evict.result(timeout=2)


def test_idle_expiration_does_not_select_busy_entries(pool):
    endpoint = Endpoint(host="idle.example", port=22)
    invoke(endpoint)
    with rpc._pool_lock:
        entry = next(iter(rpc._pool.values()))
        entry.active = 1
        assert rpc._idle_connections(entry.last_used + rpc._IDLE_SECONDS + 1) == []
        entry.active = 0
        assert rpc._idle_connections(entry.last_used + rpc._IDLE_SECONDS + 1) == [entry.connection]
    entry.connection.close()


def test_lru_uses_completion_order_when_clock_ticks_are_equal(pool):
    first = Endpoint(host="a.example", port=22)
    second = replace(first, host="z.example")
    with mock.patch.object(rpc, "_POOL_LIMIT", 2), mock.patch.object(rpc.time, "monotonic", return_value=1):
        invoke(first)
        invoke(second)
        entries = {entry.connection.endpoint.host: entry.connection for entry in rpc._pool.values()}
        invoke(first)  # Same timer tick, but now newer than the second entry.
        invoke(replace(first, host="third.example"))
        assert entries[second.host].closed
        assert not entries[first.host].closed
