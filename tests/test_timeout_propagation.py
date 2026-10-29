"""Timeouts must escape optional file/media fallbacks without retrying work."""
from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from remote_dev.core import job_logs, media_core, media_image, media_pdf, search_ops


def _timeout(*args, **kwargs):
    raise TimeoutError("injected I/O deadline")


def test_job_log_cache_timeout_is_not_reported_as_missing(monkeypatch):
    monkeypatch.setattr(Path, "stat", _timeout)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        job_logs.job_log_refs(job_id="job-timeout", record_path="/tmp/job-timeout.json", record={})


@pytest.mark.parametrize("operation", [
    lambda path: media_core._read_bounded(path, 1024, "rendered output"),
    media_image._probe_image_dimensions,
])
def test_media_file_timeout_is_not_missing_output(monkeypatch, operation):
    path = SimpleNamespace(stat=_timeout, read_bytes=_timeout)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        operation(path)


def test_image_renderer_timeout_escapes_without_trying_another_renderer(monkeypatch):
    calls = []

    def timed_out(*args, **kwargs):
        calls.append("first")
        _timeout()

    def unused(*args, **kwargs):
        calls.append("second")
        raise AssertionError("a timed-out renderer must not trigger another attempt")

    # Only header parsing precedes the renderer; no successful decode is simulated.
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 8, 8)
    monkeypatch.setattr(media_image, "_image_renderers", lambda: [("first", timed_out), ("second", unused)])
    with pytest.raises(TimeoutError, match="I/O deadline"):
        media_image.render_image(png_header, max_edge=4)
    assert calls == ["first"]


def test_pdf_renderer_timeout_escapes_without_trying_another_renderer(monkeypatch):
    monkeypatch.setattr(media_pdf, "_pdf_page_count", lambda *args: 1)
    monkeypatch.setattr(media_pdf, "_available_pdf_renderers", lambda: ["pdftoppm", "pypdfium2"])
    monkeypatch.setattr(media_pdf, "_render_pdf_pdftoppm", _timeout)

    def unused(*args, **kwargs):
        raise AssertionError("a timed-out renderer must not trigger another attempt")

    monkeypatch.setattr(media_pdf, "_render_pdf_pypdfium2", unused)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        media_pdf.render_pdf(b"%PDF-1.4\n/Type /Page\n")


def test_pdf_count_timeout_is_not_replaced_by_uncompressed_page_guess(monkeypatch):
    monkeypatch.setattr(media_pdf, "_which", lambda name: None)
    monkeypatch.setattr(media_pdf, "_module_available", lambda name: name == "pypdfium2")
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=_timeout))
    with pytest.raises(TimeoutError, match="I/O deadline"):
        media_pdf.pdf_page_count(b"%PDF-1.4\n/Type /Page\n")


@pytest.fixture
def search_worker(monkeypatch, capsys):
    # Load the shipped standalone worker in-process; the unsupported operation
    # defines helpers but performs no search or subprocess launch.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"op": "load-only", "root": "/"})))
    namespace = {}
    with pytest.raises(SystemExit):
        exec(compile(search_ops.REMOTE_SEARCH_PY, "search_worker.py", "exec"), namespace)
    capsys.readouterr()
    return namespace


def test_search_stat_timeout_is_not_used_as_zero_mtime(search_worker, monkeypatch):
    monkeypatch.setitem(search_worker, "os", SimpleNamespace(stat=_timeout))
    with pytest.raises(TimeoutError, match="I/O deadline"):
        search_worker["sort_files_by_mtime"](["a.log", "b.log"])


def test_search_resolution_timeout_does_not_drop_a_matching_file(search_worker, monkeypatch):
    base = SimpleNamespace(is_dir=lambda: True, resolve=lambda: Path("/workspace"))
    monkeypatch.setattr(Path, "resolve", _timeout)
    with pytest.raises(TimeoutError, match="I/O deadline"):
        search_worker["filter_gitignored_paths"](base, ["/workspace/match.txt"])
