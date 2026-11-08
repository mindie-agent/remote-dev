"""Job-log preview/incremental reads must not disguise OSError as empty output."""
from __future__ import annotations

import errno
from pathlib import Path

import pytest

import remote_dev.mcp.native_handlers_claude as claude_handlers
import remote_dev.mcp.native_common as native_common
from remote_dev.mcp.native_handlers_wb import _read_stream_incremental


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
