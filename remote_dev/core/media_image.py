"""
[Role] Local image sniff/crop/downscale (`media_image.py`).
[Invariants] sips first, optional Pillow/ImageMagick; output dimensions are probed from bytes.
[Timeouts & Locks] Delegates subprocess cap to media_core.
[Side-Effect Policy] Local temp files only.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import re
import struct
import subprocess
import tempfile

from typing import Any, Callable

from .media_core import (
    MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MediaOpError, _JPEG_SOF_MARKERS, _PNG_SIGNATURE,
    _as_bytes, _cleanup_temp, _failed, _module_available, _read_bounded, _run, _which,
)


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


