"""
[Role] Local PDF page rendering (`media_pdf.py`).
[Invariants] pdftoppm first, optional pypdfium2/PyMuPDF; page/byte bounds enforced.
[Timeouts & Locks] Delegates subprocess cap to media_core.
[Side-Effect Policy] Local temp files only.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import struct
import subprocess
import tempfile

from typing import Any

from .media_image import _image_dimensions_from_bytes, _sniff_image, _validate_max_edge
from .media_core import (
    MAX_PDF_BYTES, MAX_PDF_PAGE_BYTES, MAX_PDF_TOTAL_BYTES, MediaOpError,
    _as_bytes, _cleanup_temp, _failed, _module_available, _read_bounded, _run, _which,
)


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
        "mime_type": "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "image/png",
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
    image_format: str = "png",
) -> list[dict[str, Any]]:
    pdftoppm = _which("pdftoppm")
    if not pdftoppm:
        raise MediaOpError("pdftoppm is not on PATH")
    pages: list[dict[str, Any]] = []
    total_bytes = 0
    for page_number in selection:
        prefix = temporary / f"page-{page_number:04d}"
        extension = ".jpg" if image_format == "jpeg" else ".png"
        output = temporary / f"{prefix.name}{extension}"
        argv = [
            pdftoppm,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-r",
            "144",
            "-jpeg" if image_format == "jpeg" else "-png",
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


def pdf_page_count(data: bytes) -> dict[str, Any]:
    temporary: Path | None = None
    try:
        raw = _as_bytes(data, label="PDF")
        if not raw or b"%PDF-" not in raw[:1024]:
            raise MediaOpError("not a PDF document")
        temporary = Path(tempfile.mkdtemp(prefix="remote-dev-media-pdfcount-"))
        pdf_path = temporary / "input.pdf"
        pdf_path.write_bytes(raw)
        count = _pdf_page_count(pdf_path, raw)
        return {"status": "ok", "page_count": count}
    except (MediaOpError, OSError, ValueError) as exc:
        return _failed(exc)
    finally:
        _cleanup_temp(temporary)


def render_pdf(
    data: bytes,
    *,
    pages: str | None = None,
    max_pages: int = 20,
    max_edge: int | None = 1568,
    image_format: str = "png",
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
        if image_format not in {"png", "jpeg"}:
            raise MediaOpError("image_format must be png or jpeg")
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
                if renderer_name == "pdftoppm":
                    rendered_pages = renderers[renderer_name](
                        pdf_path, temporary, selection=selection, max_edge=max_edge,
                        discover_end=discover_end, image_format=image_format,
                    )
                else:
                    rendered_pages = renderers[renderer_name](
                        pdf_path, temporary, selection=selection, max_edge=max_edge,
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


