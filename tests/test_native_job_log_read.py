"""Job-log preview/incremental reads must not disguise OSError as empty output."""
from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import remote_dev.core.state_store as state_store
import remote_dev.mcp.native_handlers_claude as claude_handlers
import remote_dev.mcp.native_common as native_common
import remote_dev.mcp.native_handlers_wb as wb_handlers
from remote_dev.mcp.native_handlers_wb import _load_wb_cursor, _read_stream_incremental, wb_task_output


def _timeout(*_args, **_kwargs):
    raise TimeoutError("injected I/O deadline")


def _permission(path: Path) -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", str(path))


def _forbidden_tail(*_args, **_kwargs):
    raise AssertionError("failed log read must not fall through to an empty tail")


def test_job_stream_preview_stat_timeout_is_not_empty_preview(tmp_path, monkeypatch):
    log = tmp_path / "job.stdout.log"
    log.write_text("visible-log\n", encoding="utf-8")
    original_stat = Path.stat

    def timed_out_stat(self, *args, **kwargs):
        if self == log:
            _timeout()
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", timed_out_stat)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        native_common._job_stream_preview(log, tail_reader=_forbidden_tail)


def test_job_stream_preview_stat_permission_is_not_empty_preview(tmp_path, monkeypatch):
    log = tmp_path / "job.stdout.log"
    log.write_text("visible-log\n", encoding="utf-8")
    original_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self == log:
            raise _permission(log)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(OSError) as caught:
        native_common._job_stream_preview(log, tail_reader=_forbidden_tail)
    assert caught.value.errno == errno.EACCES
    assert str(log) in str(caught.value)


def test_job_stream_preview_read_timeout_is_not_empty_preview(tmp_path, monkeypatch):
    log = tmp_path / "job.stdout.log"
    log.write_text("visible-log\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", _timeout)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        native_common._job_stream_preview(log, tail_reader=_forbidden_tail)


def test_job_stream_preview_read_permission_is_not_empty_preview(tmp_path, monkeypatch):
    log = tmp_path / "job.stdout.log"
    log.write_text("visible-log\n", encoding="utf-8")

    def denied_read(self):
        raise _permission(self)

    monkeypatch.setattr(Path, "read_bytes", denied_read)
    with pytest.raises(OSError) as caught:
        native_common._job_stream_preview(log, tail_reader=_forbidden_tail)
    assert caught.value.errno == errno.EACCES
    assert str(log) in str(caught.value)


def test_job_stream_preview_missing_file_stays_empty(tmp_path):
    missing = tmp_path / "missing.stdout.log"
    preview = native_common._job_stream_preview(missing)
    assert preview["text"] == ""
    assert preview["truncated"] is False


def test_job_stream_preview_readable_file_keeps_content(tmp_path):
    log = tmp_path / "job.stdout.log"
    log.write_text("hello-from-cache\n", encoding="utf-8")
    preview = native_common._job_stream_preview(log)
    assert "hello-from-cache" in preview["text"]


def test_read_stream_incremental_timeout_is_not_empty_delta(tmp_path, monkeypatch):
    record = tmp_path / "job.json"
    record.write_text("{}", encoding="utf-8")
    log = tmp_path / "job.stdout.log"
    log.write_text("real-output\n", encoding="utf-8")
    original_open = Path.open

    def timed_out_open(self, *args, **kwargs):
        if self == log:
            _timeout()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", timed_out_open)
    monkeypatch.setattr(claude_handlers, "_tail_text", _forbidden_tail)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        _read_stream_incremental(
            record, "stdout", 0, "", full_snapshot=False, full_preview_text="preview-text"
        )


def test_read_stream_incremental_permission_is_not_empty_delta(tmp_path, monkeypatch):
    record = tmp_path / "job.json"
    record.write_text("{}", encoding="utf-8")
    log = tmp_path / "job.stdout.log"
    log.write_text("real-output\n", encoding="utf-8")
    original_open = Path.open

    def denied_open(self, *args, **kwargs):
        if self == log:
            raise _permission(log)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(claude_handlers, "_tail_text", lambda *_a, **_k: ("", False))
    with pytest.raises(OSError) as caught:
        _read_stream_incremental(
            record, "stdout", 0, "", full_snapshot=False, full_preview_text="preview-text"
        )
    assert caught.value.errno == errno.EACCES
    assert str(log) in str(caught.value)


def test_read_stream_incremental_missing_log_stays_empty(tmp_path):
    record = tmp_path / "job.json"
    record.write_text("{}", encoding="utf-8")
    text, size, head = _read_stream_incremental(
        record, "stdout", 0, "", full_snapshot=False, full_preview_text=""
    )
    assert text == ""
    assert size == 0
    assert head == ""


def test_read_stream_incremental_readable_log_returns_delta(tmp_path):
    record = tmp_path / "job.json"
    record.write_text("{}", encoding="utf-8")
    log = tmp_path / "job.stdout.log"
    log.write_text("delta-line\n", encoding="utf-8")
    text, size, head = _read_stream_incremental(
        record, "stdout", 0, "", full_snapshot=False, full_preview_text="unused"
    )
    assert "delta-line" in text
    assert size == len("delta-line\n")
    assert head.startswith("delta-line")


def test_path_is_file_does_not_swallow_timeout_or_permission(tmp_path):
    log = tmp_path / "job.stdout.log"
    log.write_text("visible-log\n", encoding="utf-8")
    original_stat = Path.stat

    def timed_out_stat(self, *args, **kwargs):
        if self == log:
            _timeout()
        return original_stat(self, *args, **kwargs)

    def denied_stat(self, *args, **kwargs):
        if self == log:
            raise _permission(log)
        return original_stat(self, *args, **kwargs)

    with mock.patch.object(Path, "stat", timed_out_stat):
        with pytest.raises(TimeoutError, match="I/O deadline"):
            log.is_file()
    with mock.patch.object(Path, "stat", denied_stat):
        with pytest.raises(PermissionError):
            log.is_file()
    missing = tmp_path / "missing.stdout.log"
    assert missing.is_file() is False


def test_load_wb_cursor_timeout_is_not_treated_as_missing(tmp_path, monkeypatch):
    record_path = tmp_path / "job.json"
    record_path.write_text(json.dumps({"wb_output_offsets": {"stdout_offset": 9}}), encoding="utf-8")
    monkeypatch.setattr(state_store, "read_json", _timeout)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        _load_wb_cursor("job-1", record_path, {"wb_output_offsets": {"stdout_offset": 0}})


def test_load_wb_cursor_permission_is_not_treated_as_missing(tmp_path, monkeypatch):
    record_path = tmp_path / "job.json"
    record_path.write_text(json.dumps({"wb_output_offsets": {"stdout_offset": 9}}), encoding="utf-8")

    def denied(_path):
        raise _permission(record_path)

    monkeypatch.setattr(state_store, "read_json", denied)
    with pytest.raises(OSError) as caught:
        _load_wb_cursor("job-1", record_path, {"wb_output_offsets": {"stdout_offset": 0}})
    assert caught.value.errno == errno.EACCES
    assert str(record_path) in str(caught.value)


def test_load_wb_cursor_corrupt_json_is_not_treated_as_missing(tmp_path):
    record_path = tmp_path / "job.json"
    record_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="job record unreadable"):
        _load_wb_cursor("job-1", record_path, {"wb_output_offsets": {"stdout_offset": 0}})


def test_load_wb_cursor_missing_record_falls_back_to_memory(tmp_path):
    wb_handlers._WB_CURSORS.clear()
    missing = tmp_path / "missing-job.json"
    cursor = _load_wb_cursor(
        "job-1",
        missing,
        {"wb_output_offsets": {"stdout_offset": 12, "stderr_offset": 3, "stdout_head": "ab", "stderr_head": ""}},
    )
    assert cursor["stdout_offset"] == 12
    assert cursor["stderr_offset"] == 3
    assert cursor["stdout_head"] == "ab"


def test_load_wb_cursor_reads_persisted_offsets(tmp_path):
    wb_handlers._WB_CURSORS.clear()
    record_path = tmp_path / "job.json"
    record_path.write_text(
        json.dumps({"wb_output_offsets": {"stdout_offset": 21, "stderr_offset": 2, "stdout_head": "ok", "stderr_head": "e"}}),
        encoding="utf-8",
    )
    cursor = _load_wb_cursor("job-1", record_path, None)
    assert cursor == {"stdout_offset": 21, "stderr_offset": 2, "stdout_head": "ok", "stderr_head": "e"}


def _wb_output_payload(**result_fields):
    body = {
        "text": "<output>\nlog\n</output>",
        "result": {
            "task": {"output": "log"},
            "preview": {"stdout": "log", "stderr": ""},
        },
    }
    body["result"].update(result_fields)
    return body


def test_wb_cursor_commit_preserves_real_failed_outcome(tmp_path, monkeypatch):
    record_path = tmp_path / "job.json"
    record_path.write_text("{}", encoding="utf-8")
    payload = _wb_output_payload(outcome="failed", status="failed", summary="remote job failed.")
    monkeypatch.setattr(state_store, "find_job_record", lambda _job_id: (record_path, {"job_id": "j"}))
    monkeypatch.setattr(wb_handlers, "claude_task_output", lambda *_a, **_k: payload)
    monkeypatch.setattr(wb_handlers, "_save_wb_cursor", mock.Mock(side_effect=OSError("disk full")))
    out = wb_task_output("wb", SimpleNamespace(wire="TaskOutput"), {"task_id": "j"}, {}, lambda *_a, **_k: {})
    result = out["result"]
    assert result["status"] == "local_recording_failed"
    assert result["operation_completed"] is True
    assert result["execution_outcome"] == "failed"
    assert result["execution_status"] == "failed"
    assert result["outcome"] != "success"


def test_wb_cursor_commit_does_not_invent_success_receipt(tmp_path, monkeypatch):
    record_path = tmp_path / "job.json"
    record_path.write_text("{}", encoding="utf-8")
    payload = _wb_output_payload()
    monkeypatch.setattr(state_store, "find_job_record", lambda _job_id: (record_path, {"job_id": "j"}))
    monkeypatch.setattr(wb_handlers, "claude_task_output", lambda *_a, **_k: payload)
    monkeypatch.setattr(wb_handlers, "_save_wb_cursor", mock.Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        wb_task_output("wb", SimpleNamespace(wire="TaskOutput"), {"task_id": "j"}, {}, lambda *_a, **_k: {})
