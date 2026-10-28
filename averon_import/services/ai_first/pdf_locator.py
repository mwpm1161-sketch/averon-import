"""Fail-closed mapping from an AI-first row to exact searchable PDF text."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Sequence

import fitz

from averon_import.services.ai_first.contracts import AIDocumentDraft


_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
MAX_PAGE_WORDS = 50_000
MAX_MATCH_TOKENS = 500


def _tokens(value: object) -> list[str]:
    return [item.casefold().replace("ё", "е") for item in _TOKEN_RE.findall(str(value or ""))]


def _row_for_id(draft: AIDocumentDraft, record_id: str):
    for page in draft.pages:
        for table in page.tables:
            for row in table.rows:
                if row.id == record_id:
                    return page, table, row
    return None


def _observed_tokens(cells: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for cell in cells:
        result.extend(_tokens(cell.raw_value))
    return result


def _pdf_token_stream(page: fitz.Page) -> list[tuple[str, fitz.Rect]]:
    words = page.get_text("words", sort=True)
    if len(words) > MAX_PAGE_WORDS:
        return []
    rotation = page.rotation_matrix
    stream: list[tuple[str, fitz.Rect]] = []
    for word in words:
        if len(word) < 5:
            continue
        bounds = fitz.Rect(word[:4]) * rotation
        if not bounds.is_valid or bounds.is_empty:
            continue
        for token in _tokens(word[4]):
            stream.append((token, bounds))
    return stream


def _unique_span(stream: Sequence[tuple[str, fitz.Rect]], target: Sequence[str]) -> tuple[int, int] | None:
    if not target or len(target) > MAX_MATCH_TOKENS:
        return None
    if len(target) == 1 and len(target[0]) < 3:
        return None
    matches = []
    last_start = len(stream) - len(target)
    for start in range(max(0, last_start + 1)):
        if all(stream[start + offset][0] == token for offset, token in enumerate(target)):
            matches.append((start, start + len(target)))
            if len(matches) > 1:
                return None
    return matches[0] if len(matches) == 1 else None


def locate_ai_first_pdf_bbox(
    pdf_path: str | Path,
    draft: AIDocumentDraft,
    record_id: str,
    *,
    cell_index: int | None = None,
) -> dict[str, Any]:
    """Return a normalized bbox only for one unique exact text-layer match.

    Model-proposed bbox values are deliberately not used as proof. Image-only
    pages and ambiguous matches return `matched=False`, so the UI shows the
    source page without a rectangle.
    """
    found = _row_for_id(draft, record_id)
    if found is None:
        return {"matched": False, "reason": "row_not_found"}
    page_extract, _table, row = found
    if page_extract.status != "extracted":
        return {"matched": False, "reason": "page_not_extracted"}
    cells = sorted(row.raw_cells, key=lambda item: item.column_index)
    if cell_index is not None:
        cell = next((item for item in cells if item.column_index == cell_index), None)
        if cell is None or not _tokens(cell.raw_value):
            return {"matched": False, "reason": "cell_text_missing"}
        target = _tokens(cell.raw_value)
    else:
        target = _observed_tokens(cells)
        # A row match must carry enough evidence to distinguish it from page
        # numbering and short service labels.
        if len(target) < 3 or len({token for token in target if len(token) > 1}) < 2:
            return {"matched": False, "reason": "insufficient_text_evidence"}

    try:
        document = fitz.open(pdf_path)
    except (OSError, ValueError, RuntimeError):
        return {"matched": False, "reason": "source_pdf_unavailable"}
    try:
        page_number = page_extract.page_number
        if page_number < 1 or page_number > document.page_count:
            return {"matched": False, "reason": "source_page_unavailable"}
        page = document.load_page(page_number - 1)
        stream = _pdf_token_stream(page)
        if not stream:
            return {"matched": False, "reason": "text_layer_unavailable"}
        span = _unique_span(stream, target)
        if span is None:
            return {"matched": False, "reason": "text_match_missing_or_ambiguous"}
        matched_words = [bounds for _token, bounds in stream[span[0]:span[1]]]
        rect = fitz.Rect(
            min(item.x0 for item in matched_words),
            min(item.y0 for item in matched_words),
            max(item.x1 for item in matched_words),
            max(item.y1 for item in matched_words),
        )
        page_rect = page.rect
        if (page_rect.width <= 0 or page_rect.height <= 0 or not rect.is_valid or rect.is_empty
                or rect.x0 < page_rect.x0 or rect.y0 < page_rect.y0
                or rect.x1 > page_rect.x1 or rect.y1 > page_rect.y1):
            return {"matched": False, "reason": "text_bbox_invalid"}
        bbox = {
            "x": (rect.x0 - page_rect.x0) / page_rect.width,
            "y": (rect.y0 - page_rect.y0) / page_rect.height,
            "width": rect.width / page_rect.width,
            "height": rect.height / page_rect.height,
        }
        if not all(math.isfinite(value) for value in bbox.values()):
            return {"matched": False, "reason": "text_bbox_invalid"}
        if not (0 <= bbox["x"] < 1 and 0 <= bbox["y"] < 1
                and 0 < bbox["width"] <= 1 and 0 < bbox["height"] <= 1
                and bbox["x"] + bbox["width"] <= 1.000001
                and bbox["y"] + bbox["height"] <= 1.000001):
            return {"matched": False, "reason": "text_bbox_invalid"}
        return {
            "matched": True,
            "bbox": {key: round(value, 6) for key, value in bbox.items()},
            "basis": "unique_exact_pdf_text_layer",
            "page_number": page_number,
        }
    finally:
        document.close()


__all__ = ["locate_ai_first_pdf_bbox"]
