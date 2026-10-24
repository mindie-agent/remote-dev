"""
[Role] Local media conversion and rendering for bounded remote pulls (`media_ops.py`).
[Invariants]
- Pure local bytes-in / bytes-out helpers: no SSH, endpoint, or filesystem-path inputs.
- Every renderer receives a bounded byte payload, writes only inside a unique
  ``tempfile.mkdtemp`` directory, and that exact directory is removed in ``finally``.
- Image/PDF output dimensions are probed from the produced file rather than trusted
  from renderer arguments; renderer failures fall through to the next local backend.
- Remote ``python3 -I`` never needs PyMuPDF/Pillow: rendering happens on the host.
[Timeouts & Locks]
- Renderer subprocesses are finite-timeout, non-interactive invocations with piped
  stdout/stderr; there is no shell and no background process.
[Side-Effect Policy]
- Read-only outside the owned temporary directory; no repository or state writes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

# Bounds keep a single media read from becoming an unbounded decode.
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGE_BYTES = 8 * 1024 * 1024
MAX_PDF_TOTAL_BYTES = 32 * 1024 * 1024
MAX_NOTEBOOK_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024
MAX_VIDEO_THUMB_BYTES = 16 * 1024 * 1024

_SUBPROCESS_TIMEOUT_SECONDS = 120
_VIDEO_THUMB_TIMEOUT_SECONDS = 30
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}
_SUPPORTED_NOTEBOOK_CELL_TYPES = {"code", "markdown", "raw"}


class MediaOpError(RuntimeError):
    """A local renderer or input validation failed; converted to ``failed`` results."""


def _failed(error: object) -> dict[str, Any]:
    return {"status": "failed", "error": str(error)}


def _as_bytes(data: object, *, label: str) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    raise MediaOpError(f"{label} data must be bytes")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _module_available(name: str) -> bool:
    """Return whether an optional import exists without importing it eagerly."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _run(argv: list[str], *, timeout: int = _SUBPROCESS_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[bytes]:
    """Run one local renderer without a shell and normalize failure into MediaOpError."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaOpError(f"renderer executable is unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaOpError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        raise MediaOpError(
            f"{Path(argv[0]).name} exited {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return completed


def _cleanup_temp(path: Path | None) -> None:
    """Remove exactly the directory returned by ``mkdtemp``; never a prefix/glob."""
    if path is not None:
        shutil.rmtree(str(path), ignore_errors=True)


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    """Read a renderer output only after its on-disk size is known to be bounded."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaOpError(f"{label} is missing: {exc}") from exc
    if size > limit:
        raise MediaOpError(f"{label} is {size} bytes, over the {limit}-byte bound")
    data = path.read_bytes()
    if len(data) > limit:
        raise MediaOpError(f"{label} is {len(data)} bytes, over the {limit}-byte bound")
    return data


# ---------------------------------------------------------------------------
# Image format sniffing, dimension probing, and validation
# ---------------------------------------------------------------------------


def _sniff_image(data: bytes) -> tuple[str, str, str] | None:
    """Return ``(format, mime_type, extension)`` for the four advertised formats."""
    if data.startswith(_PNG_SIGNATURE):
        return "png", "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg", ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp", ".webp"
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    position = 2
    length = len(data)
    while position + 4 <= length:
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == 0xDA:  # Start of scan; no frame header remains.
            break
        segment_length = struct.unpack(">H", data[position + 2 : position + 4])[0]
        if segment_length < 2:
            break
        if marker in _JPEG_SOF_MARKERS and position + 9 <= length:
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            if width > 0 and height > 0:
                return width, height
        position += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        if width and height:
            return width, height
    return None


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(_PNG_SIGNATURE) and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg_dimensions(data)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    return None


def _sips_dimensions(path: Path) -> tuple[int, int] | None:
    sips = _which("sips")
    if not sips:
        return None
    try:
        completed = _run(
            [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            timeout=30,
        )
    except MediaOpError:
        return None
    stdout = completed.stdout.decode("utf-8", errors="replace")
    width_match = re.search(r"pixelWidth:\s*(\d+)", stdout)
    height_match = re.search(r"pixelHeight:\s*(\d+)", stdout)
    if not width_match or not height_match:
        return None
    width, height = int(width_match.group(1)), int(height_match.group(1))
    return (width, height) if width and height else None


def _probe_image_dimensions(path: Path) -> tuple[int, int] | None:
    """Probe dimensions from the produced file itself, with sips as a fallback."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    dimensions = _image_dimensions_from_bytes(data)
    if dimensions is not None:
        return dimensions
    return _sips_dimensions(path)


def _validate_max_edge(max_edge: int | None) -> int | None:
    if max_edge is None:
        return None
    if isinstance(max_edge, bool) or not isinstance(max_edge, int) or max_edge < 1:
        raise MediaOpError("max_edge must be a positive integer or None")
    return max_edge


def _validate_region(region: object, width: int, height: int) -> dict[str, int] | None:
    if region is None:
        return None
    if not isinstance(region, dict):
        raise MediaOpError("region must be an object with x, y, width, and height")
    values: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        value = region.get(key)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise MediaOpError(f"region.{key} must be an integer")
        values[key] = value
    if values["x"] < 0 or values["y"] < 0 or values["width"] <= 0 or values["height"] <= 0:
        raise MediaOpError("region requires non-negative x/y and positive width/height")
    if values["x"] + values["width"] > width or values["y"] + values["height"] > height:
        raise MediaOpError(
            f"region {values!r} is outside the {width}x{height} image"
        )
    return values


def _aspect_dimensions(
    source_width: int, source_height: int, max_edge: int
) -> tuple[float, float]:
    scale = max_edge / max(source_width, source_height)
    return source_width * scale, source_height * scale


def _validate_rendered_dimensions(
    actual: tuple[int, int],
    *,
    cropped_width: int,
    cropped_height: int,
    resized: bool,
    max_edge: int | None,
) -> None:
    width, height = actual
    if width < 1 or height < 1:
        raise MediaOpError(f"renderer produced invalid dimensions {width}x{height}")
    if not resized:
        expected = (cropped_width, cropped_height)
        if expected != actual:
            raise MediaOpError(
                f"renderer produced {width}x{height}, expected {expected[0]}x{expected[1]}"
            )
        return
    assert max_edge is not None
    if max(width, height) > max_edge:
        raise MediaOpError(
            f"renderer produced {width}x{height}, exceeding max_edge={max_edge}"
        )
    if max(width, height) < max_edge - 1:
        raise MediaOpError(
            f"renderer downscaled to {width}x{height} instead of max_edge={max_edge}"
        )
    expected_width, expected_height = _aspect_dimensions(
        cropped_width, cropped_height, max_edge
    )
    if abs(width - expected_width) > 1.01 or abs(height - expected_height) > 1.01:
        raise MediaOpError(
            "renderer did not preserve the aspect ratio: "
            f"got {width}x{height}, expected about "
            f"{expected_width:.2f}x{expected_height:.2f}"
        )


# ---------------------------------------------------------------------------
# Image renderer backends
# ---------------------------------------------------------------------------


def _sips_render_image(
    source: Path,
    destination: Path,
    *,
    region: dict[str, int] | None,
    max_edge: int | None,
    output_mime: str,
    temporary: Path,
) -> Path:
    sips = _which("sips")
    if not sips:
        raise MediaOpError("sips is not on PATH")
    current = source
    if region is not None:
        cropped = temporary / "cropped.png"
        _run(
            [
                sips,
                "-s",
                "format",
                "png",
                "--cropToHeightWidth",
                str(region["height"]),
                str(region["width"]),
                "--cropOffset",
                str(region["y"]),
                str(region["x"]),
                str(current),
                "--out",
                str(cropped),
            ]
        )
        current = cropped
    if max_edge is not None:
        resized = temporary / "resized.png"
        _run(
            [
                sips,
                "-s",
                "format",
                "png",
                "--resampleHeightWidthMax",
                str(max_edge),
                str(current),
                "--out",
                str(resized),
            ]
        )
        current = resized
    if output_mime == "image/png":
        if current.suffix.lower() != ".png":
            _run([sips, "-s", "format", "png", str(current), "--out", str(destination)])
            return destination
        return current
    _run(
        [
            sips,
            "-s",
            "format",
            "jpeg",
            "-s",
            "formatOptions",
            "85",
            str(current),
            "--out",
            str(destination),
        ]
    )
    return destination


def _pillow_render_image(
    source: Path,
    destination: Path,
    *,
    region: dict[str, int] | None,
    max_edge: int | None,
    output_mime: str,
    temporary: Path,
) -> Path:
    try:
        from PIL import Image
    except ImportError as exc:
        raise MediaOpError("Pillow is not importable") from exc
    try:
        image = Image.open(source)
        image.load()
        if getattr(image, "is_animated", False):
            image.seek(0)
        if region is not None:
            image = image.crop(
                (
                    region["x"],
                    region["y"],
                    region["x"] + region["width"],
                    region["y"] + region["height"],
                )
            )
        if max_edge is not None:
            resampling = getattr(Image, "Resampling", Image)
            image.thumbnail((max_edge, max_edge), resampling.LANCZOS)
        if output_mime == "image/png":
            if image.mode not in ("RGB", "RGBA", "P", "L", "LA"):
                image = image.convert("RGBA" if "A" in image.mode else "RGB")
            image.save(destination, format="PNG", optimize=True)
        else:
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.save(destination, format="JPEG", quality=85, optimize=True)
        return destination
    except MediaOpError:
        raise
    except Exception as exc:  # Pillow raises a variety of decode/save errors.
        raise MediaOpError(f"Pillow failed to crop/resize image: {exc}") from exc


def _imagemagick_binary() -> str | None:
    magick = _which("magick")
    if magick:
        return magick
    # Windows ships an unrelated ``convert.exe``; only trust legacy ``convert``
    # on POSIX hosts.
    if os.name != "nt":
        return _which("convert")
    return None


def _imagemagick_render_image(
    source: Path,
    destination: Path,
    *,
    region: dict[str, int] | None,
    max_edge: int | None,
    output_mime: str,
    temporary: Path,
) -> Path:
    binary = _imagemagick_binary()
    if not binary:
        raise MediaOpError("ImageMagick magick/convert is not on PATH")
    argv = [binary, str(source)]
    if region is not None:
        argv.extend(
            [
                "-crop",
                f"{region['width']}x{region['height']}+{region['x']}+{region['y']}",
                "+repage",
            ]
        )
    if max_edge is not None:
        argv.extend(["-resize", f"{max_edge}x{max_edge}>"])
    if output_mime == "image/jpeg":
        argv.extend(["-quality", "85"])
    elif output_mime == "image/png":
        argv.extend(["-define", "png:compression-level=6"])
    argv.append(str(destination))
    _run(argv)
    return destination


def _image_renderers() -> list[tuple[str, Callable[..., Path]]]:
    renderers: list[tuple[str, Callable[..., Path]]] = []
    if _which("sips"):
        renderers.append(("sips", _sips_render_image))
    if _module_available("PIL"):
        renderers.append(("pillow", _pillow_render_image))
    if _imagemagick_binary():
        renderers.append(("imagemagick", _imagemagick_render_image))
    return renderers


def render_image(
    data: bytes,
    *,
    mime_type: str | None = None,
    region: dict | None = None,
    max_edge: int | None = 1568,
) -> dict:
    """Crop/resize a PNG, JPEG, GIF, or WebP image with local host tools.

    ``mime_type`` is advisory only; the bytes are sniffed. ``max_edge=None`` means
    native resolution. A region is an exact ``{x, y, width, height}`` crop with a
    top-left origin. Output is PNG for GIF/WebP crops or resizes and preserves
    PNG/JPEG for those inputs. Animated GIF/WebP inputs are rendered as first-frame
    stills when an operation is requested.
    """
    try:
        raw = _as_bytes(data, label="image")
        if not raw:
            raise MediaOpError("image data is empty")
        if len(raw) > MAX_IMAGE_BYTES:
            raise MediaOpError(
                f"image is {len(raw)} bytes, over the {MAX_IMAGE_BYTES}-byte bound"
            )
        del mime_type  # Bytes, not caller hints, decide the format.
        sniffed = _sniff_image(raw)
        if sniffed is None:
            raise MediaOpError("not a PNG, JPEG, GIF, or WebP image")
        image_format, input_mime, extension = sniffed
        original = _image_dimensions_from_bytes(raw)
        if original is None:
            raise MediaOpError(f"could not determine dimensions of the {image_format} image")
        original_width, original_height = original
        pixel_count = original_width * original_height
        if pixel_count > MAX_IMAGE_PIXELS:
            raise MediaOpError(
                f"image has {pixel_count} pixels, over the {MAX_IMAGE_PIXELS}-pixel decode bound"
            )
        crop = _validate_region(region, original_width, original_height)
        max_edge = _validate_max_edge(max_edge)
        cropped_width = crop["width"] if crop is not None else original_width
        cropped_height = crop["height"] if crop is not None else original_height
        resized = max_edge is not None and max(cropped_width, cropped_height) > max_edge
        cropped = crop is not None
        full_frame_crop = (
            crop is not None
            and crop["x"] == 0
            and crop["y"] == 0
            and crop["width"] == original_width
            and crop["height"] == original_height
        )
        if not resized and (crop is None or full_frame_crop):
            # Identity crop (notably a 1x1 source) needs no renderer round-trip.
            return {
                "status": "ok",
                "data": raw,
                "mime_type": input_mime,
                "width": original_width,
                "height": original_height,
                "original_width": original_width,
                "original_height": original_height,
                "resized": False,
                "cropped": cropped,
            }
        output_mime = "image/jpeg" if image_format == "jpeg" else "image/png"
        output_extension = ".jpg" if output_mime == "image/jpeg" else ".png"
        temporary: Path | None = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix="remote-dev-media-image-"))
            source = temporary / f"source{extension}"
            source.write_bytes(raw)
            failures: list[str] = []
            for name, renderer in _image_renderers():
                destination = temporary / f"output-{name}{output_extension}"
                try:
                    produced = renderer(
                        source,
                        destination,
                        region=None if full_frame_crop else crop,
                        max_edge=max_edge if resized else None,
                        output_mime=output_mime,
                        temporary=temporary,
                    )
                    rendered = _read_bounded(
                        produced, MAX_IMAGE_BYTES, "rendered image"
                    )
                    if not rendered:
                        raise MediaOpError("renderer produced an empty file")
                    rendered_format = _sniff_image(rendered)
                    if rendered_format is None:
                        raise MediaOpError("renderer produced an unrecognized image format")
                    actual = _probe_image_dimensions(produced)
                    if actual is None:
                        raise MediaOpError("could not probe dimensions of the rendered image")
                    _validate_rendered_dimensions(
                        actual,
                        cropped_width=cropped_width,
                        cropped_height=cropped_height,
                        resized=resized,
                        max_edge=max_edge,
                    )
                    return {
                        "status": "ok",
                        "data": rendered,
                        "mime_type": rendered_format[1],
                        "width": actual[0],
                        "height": actual[1],
                        "original_width": original_width,
                        "original_height": original_height,
                        "resized": resized,
                        "cropped": cropped,
                    }
                except (MediaOpError, OSError, struct.error) as exc:
                    failures.append(f"{name}: {exc}")
            raise MediaOpError(
                "local image rendering failed; "
                + ("; ".join(failures) if failures else "no local renderer available")
            )
        finally:
            _cleanup_temp(temporary)
    except (MediaOpError, OSError, ValueError, struct.error) as exc:
        return _failed(exc)


# ---------------------------------------------------------------------------
# PDF page selection and rendering
# ---------------------------------------------------------------------------


def _validate_max_pages(max_pages: int) -> int:
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise MediaOpError("max_pages must be a positive integer")
    return max_pages


def _parse_page_selection(
    pages: str | None, *, total_pages: int | None, max_pages: int
) -> list[int]:
    """Parse 1-indexed page ranges such as ``1-5``, ``3``, and ``10-20``."""
    if pages is not None and not isinstance(pages, str):
        raise MediaOpError("pages must be a string such as '1-5' or None")
    selected: list[int] = []
    if pages is None or not pages.strip():
        if total_pages == 0:
            raise MediaOpError("PDF has no pages")
        upper = total_pages if total_pages is not None else max_pages
        selected = list(range(1, min(upper, max_pages) + 1))
    else:
        for raw_part in pages.split(","):
            part = raw_part.strip()
            if not part:
                raise MediaOpError(f"invalid PDF page selection {pages!r}")
            if "-" in part:
                pieces = part.split("-")
                if len(pieces) != 2:
                    raise MediaOpError(f"invalid PDF page range {part!r}")
                start_text, end_text = (piece.strip() for piece in pieces)
                if not start_text.isdigit() or not end_text.isdigit():
                    raise MediaOpError(f"invalid PDF page range {part!r}")
                start, end = int(start_text), int(end_text)
            else:
                if not part.isdigit():
                    raise MediaOpError(f"invalid PDF page number {part!r}")
                start = end = int(part)
            if start < 1 or end < 1 or end < start:
                raise MediaOpError(f"invalid PDF page range {part!r}")
            for page_number in range(start, end + 1):
                if page_number not in selected:
                    selected.append(page_number)
        if not selected:
            raise MediaOpError("PDF page selection is empty")
        if len(selected) > max_pages:
            raise MediaOpError(
                f"PDF page selection requests {len(selected)} pages, over max_pages={max_pages}"
            )
        if total_pages is not None:
            beyond = [page for page in selected if page > total_pages]
            if beyond:
                raise MediaOpError(
                    f"PDF page {beyond[0]} is outside the {total_pages}-page document"
                )
    if not selected:
        raise MediaOpError("PDF page selection is empty")
    if len(selected) > max_pages:
        raise MediaOpError(
            f"PDF page selection requests {len(selected)} pages, over max_pages={max_pages}"
        )
    return selected


def _pdf_page_count(path: Path, data: bytes) -> int | None:
    pdfinfo = _which("pdfinfo")
    if pdfinfo:
        try:
            completed = _run([pdfinfo, str(path)], timeout=30)
            match = re.search(
                rb"^Pages:\s+(\d+)\s*$",
                completed.stdout,
                re.MULTILINE,
            )
            if match:
                count = int(match.group(1))
                if count > 0:
                    return count
        except MediaOpError:
            pass
    if _module_available("pypdfium2"):
        try:
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(str(path))
            try:
                count = len(document)
            finally:
                document.close()
            if count > 0:
                return int(count)
        except Exception:
            pass
    if _module_available("fitz"):
        try:
            import fitz

            document = fitz.open(str(path))
            try:
                count = int(document.page_count)
            finally:
                document.close()
            if count > 0:
                return count
        except Exception:
            pass
    # Last resort for uncompressed PDFs. Compressed page trees may return None.
    count = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))
    return count if count > 0 else None


def _available_pdf_renderers() -> list[str]:
    renderers: list[str] = []
    if _which("pdftoppm"):
        renderers.append("pdftoppm")
    if _module_available("pypdfium2"):
        renderers.append("pypdfium2")
    if _module_available("fitz"):
        renderers.append("pymupdf")
    return renderers


def _finish_pdf_page(
    data: bytes,
    *,
    page_number: int,
    max_edge: int | None,
    total_bytes: int,
) -> dict[str, Any]:
    if len(data) > MAX_PDF_PAGE_BYTES:
        raise MediaOpError(
            f"PDF page {page_number} rendered to {len(data)} bytes, over the "
            f"{MAX_PDF_PAGE_BYTES}-byte per-page bound"
        )
    dimensions = _image_dimensions_from_bytes(data)
    if dimensions is None:
        raise MediaOpError(f"PDF page {page_number} did not produce a readable PNG")
    if max_edge is not None and max(dimensions) > max_edge:
        normalized = render_image(data, mime_type="image/png", max_edge=max_edge)
        if normalized.get("status") != "ok":
            raise MediaOpError(
                f"failed to fit PDF page {page_number} to max_edge={max_edge}: "
                f"{normalized.get('error')}"
            )
        data = normalized["data"]
        dimensions = (int(normalized["width"]), int(normalized["height"]))
    if max_edge is not None and max(dimensions) > max_edge:
        raise MediaOpError(
            f"PDF page {page_number} is {dimensions[0]}x{dimensions[1]}, "
            f"over max_edge={max_edge}"
        )
    if len(data) > MAX_PDF_PAGE_BYTES:
        raise MediaOpError(
            f"PDF page {page_number} is {len(data)} bytes after fitting, over the "
            f"{MAX_PDF_PAGE_BYTES}-byte per-page bound"
        )
    if total_bytes + len(data) > MAX_PDF_TOTAL_BYTES:
        raise MediaOpError(
            f"PDF page output would exceed the {MAX_PDF_TOTAL_BYTES}-byte total bound"
        )
    return {
        "page": page_number,
        "data": data,
        "mime_type": "image/png",
        "width": dimensions[0],
        "height": dimensions[1],
    }


def _render_pdf_pdftoppm(
    pdf_path: Path,
    temporary: Path,
    *,
    selection: list[int],
    max_edge: int | None,
    discover_end: bool,
) -> list[dict[str, Any]]:
    pdftoppm = _which("pdftoppm")
    if not pdftoppm:
        raise MediaOpError("pdftoppm is not on PATH")
    pages: list[dict[str, Any]] = []
    total_bytes = 0
    for page_number in selection:
        prefix = temporary / f"page-{page_number:04d}"
        output = temporary / f"{prefix.name}.png"
        argv = [
            pdftoppm,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            "144",
            "-png",
            "-singlefile",
        ]
        if max_edge is not None:
            argv.extend(["-scale-to", str(max_edge)])
        argv.extend([str(pdf_path), str(prefix)])
        try:
            _run(argv)
        except MediaOpError:
            if discover_end and page_number > 1:
                break
            raise
        if not output.is_file():
            if discover_end and page_number > 1:
                break
            raise MediaOpError(f"pdftoppm did not produce page {page_number}")
        page_bytes = _read_bounded(
            output, MAX_PDF_PAGE_BYTES, f"PDF page {page_number} output"
        )
        page = _finish_pdf_page(
            page_bytes,
            page_number=page_number,
            max_edge=max_edge,
            total_bytes=total_bytes,
        )
        pages.append(page)
        total_bytes += len(page["data"])
        try:
            output.unlink()
        except OSError:
            pass
    return pages


def _loaded_pypdfium2_document(path: Path) -> tuple[Any, int]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise MediaOpError("pypdfium2 is not importable") from exc
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise MediaOpError(f"pypdfium2 could not open the PDF: {exc}") from exc
    count = len(document) if hasattr(document, "__len__") else document.get_page_count()
    return document, int(count)


def _render_pdf_pypdfium2(
    pdf_path: Path,
    temporary: Path,
    *,
    selection: list[int],
    max_edge: int | None,
    discover_end: bool,
) -> list[dict[str, Any]]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise MediaOpError("pypdfium2 rendering needs Pillow for PNG encoding") from exc
    document, count = _loaded_pypdfium2_document(pdf_path)
    pages: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for page_number in selection:
            if page_number > count:
                if discover_end and page_number > 1:
                    break
                raise MediaOpError(f"PDF page {page_number} is outside the {count}-page document")
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                scale = (
                    max_edge / max(width, height)
                    if max_edge is not None
                    else 2.0  # 144 DPI
                )
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                if max_edge is not None and max(image.size) > max_edge:
                    resampling = getattr(Image, "Resampling", Image)
                    image.thumbnail((max_edge, max_edge), resampling.LANCZOS)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                output = temporary / f"page-{page_number:04d}.png"
                image.save(output, format="PNG")
                page_bytes = _read_bounded(
                    output, MAX_PDF_PAGE_BYTES, f"PDF page {page_number} output"
                )
            finally:
                page.close()
            page_result = _finish_pdf_page(
                page_bytes,
                page_number=page_number,
                max_edge=max_edge,
                total_bytes=total_bytes,
            )
            pages.append(page_result)
            total_bytes += len(page_result["data"])
    finally:
        document.close()
    return pages


def _render_pdf_fitz(
    pdf_path: Path,
    temporary: Path,
    *,
    selection: list[int],
    max_edge: int | None,
    discover_end: bool,
) -> list[dict[str, Any]]:
    try:
        import fitz
    except ImportError as exc:
        raise MediaOpError("PyMuPDF is not importable") from exc
    try:
        document = fitz.open(str(pdf_path))
    except Exception as exc:
        raise MediaOpError(f"PyMuPDF could not open the PDF: {exc}") from exc
    pages: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for page_number in selection:
            if page_number > document.page_count:
                if discover_end and page_number > 1:
                    break
                raise MediaOpError(
                    f"PDF page {page_number} is outside the {document.page_count}-page document"
                )
            page = document[page_number - 1]
            rect = page.rect
            scale = (
                max_edge / max(rect.width, rect.height)
                if max_edge is not None
                else 2.0  # 144 DPI
            )
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            output = temporary / f"page-{page_number:04d}.png"
            pixmap.save(str(output))
            page_bytes = _read_bounded(
                output, MAX_PDF_PAGE_BYTES, f"PDF page {page_number} output"
            )
            page_result = _finish_pdf_page(
                page_bytes,
                page_number=page_number,
                max_edge=max_edge,
                total_bytes=total_bytes,
            )
            pages.append(page_result)
            total_bytes += len(page_result["data"])
    finally:
        document.close()
    return pages


def render_pdf(
    data: bytes,
    *,
    pages: str | None = None,
    max_pages: int = 20,
    max_edge: int | None = 1568,
) -> dict:
    """Render selected PDF pages to PNG with ``pdftoppm`` or optional Python bindings.

    Page numbers are 1-indexed. ``pages`` accepts comma-separated ranges such as
    ``"1-5,8"``. With ``pages=None`` the first ``max_pages`` pages are rendered.
    ``max_edge`` fits the long side of each page; ``None`` renders at 144 DPI.
    """
    temporary: Path | None = None
    try:
        raw = _as_bytes(data, label="PDF")
        if not raw:
            raise MediaOpError("PDF data is empty")
        if len(raw) > MAX_PDF_BYTES:
            raise MediaOpError(
                f"PDF is {len(raw)} bytes, over the {MAX_PDF_BYTES}-byte bound"
            )
        if b"%PDF-" not in raw[:1024]:
            raise MediaOpError("not a PDF document")
        max_pages = _validate_max_pages(max_pages)
        max_edge = _validate_max_edge(max_edge)
        temporary = Path(tempfile.mkdtemp(prefix="remote-dev-media-pdf-"))
        pdf_path = temporary / "input.pdf"
        pdf_path.write_bytes(raw)
        total_pages = _pdf_page_count(pdf_path, raw)
        selection = _parse_page_selection(
            pages, total_pages=total_pages, max_pages=max_pages
        )
        discover_end = total_pages is None and (pages is None or not pages.strip())
        renderer_names = _available_pdf_renderers()
        if not renderer_names:
            raise MediaOpError(
                "no local PDF renderer available; looked for pdftoppm, pypdfium2, "
                "and PyMuPDF"
            )
        renderers: dict[str, Callable[..., list[dict[str, Any]]]] = {
            "pdftoppm": _render_pdf_pdftoppm,
            "pypdfium2": _render_pdf_pypdfium2,
            "pymupdf": _render_pdf_fitz,
        }
        failures: list[str] = []
        for renderer_name in renderer_names:
            try:
                rendered_pages = renderers[renderer_name](
                    pdf_path,
                    temporary,
                    selection=selection,
                    max_edge=max_edge,
                    discover_end=discover_end,
                )
                if not rendered_pages:
                    raise MediaOpError("renderer produced no pages")
                return {
                    "status": "ok",
                    "pages": rendered_pages,
                    "count": len(rendered_pages),
                    "renderer": renderer_name,
                }
            except (MediaOpError, OSError) as exc:
                failures.append(f"{renderer_name}: {exc}")
        raise MediaOpError(
            "local PDF rendering failed; "
            + ("; ".join(failures) if failures else "no usable renderer")
        )
    except (MediaOpError, OSError, ValueError) as exc:
        return _failed(exc)
    finally:
        _cleanup_temp(temporary)


# ---------------------------------------------------------------------------
# Notebook read/edit
# ---------------------------------------------------------------------------


def _text_from_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text_from_value(part) for part in value)
    return str(value)


def _notebook_payload(data: bytes | str) -> dict[str, Any]:
    if isinstance(data, str):
        text = data
    else:
        raw = _as_bytes(data, label="notebook")
        if len(raw) > MAX_NOTEBOOK_BYTES:
            raise MediaOpError(
                f"notebook is {len(raw)} bytes, over the {MAX_NOTEBOOK_BYTES}-byte bound"
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MediaOpError(f"notebook is not UTF-8: {exc}") from exc
    try:
        notebook = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise MediaOpError(f"notebook is not valid JSON: {exc}") from exc
    if not isinstance(notebook, dict):
        raise MediaOpError("notebook JSON must be an object")
    if not isinstance(notebook.get("cells"), list):
        raise MediaOpError("notebook has no cells array")
    return notebook


def _cell_id(cell: dict[str, Any], index: int) -> str:
    value = cell.get("id")
    if isinstance(value, str) and value:
        return value
    return f"cell-{index}"


def _output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, dict):
        return ""
    output_type = output.get("output_type")
    if output_type == "stream":
        return _text_from_value(output.get("text"))
    if output_type in ("execute_result", "display_data"):
        data = output.get("data")
        if isinstance(data, dict):
            for mime in ("text/plain", "text/markdown", "text/html"):
                if mime in data:
                    return _text_from_value(data[mime])
            for mime in data:
                if isinstance(mime, str) and mime.startswith("image/"):
                    return f"[{mime} output]"
        return _text_from_value(output.get("text"))
    if output_type == "error":
        ename = str(output.get("ename") or "Error")
        evalue = str(output.get("evalue") or "")
        headline = f"{ename}: {evalue}".strip()
        traceback = output.get("traceback")
        if isinstance(traceback, list):
            trace_text = "\n".join(str(part) for part in traceback).strip()
        else:
            trace_text = _text_from_value(traceback).strip()
        return headline + (("\n" + trace_text) if trace_text else "")
    if "text" in output:
        return _text_from_value(output.get("text"))
    data = output.get("data")
    if isinstance(data, dict) and "text/plain" in data:
        return _text_from_value(data["text/plain"])
    return ""


def _normalized_cells(notebook: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict):
            continue
        outputs = cell.get("outputs")
        output_texts: list[str] = []
        if isinstance(outputs, list):
            for output in outputs:
                text = _output_text(output)
                if text:
                    output_texts.append(text)
        result.append(
            {
                "id": _cell_id(cell, index),
                "cell_type": str(cell.get("cell_type") or "unknown"),
                "source": _text_from_value(cell.get("source")),
                "outputs": output_texts,
            }
        )
    return result


def _notebook_text(cells: list[dict[str, Any]]) -> str:
    lines = [f"Notebook ({len(cells)} cells)"]
    for index, cell in enumerate(cells):
        lines.extend(
            [
                "",
                f"# Cell {index} [{cell['cell_type']}] id={cell['id']}",
                cell["source"],
            ]
        )
        if cell["outputs"]:
            lines.append("Outputs:")
            lines.extend(cell["outputs"])
    return "\n".join(lines).rstrip() + "\n"


def read_notebook(data: bytes) -> dict:
    """Parse an ``.ipynb`` payload into structured cells plus a text fallback."""
    try:
        notebook = _notebook_payload(data)
        cells = _normalized_cells(notebook)
        return {
            "status": "ok",
            "cells": cells,
            "count": len(cells),
            "text": _notebook_text(cells),
        }
    except (MediaOpError, OSError, ValueError, RecursionError) as exc:
        return _failed(exc)


def _find_cell_index(cells: list[Any], cell_id: str) -> int:
    # Explicit ids take precedence over synthetic index fallbacks.
    for index, cell in enumerate(cells):
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return index
    for index, cell in enumerate(cells):
        if isinstance(cell, dict) and f"cell-{index}" == cell_id:
            return index
    raise MediaOpError(f"notebook cell_id {cell_id!r} was not found")


def _new_cell_id(cells: list[Any]) -> str:
    existing = {
        cell.get("id")
        for cell in cells
        if isinstance(cell, dict) and isinstance(cell.get("id"), str)
    }
    while True:
        candidate = f"cell-{uuid.uuid4().hex[:12]}"
        if candidate not in existing:
            return candidate


def _normalize_cell_type_for_edit(
    cell: dict[str, Any], cell_type: str
) -> None:
    if cell_type not in _SUPPORTED_NOTEBOOK_CELL_TYPES:
        raise MediaOpError(
            "cell_type must be one of " + ", ".join(sorted(_SUPPORTED_NOTEBOOK_CELL_TYPES))
        )
    cell["cell_type"] = cell_type
    if cell_type == "code":
        cell.setdefault("outputs", [])
        cell.setdefault("execution_count", None)
    else:
        cell.pop("outputs", None)
        cell.pop("execution_count", None)


def edit_notebook(
    data: bytes,
    *,
    new_source: str,
    cell_id: str | None,
    cell_type: str | None,
    edit_mode: str = "replace",
) -> dict:
    """Replace, insert, or delete notebook cells and return canonical JSON bytes.

    ``replace``/``delete`` require an existing ``cell_id``. ``insert`` requires
    ``cell_type`` and generates a unique id; with a ``cell_id`` the new cell goes
    immediately after that cell, otherwise it becomes the first cell. Replace keeps
    the existing type unless ``cell_type`` is supplied. Sources are stored as a
    one-element list, matching the existing Claude native notebook writer.
    """
    try:
        if not isinstance(new_source, str):
            raise MediaOpError("new_source must be a string")
        if len(new_source.encode("utf-8")) > MAX_NOTEBOOK_BYTES:
            raise MediaOpError("new_source is too large")
        notebook = _notebook_payload(data)
        cells = notebook["cells"]
        mode = str(edit_mode or "replace").lower()
        if mode not in {"replace", "insert", "delete"}:
            raise MediaOpError(f"unknown edit_mode {edit_mode!r}")
        if cell_id is not None and not isinstance(cell_id, str):
            raise MediaOpError("cell_id must be a string or None")
        if mode in {"replace", "delete"}:
            if not cell_id:
                raise MediaOpError(f"edit_mode={mode} requires cell_id")
            index = _find_cell_index(cells, cell_id)
            cell = cells[index]
            if not isinstance(cell, dict):
                raise MediaOpError("notebook target cell is not an object")
            if mode == "delete":
                del cells[index]
            else:
                cell["source"] = [new_source]
                if cell_type is not None:
                    if not isinstance(cell_type, str):
                        raise MediaOpError("cell_type must be a string or None")
                    _normalize_cell_type_for_edit(cell, cell_type)
        else:
            if cell_type is None:
                raise MediaOpError("edit_mode=insert requires cell_type")
            if not isinstance(cell_type, str):
                raise MediaOpError("cell_type must be a string")
            if cell_type not in _SUPPORTED_NOTEBOOK_CELL_TYPES:
                raise MediaOpError(
                    "cell_type must be one of "
                    + ", ".join(sorted(_SUPPORTED_NOTEBOOK_CELL_TYPES))
                )
            new_cell: dict[str, Any] = {
                "cell_type": cell_type,
                "metadata": {},
                "source": [new_source],
                "id": _new_cell_id(cells),
            }
            if cell_type == "code":
                new_cell["execution_count"] = None
                new_cell["outputs"] = []
            position = 0
            if cell_id:
                position = _find_cell_index(cells, cell_id) + 1
            cells.insert(position, new_cell)
        canonical = (
            json.dumps(
                notebook,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        normalized = _normalized_cells(notebook)
        return {
            "status": "ok",
            "data": canonical,
            "cells": normalized,
            "count": len(normalized),
        }
    except (MediaOpError, OSError, ValueError, TypeError, RecursionError) as exc:
        return _failed(exc)


# ---------------------------------------------------------------------------
# Video thumbnail (first available local thumbnailer)
# ---------------------------------------------------------------------------


def _sniff_video_container(data: bytes) -> str | None:
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        return ".mov" if brand == b"qt  " else ".mp4"
    if data[:4] in (b"moov", b"mdat", b"wide"):
        return ".mov"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".webm" if b"webm" in data[:64] else ".mkv"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return ".avi"
    if data.startswith(b"OggS"):
        return ".ogv"
    if data.startswith(b"FLV"):
        return ".flv"
    if data[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return ".mpg"
    return None


def _video_renderer_names() -> list[str]:
    names: list[str] = []
    if _which("ffmpeg"):
        names.append("ffmpeg")
    if _which("qlmanage"):
        names.append("qlmanage")
    return names


def _ffmpeg_video_thumbnail(
    source: Path, temporary: Path, *, max_edge: int
) -> bytes:
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise MediaOpError("ffmpeg is not on PATH")
    output = temporary / "ffmpeg-frame.png"
    scale = (
        f"scale=w='min(iw,{max_edge})':h='min(ih,{max_edge})':"
        "force_original_aspect_ratio=decrease"
    )
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-vf",
            scale,
            "-f",
            "image2",
            str(output),
        ],
        timeout=45,
    )
    if not output.is_file():
        raise MediaOpError("ffmpeg produced no frame")
    return _read_bounded(output, MAX_VIDEO_THUMB_BYTES, "ffmpeg thumbnail")


def _qlmanage_video_thumbnail(
    source: Path, temporary: Path, *, max_edge: int
) -> bytes:
    qlmanage = _which("qlmanage")
    if not qlmanage:
        raise MediaOpError("qlmanage is not on PATH")
    output_directory = temporary / "quicklook"
    output_directory.mkdir()
    _run(
        [
            qlmanage,
            "-t",
            "-s",
            str(max_edge),
            "-o",
            str(output_directory),
            str(source),
        ],
        timeout=_VIDEO_THUMB_TIMEOUT_SECONDS,
    )
    candidates = [
        path
        for path in output_directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".png"
    ]
    if not candidates:
        expected = output_directory / f"{source.name}.png"
        if expected.is_file():
            candidates = [expected]
    if not candidates:
        raise MediaOpError("qlmanage produced no thumbnail PNG")
    return _read_bounded(
        sorted(candidates)[0], MAX_VIDEO_THUMB_BYTES, "qlmanage thumbnail"
    )


def render_video_thumbnail(data: bytes, *, max_edge: int = 1280) -> dict:
    """Return a best-effort local first-frame/QuickLook thumbnail as PNG.

    This does not decode a video stream in Python. It asks ``ffmpeg`` (first frame)
    or macOS ``qlmanage`` (QuickLook thumbnail) for one still and then verifies and
    fits the produced PNG locally. If neither tool exists, or the container is not
    recognized, an explicit ``failed`` result is returned.
    """
    temporary: Path | None = None
    try:
        raw = _as_bytes(data, label="video")
        if not raw:
            raise MediaOpError("video data is empty")
        if len(raw) > MAX_VIDEO_BYTES:
            raise MediaOpError(
                f"video is {len(raw)} bytes, over the {MAX_VIDEO_BYTES}-byte bound"
            )
        validated_edge = _validate_max_edge(max_edge)
        if validated_edge is None:
            raise MediaOpError("max_edge must be a positive integer for video thumbnails")
        max_edge = validated_edge
        extension = _sniff_video_container(raw)
        if extension is None:
            raise MediaOpError(
                "unrecognized video container; local thumbnailing supports "
                "MP4/MOV, WebM/MKV, AVI, Ogg, FLV, and MPEG"
            )
        renderer_names = _video_renderer_names()
        if not renderer_names:
            raise MediaOpError(
                "no local video thumbnail renderer available; looked for ffmpeg "
                "and qlmanage"
            )
        temporary = Path(tempfile.mkdtemp(prefix="remote-dev-media-video-"))
        source = temporary / f"input{extension}"
        source.write_bytes(raw)
        failures: list[str] = []
        for renderer_name in renderer_names:
            try:
                if renderer_name == "ffmpeg":
                    thumbnail = _ffmpeg_video_thumbnail(
                        source, temporary, max_edge=max_edge
                    )
                else:
                    thumbnail = _qlmanage_video_thumbnail(
                        source, temporary, max_edge=max_edge
                    )
                if not thumbnail:
                    raise MediaOpError("thumbnailer produced an empty image")
                if len(thumbnail) > MAX_VIDEO_THUMB_BYTES:
                    raise MediaOpError(
                        f"thumbnail is {len(thumbnail)} bytes, over the "
                        f"{MAX_VIDEO_THUMB_BYTES}-byte bound"
                    )
                normalized = render_image(
                    thumbnail, mime_type="image/png", max_edge=max_edge
                )
                if normalized.get("status") != "ok":
                    raise MediaOpError(
                        "thumbnail output could not be verified: "
                        f"{normalized.get('error')}"
                    )
                if normalized.get("mime_type") != "image/png":
                    raise MediaOpError(
                        "thumbnailer did not produce a PNG image: "
                        f"{normalized.get('mime_type')}"
                    )
                return {
                    "status": "ok",
                    "data": normalized["data"],
                    "mime_type": "image/png",
                    "width": int(normalized["width"]),
                    "height": int(normalized["height"]),
                    "renderer": renderer_name,
                }
            except (MediaOpError, OSError) as exc:
                failures.append(f"{renderer_name}: {exc}")
        raise MediaOpError(
            "local video thumbnail failed; "
            + ("; ".join(failures) if failures else "no usable renderer")
        )
    except (MediaOpError, OSError, ValueError) as exc:
        return _failed(exc)
    finally:
        _cleanup_temp(temporary)
