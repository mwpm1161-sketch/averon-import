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

from dataclasses import dataclass, field
import re
from statistics import median

from averon_import.core.constants import BASE_COLUMNS
from averon_import.core.normalizers import normalize_cell, numeric_cell_metadata
from averon_import.services.ocr.base import OcrRow

BASE_COLUMN_KEYS: tuple[str, ...] = tuple(column["key"] for column in BASE_COLUMNS)

HEADER_ANCHORS: dict[str, tuple[str, ...]] = {
    "position": ("поз", "позици", "номер", "no"),
    "name": ("наименован", "характерист"),
    "type_mark": ("тип", "марка", "обознач", "документ"),
    "code": ("код", "оборуд", "издел", "материал"),
    "manufacturer": ("производит", "завод", "изготовит", "поставщик"),
    "unit": ("единиц", "ед", "измер", "езмер", "езме", "edu", "ниц"),
    "quantity": ("кол", "колич"),
    "mass": ("масса", "вес"),
    "note": ("примеч", "примечан"),
}

_MAX_HEADER_ROWS = 5


@dataclass(slots=True)
class DetectedCell:
    """Provider-neutral cell kept between OCR and semantic mapping."""

    row_index: int
    column_index: int
    row_span: int
    column_span: int
    text: str
    bbox: dict
    source: str


@dataclass(slots=True)
class DetectedRow:
    row_index: int
    cells: list[DetectedCell]
    inferred_split: bool = False


@dataclass(slots=True)
class DetectedTable:
    rows: list[DetectedRow]
    column_count: int
    source_table_index: int
    header_rows: set[int] = field(default_factory=set)
    column_mapping: dict[int, tuple[str, ...]] = field(default_factory=dict)
    review_reasons: list[str] = field(default_factory=list)


# ------------------------------------------------------------- shared helpers


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


def _field(cell, name: str, default=None):
    if isinstance(cell, DetectedCell):
        return getattr(cell, name, default)
    if isinstance(cell, dict):
        return cell.get(name, default)
    return default


def _table_cells(table: dict, source: str) -> list[DetectedCell]:
    cells: list[DetectedCell] = []
    for raw in table.get("cells") or []:
        if not isinstance(raw, dict):
            continue
        try:
            row_index = int(raw.get("rowIndex", 0))
            column_index = int(raw.get("columnIndex", 0))
            row_span = max(1, int(raw.get("rowSpan", 1) or 1))
            column_span = max(1, int(raw.get("columnSpan", 1) or 1))
        except (TypeError, ValueError):
            continue
        cells.append(
            DetectedCell(
                row_index=row_index,
                column_index=column_index,
                row_span=row_span,
                column_span=column_span,
                text=_cell_text(raw),
                bbox=dict(raw.get("boundingBox") or {}),
                source=source,
            )
        )
    return cells


def _cell_covering(
    grid: dict[tuple[int, int], DetectedCell | dict], row: int, column: int
) -> DetectedCell | dict | None:
    for (start_row, start_column), cell in grid.items():
        row_span = max(1, int(_field(cell, "row_span", _field(cell, "rowSpan", 1)) or 1))
        column_span = max(1, int(_field(cell, "column_span", _field(cell, "columnSpan", 1)) or 1))
        if (
            start_row <= row < start_row + row_span
            and start_column <= column < start_column + column_span
        ):
            return cell
    return None


def _header_text(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(text).lower().replace("ё", "е"))


def _match_header_keys(text: str) -> tuple[str, ...]:
    lowered = _header_text(text)
    matched: list[str] = []
    for key, patterns in HEADER_ANCHORS.items():
        if any(
            lowered.startswith(pattern) if len(pattern) <= 2 else pattern in lowered
            for pattern in patterns
        ):
            matched.append(key)
    return tuple(matched)


def _match_header_key(text: str) -> str | None:
    keys = _match_header_keys(text)
    return keys[0] if keys else None


def _match_row_anchors(
    grid: dict[tuple[int, int], DetectedCell | dict], row: int, column_count: int
) -> dict[int, tuple[str, ...]]:
    matched: dict[int, tuple[str, ...]] = {}
    for column in range(max(column_count, 1)):
        cell = _cell_covering(grid, row, column)
        if not cell:
            continue
        keys = _match_header_keys(_cell_text(cell))
        if keys:
            matched[column] = keys
    return matched


def _cell_text(cell: DetectedCell | dict) -> str:
    if isinstance(cell, DetectedCell):
        return cell.text
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


def _is_numbering_row(
    grid: dict[tuple[int, int], DetectedCell | dict], row: int, column_count: int
) -> bool:
    texts: list[str] = []
    for column in range(max(column_count, 1)):
        cell = _cell_covering(grid, row, column)
        if not cell:
            continue
        text = _cell_text(cell).strip()
        if text:
            texts.append(text)
    minimum = max(3, (column_count + 1) // 2)
    if len(texts) < minimum:
        return False
    return all(re.fullmatch(r"[1-9]", text) for text in texts)


def _build_detected_table(table: dict, source: str, source_table_index: int) -> DetectedTable | None:
    cells = _table_cells(table, source)
    if not cells:
        return None
    try:
        column_count = int(table.get("columnCount") or 0)
    except (TypeError, ValueError):
        column_count = 0
    rows_by_index: dict[int, list[DetectedCell]] = {}
    for cell in cells:
        rows_by_index.setdefault(cell.row_index, []).append(cell)
    return DetectedTable(
        rows=[DetectedRow(row_index=index, cells=sorted(row, key=lambda item: item.column_index))
              for index, row in sorted(rows_by_index.items())],
        column_count=column_count,
        source_table_index=source_table_index,
    )


def _table_header_mapping(table: DetectedTable) -> tuple[dict[int, tuple[str, ...]] | None, set[int]]:
    if not table.rows or table.column_count <= 0:
        return None, set()
    grid = {
        (cell.row_index, cell.column_index): cell
        for row in table.rows
        for cell in row.cells
    }
    max_row = max(row.row_index for row in table.rows)
    scan_limit = min(max_row, _MAX_HEADER_ROWS - 1)
    for start in range(scan_limit + 1):
        mapping: dict[int, list[str]] = {}
        header_rows: set[int] = set()
        for row_index in range(start, min(max_row, start + 2) + 1):
            matched = _match_row_anchors(grid, row_index, table.column_count)
            if row_index == start and not matched:
                break
            if matched:
                header_rows.add(row_index)
                for column, keys in matched.items():
                    mapping.setdefault(column, [])
                    for key in keys:
                        if key not in mapping[column]:
                            mapping[column].append(key)
                continue
            if _is_numbering_row(grid, row_index, table.column_count):
                header_rows.add(row_index)
                continue
            if row_index > start:
                break
        unique_keys = {key for keys in mapping.values() for key in keys}
        if len(unique_keys) < 3 or "name" not in unique_keys:
            continue
        inverse: dict[str, int] = {}
        ambiguous = False
        for column, keys in mapping.items():
            for key in keys:
                previous = inverse.get(key)
                if previous is not None and previous != column:
                    ambiguous = True
                inverse[key] = column
        if ambiguous:
            table.review_reasons.append("ambiguous_columns")
            return None, header_rows
        return {column: tuple(keys) for column, keys in mapping.items()}, header_rows

    if table.column_count == len(BASE_COLUMN_KEYS):
        grid = {
            (cell.row_index, cell.column_index): cell
            for row in table.rows
            for cell in row.cells
        }
        header_rows = {0} if _is_numbering_row(grid, 0, table.column_count) else set()
        return {index: (key,) for index, key in enumerate(BASE_COLUMN_KEYS)}, header_rows
    table.review_reasons.append("ambiguous_columns")
    return None, set()


def _bbox_height(bbox: dict) -> float:
    vertices = _vertices_of(bbox)
    if not vertices:
        return 0.0
    return max(y for _, y in vertices) - min(y for _, y in vertices)


def _split_bbox(bbox: dict, index: int, count: int) -> dict:
    vertices = _vertices_of(bbox)
    if not vertices or count <= 1:
        return dict(bbox)
    left = min(x for x, _ in vertices)
    right = max(x for x, _ in vertices)
    top = min(y for _, y in vertices)
    bottom = max(y for _, y in vertices)
    part_top = top + (bottom - top) * index / count
    part_bottom = top + (bottom - top) * (index + 1) / count
    return {"vertices": [
        {"x": left, "y": part_top},
        {"x": right, "y": part_top},
        {"x": right, "y": part_bottom},
        {"x": left, "y": part_bottom},
    ]}


def _expand_data_rows(table: DetectedTable) -> list[DetectedRow]:
    grid = {
        (cell.row_index, cell.column_index): cell
        for row in table.rows
        for cell in row.cells
    }

    def logical_cells(row_index: int) -> list[DetectedCell]:
        cells: list[DetectedCell] = []
        seen: set[int] = set()
        for column in range(max(table.column_count, 1)):
            cell = _cell_covering(grid, row_index, column)
            if cell is None or id(cell) in seen:
                continue
            seen.add(id(cell))
            cells.append(cell)
        return sorted(cells, key=lambda item: item.column_index)

    data_rows = [row for row in table.rows if row.row_index not in table.header_rows]
    heights = [
        max((_bbox_height(cell.bbox) for cell in logical_cells(row.row_index)), default=0.0)
        for row in data_rows
    ]
    typical_height = median([height for height in heights if height > 0] or [0.0])
    expanded: list[DetectedRow] = []
    for row in data_rows:
        row_cells = logical_cells(row.row_index)
        line_counts = [max(1, len(cell.text.splitlines())) for cell in row_cells]
        max_lines = max(line_counts, default=1)
        row_height = max((_bbox_height(cell.bbox) for cell in row_cells), default=0.0)
        should_split = (
            max_lines > 1
            and row_height > 0
            and typical_height > 0
            and row_height >= typical_height * 1.55
            and sum(count > 1 for count in line_counts) >= 2
        )
        if not should_split:
            expanded.append(row)
            continue
        for line_index in range(max_lines):
            split_cells: list[DetectedCell] = []
            for cell, line_count in zip(row_cells, line_counts):
                parts = cell.text.splitlines() or [""]
                text = parts[line_index] if line_index < len(parts) else ""
                split_cells.append(
                    DetectedCell(
                        row_index=row.row_index,
                        column_index=cell.column_index,
                        row_span=cell.row_span,
                        column_span=cell.column_span,
                        text=text,
                        bbox=_split_bbox(cell.bbox, line_index, max_lines),
                        source=cell.source,
                    )
                )
            expanded.append(DetectedRow(row.row_index, split_cells, inferred_split=True))
    return expanded


def _logical_row_cells(
    table: DetectedTable, row_index: int
) -> list[DetectedCell]:
    """Return cells visible in a row, including row/col-span coverage."""
    grid = {
        (cell.row_index, cell.column_index): cell
        for row in table.rows
        for cell in row.cells
    }
    cells: list[DetectedCell] = []
    seen: set[int] = set()
    for column in range(max(table.column_count, 1)):
        cell = _cell_covering(grid, row_index, column)
        if cell is None or id(cell) in seen:
            continue
        seen.add(id(cell))
        cells.append(cell)
    return sorted(cells, key=lambda item: item.column_index)


def _table_cell_keys(
    mapping: dict[int, tuple[str, ...]], cell: DetectedCell
) -> tuple[str, ...]:
    return mapping.get(cell.column_index, ())


def rows_from_tables(
    tables: list,
    denominator_x: float,
    denominator_y: float,
    provider_key: str,
) -> list[OcrRow] | None:
    """PRIMARY path: build a neutral grid, then map it to BASE_COLUMNS."""
    candidates = [
        _build_detected_table(table, provider_key, index)
        for index, table in enumerate(tables)
        if isinstance(table, dict)
    ]
    candidates = [table for table in candidates if table and table.rows]
    if not candidates:
        return None
    table = max(candidates, key=lambda item: sum(len(row.cells) for row in item.rows))
    mapping, header_rows = _table_header_mapping(table)
    table.header_rows = header_rows
    if mapping is None:
        return None
    table.column_mapping = mapping

    rows: list[OcrRow] = []
    for source_row, detected_row in enumerate(_expand_data_rows(table), start=1):
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        raw_values: dict[str, str] = {}
        normalization: dict[str, dict[str, object]] = {}
        cell_bboxes: dict[str, dict] = {}
        vertices: list[tuple[float, float]] = []
        row_cells = (
            detected_row.cells
            if detected_row.inferred_split
            else _logical_row_cells(table, detected_row.row_index)
        )
        for cell in row_cells:
            covered_columns = range(
                cell.column_index,
                cell.column_index + max(1, cell.column_span),
            )
            # A row/column-spanned body cell inherits the semantic mapping of
            # every covered physical column. Preserve key order and avoid
            # assigning the same text twice to one output field.
            cell_keys: list[str] = []
            for column in covered_columns:
                for key in mapping.get(column, ()):
                    if key not in cell_keys:
                        cell_keys.append(key)
            keys = tuple(cell_keys)
            if not keys:
                continue
            raw_text = cell.text
            for key in keys:
                if key not in cell_bboxes:
                    cell_vertices = _vertices_of(cell.bbox)
                    if cell_vertices:
                        cell_bboxes[key] = _fractional_bbox(
                            cell_vertices, denominator_x, denominator_y
                        )
                if key in values:
                    continue
                if key in {"mass", "note"} and len(keys) > 1:
                    if key == "mass" and not re.fullmatch(r"-?\d+(?:[.,]\d+)?", raw_text.strip()):
                        continue
                    if key == "note" and re.fullmatch(r"-?\d+(?:[.,]\d+)?", raw_text.strip()):
                        continue
                candidate = normalize_cell(key, raw_text)
                if not candidate:
                    continue
                values[key] = candidate
                raw_values[key] = raw_text
                sources[key] = provider_key
                if key in {"quantity", "mass"}:
                    normalization[key] = numeric_cell_metadata(raw_text)
                vertices.extend(_vertices_of(cell.bbox))
        if not values:
            continue
        review_reasons = list(table.review_reasons)
        for details in normalization.values():
            if details.get("numeric_suspect") and "numeric_suspect" not in review_reasons:
                review_reasons.append("numeric_suspect")
        rows.append(
            OcrRow(
                source_row=source_row,
                values=values,
                confidences={},
                sources=sources,
                bbox=_fractional_bbox(vertices, denominator_x, denominator_y)
                if vertices
                else {},
                metadata={
                    "provider": provider_key,
                    "structured_table": True,
                    "provider_has_explicit_rows": True,
                    "source_table_index": table.source_table_index,
                    "source_row_index": detected_row.row_index,
                    "column_mapping": {
                        str(column): list(keys) for column, keys in mapping.items()
                    },
                    "raw_values": raw_values,
                    "normalization": normalization,
                    "cell_bboxes": cell_bboxes,
                    "review_reasons": review_reasons,
                },
            )
        )
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


def _rows_from_geometry(
    words: list[dict],
    page: dict,
    provider_key: str,
    review_reasons: list[str] | None = None,
) -> list[OcrRow]:
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
        raw_values: dict[str, str] = {}
        normalization: dict[str, dict[str, object]] = {}
        cell_bboxes: dict[str, dict] = {}
        row_review_reasons = list(review_reasons or [])
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
            raw_values[key] = raw_text
            sources[key] = provider_key
            if key in {"quantity", "mass"}:
                details = numeric_cell_metadata(raw_text)
                normalization[key] = details
                if details.get("numeric_suspect") and "numeric_suspect" not in row_review_reasons:
                    row_review_reasons.append("numeric_suspect")
            for member in members:
                vertices.extend(member["vertices"])
            if vertices:
                cell_bboxes[key] = _fractional_bbox(
                    [point for member in members for point in member["vertices"]],
                    denominator_x,
                    denominator_y,
                )
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
                metadata={
                    "provider": provider_key,
                    "structured_table": False,
                    "provider_has_explicit_rows": False,
                    "raw_values": raw_values,
                    "normalization": normalization,
                    "cell_bboxes": cell_bboxes,
                    "review_reasons": row_review_reasons,
                },
            )
        )
        source_row += 1
    return rows


# ------------------------------------------------------------- plain text path


def _rows_from_plain_text(
    page: dict,
    provider_key: str,
    review_reasons: list[str] | None = None,
) -> list[OcrRow]:
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
                metadata={
                    "provider": provider_key,
                    "structured_table": False,
                    "provider_has_explicit_rows": False,
                    "review_reasons": list(review_reasons or []),
                },
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


def _table_fallback_reasons(tables: list) -> list[str]:
    """Explain why a detected table could not be mapped semantically."""
    reasons: list[str] = []
    for index, raw_table in enumerate(tables):
        if not isinstance(raw_table, dict):
            continue
        table = _build_detected_table(raw_table, "yandex_vision", index)
        if not table:
            continue
        mapping, _ = _table_header_mapping(table)
        if mapping is None:
            for reason in table.review_reasons or ["malformed_table"]:
                if reason not in reasons:
                    reasons.append(reason)
    return reasons or ["malformed_table"]


def reconstruct_page_rows(payload: dict, provider_key: str) -> list[OcrRow]:
    """Convert one Yandex Vision page payload into internal OCR rows.

    Tables are the primary source; geometric word reconstruction is the
    fallback; plain text is the last resort.
    """
    text_annotation = payload.get("textAnnotation")
    text_annotation = text_annotation if isinstance(text_annotation, dict) else {}

    tables = text_annotation.get("tables")
    width, height = _page_dimensions(payload)
    fallback_reasons: list[str] = []
    if isinstance(tables, list) and tables:
        rows = rows_from_tables(tables, width, height, provider_key)
        if rows is not None:
            return rows
        fallback_reasons = _table_fallback_reasons(tables)

    page_view = dict(text_annotation)
    for key in ("blocks", "fullText"):
        if key not in page_view and key in payload:
            page_view[key] = payload[key]
    page_view.setdefault("width", width)
    page_view.setdefault("height", height)

    words = collect_words(page_view)
    geometric = [word for word in words if word["vertices"]]
    if geometric:
        return _rows_from_geometry(words, page_view, provider_key, fallback_reasons)
    return _rows_from_plain_text(page_view, provider_key, fallback_reasons)
