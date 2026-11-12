"""Catalog invalidation preserves the live connection and active generation."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from remote_dev.mcp.catalog_refresh import CatalogRefresh

FIXTURE = Path(__file__).parent / "fixtures/catalog_helper.py"


def test_selected_facade_refreshes_without_cancelling_or_replaying_active_call(tmp_path):
    pointer = tmp_path / "current.json"
    pointer.write_text('{"generation":"old"}')
    process = subprocess.Popen(
        [sys.executable, str(FIXTURE), str(tmp_path), "facade"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    replies = queue.Queue()
    reader = threading.Thread(
        target=lambda: [replies.put(json.loads(line)) for line in process.stdout], daemon=True)
    reader.start()

    def send(identifier, method):
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method}) + "\n").encode())
        process.stdin.flush()

    try:
        send(1, "initialize")
        assert replies.get(timeout=5)["result"]["capabilities"]["tools"]["listChanged"]
        send(2, "tools/list")
        assert replies.get(timeout=5)["result"]["tools"][0]["description"] == "old"
        send(3, "tools/call")
        deadline = time.monotonic() + 5
        while not (tmp_path / "started").exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        temporary = pointer.with_suffix(".tmp")
        temporary.write_text('{"generation":"new"}')
        temporary.replace(pointer)
        assert replies.get(timeout=4)["method"] == "notifications/tools/list_changed"
        send(4, "tools/list")
        assert replies.get(timeout=5)["result"]["tools"][0]["description"] == "new"
        (tmp_path / "release").touch()
        result = replies.get(timeout=5)
        assert result["id"] == 3
        assert result["result"]["content"][0]["text"] == "old"
        assert not result["result"]["isError"]
        send(5, "tools/call")
        assert replies.get(timeout=5)["result"]["content"][0]["text"] == "new"
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert calls == [{"id": 3, "generation": "old"}, {"id": 5, "generation": "new"}]
        process.stdin.close()
        assert process.wait(timeout=7) == 0, process.stderr.read().decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)


def test_failed_notification_remains_retryable_and_never_replays_work():
    current = ["before"]
    messages = []

    def send(message):
        if not messages:
            messages.append("failed")
            raise BrokenPipeError("temporarily blocked writer")
        messages.append(message)

    refresh = CatalogRefresh(lambda: current[0], send)
    current[0] = "after"
    assert not refresh.check()
    assert refresh.check()
    assert refresh.check()
    assert messages == ["failed", {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]
