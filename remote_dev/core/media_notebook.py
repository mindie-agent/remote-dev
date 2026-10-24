"""
[Role] Local .ipynb read/edit (`media_notebook.py`).
[Invariants] Preserves unknown fields; cell ids/types/nbformat-compatible edits; bounded JSON.
[Timeouts & Locks] Pure JSON transformation; no locks.
[Side-Effect Policy] No filesystem side effects.
"""
from __future__ import annotations

from pathlib import Path
import os
import json
import uuid

from typing import Any

from .media_core import MAX_NOTEBOOK_BYTES, MediaOpError, _SUPPORTED_NOTEBOOK_CELL_TYPES, _as_bytes, _failed


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
    metadata = notebook.get("metadata") if isinstance(notebook.get("metadata"), dict) else {}
    kernelspec = metadata.get("kernelspec") if isinstance(metadata.get("kernelspec"), dict) else {}
    language_info = metadata.get("language_info") if isinstance(metadata.get("language_info"), dict) else {}
    language = str(kernelspec.get("language") or language_info.get("name") or "python")
    result: list[dict[str, Any]] = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict):
            continue
        outputs = cell.get("outputs")
        output_texts: list[str] = []
        raw_outputs: list[Any] = []
        if isinstance(outputs, list):
            for output in outputs:
                if isinstance(output, dict):
                    raw_outputs.append(output)
                text = _output_text(output)
                if text:
                    output_texts.append(text)
        result.append(
            {
                "id": _cell_id(cell, index),
                "cell_type": str(cell.get("cell_type") or "unknown"),
                "source": _text_from_value(cell.get("source")),
                "language": language,
                "execution_count": cell.get("execution_count"),
                "outputs": raw_outputs,
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
            for output in cell["outputs"]:
                text = _output_text(output)
                if text:
                    lines.append(text)
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
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def _supports_cell_ids(notebook: dict[str, Any]) -> bool:
    try:
        major = int(notebook.get("nbformat") or 4)
        minor = int(notebook.get("nbformat_minor") or 0)
    except (TypeError, ValueError):
        return False
    return major > 4 or (major == 4 and minor >= 5)


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
    a string, matching the current Claude native notebook writer.
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
                cell["source"] = new_source
                if cell_type is not None:
                    if not isinstance(cell_type, str):
                        raise MediaOpError("cell_type must be a string or None")
                    _normalize_cell_type_for_edit(cell, cell_type)
                if str(cell.get("cell_type") or "") == "code":
                    cell["execution_count"] = None
                    cell["outputs"] = []
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
                "source": new_source,
            }
            if _supports_cell_ids(notebook):
                new_cell["id"] = _new_cell_id(cells)
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


