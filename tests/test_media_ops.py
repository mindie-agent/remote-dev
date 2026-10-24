"""Real local renderer coverage for ``remote_dev.core.media_ops``.

Fixtures are generated with the standard library and then rendered by the real
local tools available on this host (``sips``, ``pdftoppm``, ``qlmanage``). No
MagicMock is used as evidence that a renderer produced an image.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Callable

import pytest

from remote_dev.core import media_ops


def _image_renderer_available() -> bool:
    return bool(
        shutil.which("sips")
        or shutil.which("magick")
        or (os.name != "nt" and shutil.which("convert"))
        or importlib.util.find_spec("PIL")
    )


def _pdf_renderer_available() -> bool:
    return bool(
        shutil.which("pdftoppm")
        or importlib.util.find_spec("pypdfium2")
        or importlib.util.find_spec("fitz")
    )


requires_image_renderer = pytest.mark.skipif(
    not _image_renderer_available(), reason="no local image renderer"
)
requires_pdf_renderer = pytest.mark.skipif(
    not _pdf_renderer_available(), reason="no local PDF renderer"
)


# ---------------------------------------------------------------------------
# Standard-library PNG writer/prober used by the assertions
# ---------------------------------------------------------------------------


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_bytes(
    width: int,
    height: int,
    color: Callable[[int, int], tuple[int, int, int]],
) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type None
        for x in range(width):
            rows.extend(color(x, y))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def _quadrant_color(x: int, y: int) -> tuple[int, int, int]:
    if x < 4 and y < 3:
        return (255, 0, 0)
    if x >= 4 and y < 3:
        return (0, 255, 0)
    if x < 4:
        return (0, 0, 255)
    return (255, 255, 255)


def _gradient_color(x: int, y: int) -> tuple[int, int, int]:
    return ((x * 7) % 256, (y * 5) % 256, 128)


def _paeth(a: int, b: int, c: int) -> int:
    estimate = a + b - c
    distance_a = abs(estimate - a)
    distance_b = abs(estimate - b)
    distance_c = abs(estimate - c)
    if distance_a <= distance_b and distance_a <= distance_c:
        return a
    if distance_b <= distance_c:
        return b
    return c


def _decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, ...]]]]:
    """Decode an 8-bit non-interlaced PNG (RGB/RGBA/gray) enough for assertions."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"
    offset = 8
    header: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    assert header is not None
    width, height, depth, color_type, _, _, interlace = header
    assert depth == 8, f"unexpected bit depth {depth}"
    assert interlace == 0, "interlaced PNG is not supported by this test decoder"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    rows: list[list[tuple[int, ...]]] = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        scanline = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for index in range(stride):
            left = scanline[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 0xFF
            elif filter_type == 2:
                scanline[index] = (scanline[index] + above) & 0xFF
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                scanline[index] = (
                    scanline[index] + _paeth(left, above, upper_left)
                ) & 0xFF
            else:
                assert filter_type == 0, f"unexpected PNG filter {filter_type}"
        rows.append(
            [
                tuple(scanline[index : index + channels])
                for index in range(0, stride, channels)
            ]
        )
        previous = scanline
    return width, height, rows


def _sips_convert(source: bytes, suffix: str, destination: Path) -> Path:
    sips = shutil.which("sips")
    if not sips:
        pytest.skip("sips is not available")
    # sips infers the input format from the source and the output from the flag.
    input_path = destination.with_suffix(".input")
    input_path.write_bytes(source)
    completed = subprocess.run(
        [sips, "-s", "format", suffix, str(input_path), "--out", str(destination)],
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return destination


# ---------------------------------------------------------------------------
# Minimal valid multi-page PDF fixture
# ---------------------------------------------------------------------------


def _pdf_bytes(page_count: int = 3, width: int = 200, height: int = 100) -> bytes:
    objects: dict[int, bytes] = {}
    kids = " ".join(f"{3 + index} 0 R" for index in range(page_count))
    contents_object = 3 + page_count
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")
    )
    for index in range(page_count):
        objects[3 + index] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Contents {contents_object} 0 R /Resources << >> >>"
        ).encode("ascii")
    objects[contents_object] = b"<< /Length 0 >>\nstream\n\nendstream"
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for number in sorted(objects):
        output.extend(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point mkdtemp at an inspectable directory so temp cleanup is observable."""
    scratch = tmp_path / "mkdtemp-root"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def _notebook() -> dict:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3"}},
        "cells": [
            {
                "id": "intro",
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Hello\n", "world"],
            },
            {
                "id": "calc",
                "cell_type": "code",
                "metadata": {},
                "execution_count": 2,
                "source": ["print(1)"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": ["1\n"],
                    },
                    {
                        "output_type": "execute_result",
                        "execution_count": 2,
                        "data": {"text/plain": ["2"]},
                        "metadata": {},
                    },
                    {
                        "output_type": "display_data",
                        "data": {"text/markdown": ["**bold**"]},
                        "metadata": {},
                    },
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "bad",
                        "traceback": ["line one", "line two"],
                    },
                ],
            },
            {
                "cell_type": "raw",
                "metadata": {},
                "source": "raw text",
            },
        ],
    }


def _notebook_bytes() -> bytes:
    return json.dumps(_notebook(), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@requires_image_renderer
def test_render_image_crop_is_exact_and_probes_output(scratch_temp: Path) -> None:
    source = _png_bytes(8, 6, _quadrant_color)
    result = media_ops.render_image(
        source,
        mime_type="image/png",
        region={"x": 4, "y": 0, "width": 4, "height": 3},
        max_edge=None,
    )
    assert result["status"] == "ok", result
    assert result["cropped"] is True
    assert result["resized"] is False
    assert (result["width"], result["height"]) == (4, 3)
    assert (result["original_width"], result["original_height"]) == (8, 6)
    assert result["mime_type"] == "image/png"
    width, height, pixels = _decode_png(result["data"])
    assert (width, height) == (4, 3)
    # Crop offset (x=4, y=0) must land exactly on the green top-right quadrant.
    assert all(pixel[:3] == (0, 255, 0) for row in pixels for pixel in row)
    assert list(scratch_temp.iterdir()) == []


def test_render_image_identity_crop_of_tiny_png() -> None:
    source = _png_bytes(1, 1, lambda x, y: (10, 20, 30))
    result = media_ops.render_image(
        source,
        region={"x": 0, "y": 0, "width": 1, "height": 1},
        max_edge=None,
    )
    assert result["status"] == "ok", result
    assert result["cropped"] is True
    assert result["resized"] is False
    assert (result["width"], result["height"]) == (1, 1)
    assert result["data"] == source


@pytest.mark.skipif(not shutil.which("sips"), reason="sips is unavailable")
def test_render_image_jpeg_and_gif_local_conversion(scratch_temp: Path, tmp_path: Path) -> None:
    png = _png_bytes(100, 50, _gradient_color)
    jpeg_path = _sips_convert(png, "jpeg", tmp_path / "fixture.jpg")
    jpeg_result = media_ops.render_image(jpeg_path.read_bytes(), max_edge=40)
    assert jpeg_result["status"] == "ok", jpeg_result
    assert jpeg_result["mime_type"] == "image/jpeg"
    assert (jpeg_result["width"], jpeg_result["height"]) == (40, 20)
    width, height, _ = _decode_png(
        _sips_convert(
            jpeg_result["data"], "png", tmp_path / "jpeg-as-png.png"
        ).read_bytes()
    )
    assert (width, height) == (40, 20)

    gif_path = _sips_convert(png, "gif", tmp_path / "fixture.gif")
    gif_result = media_ops.render_image(gif_path.read_bytes(), max_edge=40)
    assert gif_result["status"] == "ok", gif_result
    assert gif_result["mime_type"] == "image/png"
    assert (gif_result["width"], gif_result["height"]) == (40, 20)
    assert gif_result["data"].startswith(b"\x89PNG\r\n\x1a\n")
    assert list(scratch_temp.iterdir()) == []


@requires_image_renderer
def test_render_image_downscale_and_full_resolution(scratch_temp: Path) -> None:
    source = _png_bytes(100, 50, _gradient_color)
    scaled = media_ops.render_image(source, max_edge=40)
    assert scaled["status"] == "ok", scaled
    assert scaled["resized"] is True
    assert scaled["cropped"] is False
    assert (scaled["width"], scaled["height"]) == (40, 20)
    assert (scaled["original_width"], scaled["original_height"]) == (100, 50)
    width, height, _ = _decode_png(scaled["data"])
    assert (width, height) == (scaled["width"], scaled["height"])

    full = media_ops.render_image(source, max_edge=None)
    assert full["status"] == "ok", full
    assert full["resized"] is False
    assert full["cropped"] is False
    assert (full["width"], full["height"]) == (100, 50)
    assert full["data"] == source
    assert list(scratch_temp.iterdir()) == []


@pytest.mark.parametrize(
    "region",
    [
        {"x": 90, "y": 0, "width": 20, "height": 10},
        {"x": -1, "y": 0, "width": 5, "height": 5},
        {"x": 0, "y": 0, "width": 0, "height": 5},
        {"x": "0", "y": 0, "width": 5, "height": 5},
        {"x": 0, "y": 0, "width": 5},
        "not-a-region",
    ],
)
def test_render_image_invalid_region(region: object) -> None:
    source = _png_bytes(8, 6, _quadrant_color)
    result = media_ops.render_image(source, region=region, max_edge=None)
    assert result["status"] == "failed"
    assert "region" in result["error"]


def test_render_image_rejects_oversized_and_unknown_input() -> None:
    oversized = b"\x00" * (media_ops.MAX_IMAGE_BYTES + 1)
    result = media_ops.render_image(oversized)
    assert result["status"] == "failed"
    assert "over the" in result["error"]

    result = media_ops.render_image(b"definitely not an image")
    assert result["status"] == "failed"
    assert "PNG" in result["error"] and "WebP" in result["error"]

    result = media_ops.render_image(b"")
    assert result["status"] == "failed"


def test_render_image_renderer_failure_cleans_exact_temp_dir(scratch_temp: Path) -> None:
    # Valid PNG header/dimensions but no IDAT payload: sips must fail, and no
    # fallback renderer is available in this environment.
    truncated = _png_bytes(8, 6, _quadrant_color)[:33]
    result = media_ops.render_image(truncated, max_edge=4)
    assert result["status"] == "failed"
    assert "image rendering failed" in result["error"]
    assert list(scratch_temp.iterdir()) == []


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


@requires_pdf_renderer
def test_render_pdf_page_range_scaling_and_probe(scratch_temp: Path) -> None:
    pdf = _pdf_bytes(3)
    result = media_ops.render_pdf(pdf, pages="1-2", max_pages=20, max_edge=64)
    assert result["status"] == "ok", result
    assert result["count"] == 2
    assert result["renderer"] in {"pdftoppm", "pypdfium2", "pymupdf"}
    assert [page["page"] for page in result["pages"]] == [1, 2]
    for page in result["pages"]:
        assert page["mime_type"] == "image/png"
        assert page["data"].startswith(b"\x89PNG\r\n\x1a\n")
        width, height, _ = _decode_png(page["data"])
        assert (width, height) == (page["width"], page["height"])
        assert 63 <= max(width, height) <= 64
    assert list(scratch_temp.iterdir()) == []


@requires_pdf_renderer
def test_render_pdf_single_page_and_default_selection(scratch_temp: Path) -> None:
    pdf = _pdf_bytes(3)
    single = media_ops.render_pdf(pdf, pages="2", max_pages=20, max_edge=32)
    assert single["status"] == "ok", single
    assert single["count"] == 1
    assert single["pages"][0]["page"] == 2

    default = media_ops.render_pdf(pdf, pages=None, max_pages=2, max_edge=32)
    assert default["status"] == "ok", default
    assert [page["page"] for page in default["pages"]] == [1, 2]
    assert list(scratch_temp.iterdir()) == []


@pytest.mark.parametrize(
    "pages",
    ["0", "4", "3-2", "1-3", "abc", "1-", "1,,2", ""],
)
def test_render_pdf_rejects_invalid_or_over_budget_pages(pages: str) -> None:
    pdf = _pdf_bytes(3)
    result = media_ops.render_pdf(pdf, pages=pages, max_pages=2, max_edge=32)
    if pages == "":
        # Empty means "default selection", which is bounded by max_pages.
        assert result["status"] == "ok", result
        assert result["count"] == 2
    else:
        assert result["status"] == "failed"
        assert "page" in result["error"].lower()


def test_render_pdf_missing_renderer_is_explicit(
    scratch_temp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(media_ops, "_module_available", lambda name: False)
    result = media_ops.render_pdf(_pdf_bytes(1))
    assert result["status"] == "failed"
    assert "renderer" in result["error"]
    assert "pdftoppm" in result["error"]
    assert list(scratch_temp.iterdir()) == []


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------


def test_read_notebook_structured_cells_and_text_fallback() -> None:
    result = media_ops.read_notebook(_notebook_bytes())
    assert result["status"] == "ok", result
    assert result["count"] == 3
    assert [cell["id"] for cell in result["cells"]] == ["intro", "calc", "cell-2"]
    assert result["cells"][0]["cell_type"] == "markdown"
    assert result["cells"][0]["source"] == "# Hello\nworld"
    assert result["cells"][0]["outputs"] == []
    assert result["cells"][1]["source"] == "print(1)"
    outputs = result["cells"][1]["outputs"]
    assert "1\n" in outputs
    assert "2" in outputs
    assert "**bold**" in outputs
    assert any("ValueError: bad" in output for output in outputs)
    assert "# Cell 0 [markdown] id=intro" in result["text"]
    assert "print(1)" in result["text"]
    assert "ValueError: bad" in result["text"]


def test_read_notebook_invalid_payloads() -> None:
    assert media_ops.read_notebook(b"not json")["status"] == "failed"
    assert media_ops.read_notebook(b"[]")["status"] == "failed"
    assert media_ops.read_notebook(b'{"nbformat": 4}')["status"] == "failed"


def test_edit_notebook_replace_preserves_id_and_is_canonical() -> None:
    first = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="print(2)",
        cell_id="calc",
        cell_type=None,
        edit_mode="replace",
    )
    second = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="print(2)",
        cell_id="calc",
        cell_type=None,
        edit_mode="replace",
    )
    assert first["status"] == "ok", first
    assert first["count"] == 3
    assert first["data"] == second["data"]
    assert first["data"].endswith(b"\n")
    notebook = json.loads(first["data"])
    assert notebook["cells"][1]["id"] == "calc"
    assert notebook["cells"][1]["source"] == ["print(2)"]
    assert [cell["id"] for cell in first["cells"]] == ["intro", "calc", "cell-2"]


def test_edit_notebook_insert_after_anchor_and_at_beginning() -> None:
    after = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="# inserted",
        cell_id="intro",
        cell_type="markdown",
        edit_mode="insert",
    )
    assert after["status"] == "ok", after
    assert after["count"] == 4
    ids = [cell["id"] for cell in after["cells"]]
    assert ids[0] == "intro"
    assert ids[2] == "calc"
    assert ids[1] not in {"intro", "calc", "cell-2"}
    assert after["cells"][1]["source"] == "# inserted"

    beginning = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="x = 1",
        cell_id=None,
        cell_type="code",
        edit_mode="insert",
    )
    assert beginning["status"] == "ok", beginning
    assert beginning["count"] == 4
    assert beginning["cells"][0]["source"] == "x = 1"
    assert beginning["cells"][0]["cell_type"] == "code"
    assert beginning["cells"][0]["id"] not in {"intro", "calc", "cell-2"}
    raw = json.loads(beginning["data"])
    assert raw["cells"][0]["outputs"] == []
    assert raw["cells"][0]["execution_count"] is None


def test_edit_notebook_delete() -> None:
    result = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="",
        cell_id="calc",
        cell_type=None,
        edit_mode="delete",
    )
    assert result["status"] == "ok", result
    assert result["count"] == 2
    assert all(cell["id"] != "calc" for cell in result["cells"])
    notebook = json.loads(result["data"])
    assert [cell.get("id", f"cell-{index}") for index, cell in enumerate(notebook["cells"])] == [
        "intro",
        "cell-1",
    ]


@pytest.mark.parametrize(
    ("cell_id", "cell_type", "edit_mode", "message"),
    [
        (None, None, "delete", "cell_id"),
        ("missing", None, "replace", "not found"),
        ("missing", None, "delete", "not found"),
        (None, None, "insert", "cell_type"),
        ("intro", "not-a-type", "insert", "cell_type"),
        ("intro", None, "bogus", "edit_mode"),
    ],
)
def test_edit_notebook_rejects_invalid_edits(
    cell_id: str | None,
    cell_type: str | None,
    edit_mode: str,
    message: str,
) -> None:
    result = media_ops.edit_notebook(
        _notebook_bytes(),
        new_source="whatever",
        cell_id=cell_id,
        cell_type=cell_type,
        edit_mode=edit_mode,
    )
    assert result["status"] == "failed"
    assert message in result["error"]


# ---------------------------------------------------------------------------
# Video thumbnail fallback
# ---------------------------------------------------------------------------
# 1-frame 160x90 H.264 MP4 generated locally with AVFoundation, captured here so
# the QuickLook/ffmpeg path is exercised without a test-time media toolchain.
TINY_MP4 = base64.b64decode(
    "AAAAHGZ0eXBtcDQyAAAAAWlzb21tcDQxbXA0MgAAAAFtZGF0AAAAAAAAAOcAAAA1BgUtR1ZK3FxMQz+U78URPNFDqAEAAAMAAQMA"
    "AAMAAQIAAeYACwAAAwAAAwAAERwMA5ErAYAAAACaJbggH94U2ez/zZe2WrFcDQyQ5S3kLOUXn49m4GQfvPXWUOX78rwK5VEzzHJG"
    "2AsTZ+JIAIlO0SQep9wEs5S55cTiMhAf6s1WK6YzpN5WeI5sPj9K+c91BKTBFSoLqGgAAAMCnSMJrTLfaOXMi8PbwYAAG6xNtAaG"
    "7fzw45VlreM6wzQnQQStaJ0WpwfoRqbg1AAAG+7XxutCsAAAAqZtb292AAAAbG12aGQAAAAA5wK4WucCuFsAAAJYAAACWAABAAAB"
    "AAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC"
    "AAACMnRyYWsAAABcdGtoZAAAAAHnArhb5wK4WwAAAAEAAAAAAAACWAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEA"
    "AAAAAAAAAAAAAAAAAEAAAAAAoAAAAFoAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAlgAAAAAAAEAAAAAAaptZGlhAAAAIG1k"
    "aGQAAAAA5wK4W+cCuFsAAAJYAAAAKFXEAAAAAAAxaGRscgAAAAAAAAAAdmlkZQAAAAAAAAAAAAAAAENvcmUgTWVkaWEgVmlkZW8A"
    "AAABUW1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAARFzdGJsAAAA"
    "oHN0c2QAAAAAAAAAAQAAAJBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAKAAWgBIAAAASAAAAAAAAAABAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//AAAAJmF2Y0MBZAAL/+EACydkAAusVlDDeEZEAQAEKO48sP34+AAAAAAKZmllbAEAAAAA"
    "CmNocm0AAAAAABhzdHRzAAAAAAAAAAEAAAABAAAAKAAAAA1zZHRwAAAAACAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAA"
    "FHN0c3oAAAAAAAAA1wAAAAEAAAAUc3RjbwAAAAAAAAABAAAALA=="
)



def test_video_thumbnail_rejects_non_video_and_missing_renderer(
    scratch_temp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert media_ops.render_video_thumbnail(b"not a video")["status"] == "failed"

    fake_mp4 = (
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
        + b"\x00" * 64
    )
    empty_bin = tmp_path / "empty-video-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    result = media_ops.render_video_thumbnail(fake_mp4, max_edge=128)
    assert result["status"] == "failed"
    assert "renderer" in result["error"]
    assert "ffmpeg" in result["error"] and "qlmanage" in result["error"]
    # No renderer was available, so no temporary directory is leaked either.
    assert list(scratch_temp.iterdir()) == []


@pytest.mark.parametrize("max_edge", [0, None, True])
def test_video_thumbnail_rejects_invalid_max_edge(max_edge: object) -> None:
    fake_mp4 = (
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
        + b"\x00" * 64
    )
    result = media_ops.render_video_thumbnail(fake_mp4, max_edge=max_edge)  # type: ignore[arg-type]
    assert result["status"] == "failed"
    assert "max_edge" in result["error"]


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") or shutil.which("qlmanage")),
    reason="no local video thumbnail renderer",
)
def test_video_thumbnail_real_mp4(scratch_temp: Path) -> None:
    result = media_ops.render_video_thumbnail(TINY_MP4, max_edge=128)
    assert result["status"] == "ok", result
    assert result["mime_type"] == "image/png"
    assert result["renderer"] in {"ffmpeg", "qlmanage"}
    assert max(result["width"], result["height"]) <= 128
    width, height, _ = _decode_png(result["data"])
    assert (width, height) == (result["width"], result["height"])
    assert list(scratch_temp.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX stub executables")
def test_video_thumbnail_renderer_failure_cleans_exact_temp_dir(
    scratch_temp: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "failing-video-bin"
    bin_dir.mkdir()
    for name in ("ffmpeg", "qlmanage"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    result = media_ops.render_video_thumbnail(TINY_MP4, max_edge=128)
    assert result["status"] == "failed"
    assert "ffmpeg" in result["error"] and "qlmanage" in result["error"]
    assert list(scratch_temp.iterdir()) == []
