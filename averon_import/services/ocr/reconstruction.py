"""Reconstruction of Averon OCR rows from Yandex Vision OCR page payloads.

Official page payload shape (model="table", async PDF):

    {"page": {...}, "textAnnotation": {"tables": [...], "blocks": [...]}}

PRIMARY  - ``textAnnotation.tables[].cells[]``: the header row is located by
           known GOST column titles and detected column indexes are mapped
           onto BASE_COLUMNS deterministically (rowSpan/columnSpan aware).
           A well-formed 9-column table without a recognisable header falls
           back to direct positional mapping.
FALLBACK - no/unusable tables: geometric reconstruction from
           ``blocks[].lines[].words[]`` bounding boxes; without a detectable
           header the whole visual line degrades into ``name``.
LAST RESORT - plain text lines from ``fullText``/text.

Yandex does not provide OCR confidence for words or table cells, therefore
no confidence values are ever synthesized here: rows carry an EMPTY
confidences mapping meaning "provider does not report confidence".
Values pass through the shared ``normalize_cell`` exactly once. Output is
strictly the internal :class:`OcrRow` DTO with BASE_COLUMNS keys only.
"""

from __future__ import annotations

from statistics import median

from averon_import.core.constants import BASE_COLUMNS
from averon_import.core.normalizers import normalize_cell
from averon_import.services.ocr.base import OcrRow

BASE_COLUMN_KEYS: tuple[str, ...] = tuple(column["key"] for column in BASE_COLUMNS)

HEADER_ANCHORS: dict[str, tuple[str, ...]] = {
    "position": ("поз", "№", "no."),
    "name": ("наименован", "характеристик"),
    "type_mark": ("тип", "марка", "обознач"),
    "code": ("код",),
    "manufacturer": ("производит",),
    "unit": ("ед.", "изм", "единиц"),
    "quantity": ("кол",),
    "mass": ("масса",),
    "note": ("прим",),
}

_MAX_HEADER_ROWS = 5


# ------------------------------------------------------------- shared helpers


def _match_header_key(text: str) -> str | None:
    lowered = text.lower().strip().strip(".:;№ ")
    for key, patterns in HEADER_ANCHORS.items():
        for pattern in patterns:
            if lowered.startswith(pattern) or pattern in lowered:
                return key
    return None


def _fractional_bbox(vertices, denominator_x: float, denominator_y: float) -> dict:
    xs = [point[0] for point in vertices]
    ys = [point[1] for point in vertices]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    dx = denominator_x or max(right, 1.0)
    dy = denominator_y or max(bottom, 1.0)
    return {
        "x": round(left / dx, 4),
        "y": round(top / dy, 4),
        "width": round((right - left) / dx, 4),
        "height": round((bottom - top) / dy, 4),
    }


def _vertices_of(box: dict | None) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for vertex in (box or {}).get("vertices") or []:
        try:
            points.append((float(vertex.get("x")), float(vertex.get("y"))))
        except (TypeError, ValueError, AttributeError):
            return []
    return points


# ------------------------------------------------------------ tables (PRIMARY)


def _table_cells(table: dict) -> list[dict]:
    return [cell for cell in (table.get("cells") or []) if isinstance(cell, dict)]


def _cell_covering(
    grid: dict[tuple[int, int], dict], row: int, column: int
) -> dict | None:
    for (start_row, start_column), cell in grid.items():
        row_span = max(1, int(cell.get("rowSpan", 1) or 1))
        column_span = max(1, int(cell.get("columnSpan", 1) or 1))
        if (
            start_row <= row < start_row + row_span
            and start_column <= column < start_column + column_span
        ):
            return cell
    return None


def _match_row_anchors(grid, row: int, column_count: int) -> dict[int, str]:
    matched: dict[int, str] = {}
    for column in range(max(column_count, 1)):
        cell = _cell_covering(grid, row, column)
        if not cell:
            continue
        key = _match_header_key(str(cell.get("text", "")))
        if key and key not in matched.values():
            matched[column] = key
    return matched


def _cell_text(cell: dict) -> str:
    segments = cell.get("textSegments")
    if isinstance(segments, list) and segments:
        parts = [
            str(segment.get("text", ""))
            for segment in segments
            if isinstance(segment, dict)
        ]
        joined = " ".join(part for part in parts if part.strip())
        if joined.strip():
            return joined
    return str(cell.get("text", ""))


def rows_from_tables(
    tables: list,
    denominator_x: float,
    denominator_y: float,
    provider_key: str,
) -> list[OcrRow] | None:
    """PRIMARY path: convert Yandex table cells into OcrRow items.

    Returns None when the table structure cannot be mapped reliably;
    callers fall back to geometric reconstruction then.
    """
    candidates = [
        (table, _table_cells(table)) for table in tables if isinstance(table, dict)
    ]
    candidates = [(table, cells) for table, cells in candidates if cells]
    if not candidates:
        return None
    table, cells = max(candidates, key=lambda item: len(item[1]))

    grid: dict[tuple[int, int], dict] = {}
    for cell in cells:
        try:
            row = int(cell.get("rowIndex", 0))
            column = int(cell.get("columnIndex", 0))
        except (TypeError, ValueError):
            return None
        grid[(row, column)] = cell
    max_row = max(row for row, _ in grid)
    try:
        column_count = int(table.get("columnCount") or 0)
    except (TypeError, ValueError):
        column_count = 0

    header_mapping: dict[int, str] | None = None
    header_row = -1
    for row in range(0, min(max_row, _MAX_HEADER_ROWS - 1) + 1):
        matched = _match_row_anchors(grid, row, column_count)
        if len(matched) >= 3 and "name" in matched.values():
            header_mapping = matched
            header_row = row
            break

    if header_mapping is None and column_count == len(BASE_COLUMN_KEYS):
        header_mapping = dict(enumerate(BASE_COLUMN_KEYS))
        if _match_row_anchors(grid, 0, column_count):
            header_row = 0
    if header_mapping is None:
        return None

    rows: list[OcrRow] = []
    source_row = 1
    for row in range(header_row + 1, max_row + 1):
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        vertices: list[tuple[float, float]] = []
        for (start_row, start_column), cell in sorted(grid.items()):
            row_span = max(1, int(cell.get("rowSpan", 1) or 1))
            if not (start_row <= row < start_row + row_span):
                continue
            key = header_mapping.get(start_column)
            if key is None or key in values:
                continue
            normalized = normalize_cell(key, _cell_text(cell))
            if not normalized:
                continue
            values[key] = normalized
            sources[key] = provider_key
            vertices.extend(_vertices_of(cell.get("boundingBox")))
        if not values:
            continue
        rows.append(
            OcrRow(
                source_row=source_row,
                values=values,
                confidences={},
                sources=sources,
                bbox=_fractional_bbox(vertices, denominator_x, denominator_y)
                if vertices
                else {},
            )
        )
        source_row += 1
    return rows


# ---------------------------------------------- words/geometry (FALLBACK path)


def _word_vertices(word: dict) -> list[tuple[float, float]]:
    return _vertices_of(word.get("boundingBox"))


def collect_words(page: dict) -> list[dict]:
    """Flatten blocks/lines/words into word records with geometry."""
    words: list[dict] = []
    has_geometry = False
    for block in page.get("blocks") or []:
        for line in block.get("lines") or []:
            for word in line.get("words") or []:
                text = str(word.get("text", "")).strip()
                if not text:
                    continue
                vertices = _word_vertices(word)
                if vertices:
                    has_geometry = True
                words.append({"text": text, "vertices": vertices})
    if not has_geometry:
        for word in words:
            word["vertices"] = []
    return words


def _bbox_of(vertices: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in vertices]
    ys = [point[1] for point in vertices]
    return min(xs), min(ys), max(xs), max(ys)


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    ordered = sorted(
        words,
        key=lambda item: (
            _bbox_of(item["vertices"])[1],
            _bbox_of(item["vertices"])[0],
        ),
    )
    heights = [
        _bbox_of(item["vertices"])[3] - _bbox_of(item["vertices"])[1]
        for item in ordered
    ]
    typical_height = median([height for height in heights if height > 0] or [10.0])
    tolerance = max(typical_height * 0.6, 1e-6)
    lines: list[list[dict]] = []
    current: list[dict] = []
    current_center = 0.0
    for word in ordered:
        _, top, _, bottom = _bbox_of(word["vertices"])
        center = (top + bottom) / 2
        if current and abs(center - current_center) > tolerance:
            lines.append(current)
            current = []
        if not current:
            current_center = center
        else:
            centers = [
                sum(_bbox_of(item["vertices"])[1::2]) / 2 for item in current
            ]
            current_center = sum(centers) / len(centers)
        current.append(word)
    if current:
        lines.append(current)
    for line in lines:
        line.sort(key=lambda item: _bbox_of(item["vertices"])[0])
    return lines


def _header_anchors(line_words: list[dict]) -> dict[str, float]:
    anchors: dict[str, float] = {}
    for word in line_words:
        key = _match_header_key(word["text"])
        if key and key not in anchors:
            left, _, right, _ = _bbox_of(word["vertices"])
            anchors[key] = (left + right) / 2
    return anchors


def _column_ranges(anchor_centers: dict[str, float]) -> dict[str, tuple[float, float]]:
    ordered = sorted(anchor_centers.items(), key=lambda item: item[1])
    ranges: dict[str, tuple[float, float]] = {}
    for index, (key, center) in enumerate(ordered):
        start = 0.0 if index == 0 else (ordered[index - 1][1] + center) / 2
        end = 1.0 if index == len(ordered) - 1 else (center + ordered[index + 1][1]) / 2
        ranges[key] = (start, end)
    return ranges


def _rows_from_geometry(words: list[dict], page: dict, provider_key: str) -> list[OcrRow]:
    lines = _cluster_lines(words)
    page_width = float(page.get("width") or 0)
    page_height = float(page.get("height") or 0)
    denominator_x = page_width or max(
        (_bbox_of(word["vertices"])[2] for word in words), default=0.0
    ) or 1.0
    denominator_y = page_height or max(
        (_bbox_of(word["vertices"])[3] for word in words), default=0.0
    ) or 1.0

    anchors: dict[str, float] = {}
    header_line_count = 0
    for line in lines[:_MAX_HEADER_ROWS]:
        found = _header_anchors(line)
        merged = dict(anchors)
        merged.update(found)
        if found:
            anchors = merged
            header_line_count += 1
        elif anchors:
            break
        if len(anchors) >= 5:
            break

    data_lines = lines[header_line_count:] if anchors else lines
    ranges = (
        _column_ranges(
            {key: center / denominator_x for key, center in anchors.items()}
        )
        if len(anchors) >= 3
        else None
    )

    rows: list[OcrRow] = []
    source_row = 1
    for line in data_lines:
        cells: dict[str, list[dict]] = {key: [] for key in BASE_COLUMN_KEYS}
        if ranges:
            for word in line:
                center = sum(_bbox_of(word["vertices"])[0::2]) / 2
                fraction = center / denominator_x
                for key, (start, end) in ranges.items():
                    if start <= fraction < end or (end == 1.0 and fraction >= start):
                        cells[key].append(word)
                        break
        else:
            cells["name"].extend(line)
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        vertices: list[tuple[float, float]] = []
        for key in BASE_COLUMN_KEYS:
            members = cells[key]
            if not members:
                continue
            raw_text = " ".join(member["text"] for member in members)
            normalized = normalize_cell(key, raw_text)
            if not normalized:
                continue
            values[key] = normalized
            sources[key] = provider_key
            for member in members:
                vertices.extend(member["vertices"])
        if not values:
            continue
        rows.append(
            OcrRow(
                source_row=source_row,
                values=values,
                confidences={},
                sources=sources,
                bbox=_fractional_bbox(vertices, denominator_x, denominator_y)
                if vertices
                else {},
            )
        )
        source_row += 1
    return rows


# ------------------------------------------------------------- plain text path


def _rows_from_plain_text(page: dict, provider_key: str) -> list[OcrRow]:
    full_text = ""
    full_text_block = page.get("fullText")
    if isinstance(full_text_block, dict):
        full_text = str(full_text_block.get("text", ""))
    elif isinstance(full_text_block, str):
        full_text = full_text_block
    if not full_text:
        # page may be the already-flattened textAnnotation view
        raw_text = page.get("text")
        if isinstance(raw_text, str):
            full_text = raw_text
    if not full_text:
        text_annotation = page.get("textAnnotation")
        if isinstance(text_annotation, dict):
            full_text = str(text_annotation.get("text", ""))
    rows: list[OcrRow] = []
    source_row = 1
    for raw_line in full_text.splitlines():
        normalized = normalize_cell("name", raw_line.strip())
        if not normalized:
            continue
        rows.append(
            OcrRow(
                source_row=source_row,
                values={"name": normalized},
                confidences={},
                sources={"name": provider_key},
                bbox={},
            )
        )
        source_row += 1
    return rows


# ------------------------------------------------------------------- dispatch


def _page_dimensions(payload: dict) -> tuple[float, float]:
    page_block = payload.get("page")
    if isinstance(page_block, dict):
        try:
            return (
                float(page_block.get("width") or 0),
                float(page_block.get("height") or 0),
            )
        except (TypeError, ValueError):
            pass
    text_annotation = payload.get("textAnnotation")
    if isinstance(text_annotation, dict):
        try:
            return (
                float(text_annotation.get("width") or 0),
                float(text_annotation.get("height") or 0),
            )
        except (TypeError, ValueError):
            pass
    return 0.0, 0.0


def page_geometry(payload: dict) -> dict:
    width, height = _page_dimensions(payload)
    geometry: dict = {}
    if width:
        geometry["width"] = width
    if height:
        geometry["height"] = height
    return geometry


def reconstruct_page_rows(payload: dict, provider_key: str) -> list[OcrRow]:
    """Convert one Yandex Vision page payload into internal OCR rows.

    Tables are the primary source; geometric word reconstruction is the
    fallback; plain text is the last resort.
    """
    text_annotation = payload.get("textAnnotation")
    text_annotation = text_annotation if isinstance(text_annotation, dict) else {}

    tables = text_annotation.get("tables")
    width, height = _page_dimensions(payload)
    if isinstance(tables, list) and tables:
        rows = rows_from_tables(tables, width, height, provider_key)
        if rows is not None:
            return rows

    page_view = dict(text_annotation)
    for key in ("blocks", "fullText"):
        if key not in page_view and key in payload:
            page_view[key] = payload[key]
    page_view.setdefault("width", width)
    page_view.setdefault("height", height)

    words = collect_words(page_view)
    geometric = [word for word in words if word["vertices"]]
    if geometric:
        return _rows_from_geometry(words, page_view, provider_key)
    return _rows_from_plain_text(page_view, provider_key)
