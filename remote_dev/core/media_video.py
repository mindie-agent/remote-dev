"""
[Role] Best-effort full-video thumbnail (`media_video.py`) for fallback views.
[Invariants] ffmpeg/qlmanage only; explicit failure when unavailable; bounded output.
[Timeouts & Locks] Short local subprocess caps.
[Side-Effect Policy] Local temp files only.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile

from .media_core import (
    MAX_VIDEO_BYTES, MAX_VIDEO_THUMB_BYTES, _VIDEO_THUMB_TIMEOUT_SECONDS, MediaOpError, _as_bytes, _cleanup_temp,
    _failed, _read_bounded, _run, _which,
)
from .media_image import _validate_max_edge, render_image


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
