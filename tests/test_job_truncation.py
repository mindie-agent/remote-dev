"""End-to-end truthful truncation accounting for rotated capped worker logs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _worker_source import load_worker_module
from remote_dev.core import job_logs, job_ops, job_results, state_store
from remote_dev.core.endpoint import Endpoint


@pytest.fixture(scope="module")
def worker():
    pytest.importorskip("fcntl")
    return load_worker_module()


def test_rotated_worker_payload_reaches_job_results_and_job_logs(tmp_path, monkeypatch, worker):
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "state"))
    endpoint = Endpoint(host="192.0.2.10", port=22, root="/srv/app", cwd="/srv/app")
    job_id = "job-rotated-0001"
    remote_dir = tmp_path / "remote" / ".remote-dev" / "jobs" / job_id
    remote_dir.mkdir(parents=True)
    payload = (
        "HEAD-MARKER\n"
        + "".join(f"line-{index:05d}\n" for index in range(500))
        + "TAIL-MARKER\n"
    ).encode("utf-8")
    with worker.CappedLog(remote_dir, "stdout", 1024) as log:
        log.write(payload)

    meta = json.loads((remote_dir / "logs.json").read_text(encoding="utf-8"))
    truncated = int(meta["stdout"]["truncated_head_bytes"])
    logical = int(meta["stdout"]["logical_bytes_written"])
    assert 0 < truncated < logical
    window = worker.read_log_window(remote_dir, "stdout", truncated, 1_000_000)
    retained = window["data"]
    assert window["start"] == truncated
    assert retained == payload[truncated:]

    row = {
        "state": "succeeded",
        "quiet": True,
        "result": {"state": "succeeded", "exit_code": 0},
        "stdout": retained.decode("utf-8"),
        "stderr": "",
        "stdout_offset": logical,
        "stderr_offset": 0,
        "stdout_bytes_remaining": 0,
        "stderr_bytes_remaining": 0,
        "stdout_logical_bytes": logical,
        "stderr_logical_bytes": 0,
        "stdout_truncated_head_bytes": truncated,
        "stderr_truncated_head_bytes": 0,
    }
    record_path = state_store.job_record_path(endpoint, job_id)
    record = {
        "schema_version": "remote-dev.job.v1",
        "job_id": job_id,
        "target": endpoint.to_result_target(),
        "cwd": endpoint.cwd,
        "remote_dir": str(remote_dir),
        "started_at": "2026-10-25T00:00:00Z",
        "state": "running",
        "stdin_cursors": {"stdout_offset": 0, "stderr_offset": 0},
    }
    state_store.atomic_write_json(record_path, record)
    job_ops._save_output(record, record_path, row)

    saved = json.loads(record_path.read_text(encoding="utf-8"))
    accounting = saved["output_logs"]["stdout"]
    assert accounting["truncated_head_bytes"] == truncated
    assert accounting["logical_bytes_written"] == logical
    assert accounting["last_offset"] == logical
    assert "output_logs" in job_results._public_job_record(saved)

    payload_result = job_results._session_result(
        endpoint, saved, record_path, row, tool="remote.bash",
        started="2026-10-25T00:00:00Z", start=0.0, budget=8192,
    )
    result = payload_result["result"]
    preview = result["output_preview"]
    assert preview["truncated"] is True
    assert preview["head_truncated_bytes"] == truncated
    assert preview["omitted_bytes"] == truncated
    assert preview["retained_ranges"]["stdout"] == [truncated, logical]
    assert any("head-truncated" in warning for warning in result["warnings"])
    refs = result["refs"]
    assert refs["remote_log_truncated"] is True
    assert refs["remote_retained_range"]["stdout"] == {
        "start": truncated,
        "end": logical,
        "truncated_head_bytes": truncated,
    }
    assert refs["remote_raw_stdout_segments"] == [
        f"{remote_dir}/stdout.log.prev",
        f"{remote_dir}/stdout.log",
    ]
    assert refs["local_cache_streams"]["stdout"]["status"] == "complete"
    assert refs["local_cache_streams"]["stdout"]["retained_expected_bytes"] == len(retained)
    assert "rotated away" in job_logs.render_job_refs_text(refs)

    tail_sources = {
        "head": {"stdout": retained.decode("utf-8"), "stderr": ""},
        "tail": {"stdout": retained[-200:].decode("utf-8"), "stderr": ""},
    }
    edge_payload = job_results._session_result(
        endpoint, saved, record_path, row, tool="remote.bash",
        started="2026-10-25T00:00:00Z", start=0.0, budget=8192,
        tail_sources=tail_sources,
    )
    # `preview_stream_edges` already spans the retained range; head loss must not
    # be added twice when the response is built from worker head/tail edges.
    assert edge_payload["result"]["output_preview"]["omitted_bytes"] == truncated

    # The local cache can itself be split across `.prev` + current; previews must
    # concatenate both segments instead of treating the current file as complete.
    local = record_path.with_name(record_path.stem + ".stdout.log")
    cached = local.read_bytes()
    split = len(cached) // 2
    previous = local.with_name(local.name + ".prev")
    previous.write_bytes(cached[:split])
    local.write_bytes(cached[split:])
    local_preview = job_logs.local_stream_preview(local)
    assert local_preview["bytes"] == len(cached)
    assert local_preview["text"] == cached.decode("utf-8")
    assert job_logs.local_cache_segments(local) == [previous, local]
