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
    # Keep the unit anchor tied to the beginning of a unit header (or to the
    # OCR-fragmented ``edu``/``езме`` forms).  A broad ``единиц``/``ниц``
    # substring also matches ``Масса единицы`` and makes the 8-column
    # ``mass + note`` provider column look like a second unit column.
    "unit": ("ед", "измер", "езмер", "езме", "edu"),
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
    source_cell_index: int = -1


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
    bbox: dict = field(default_factory=dict)
    header_rows: set[int] = field(default_factory=set)
    column_mapping: dict[int, tuple[str, ...]] = field(default_factory=dict)
    review_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PhysicalCell:
    """Immutable view of one provider cell before semantic projection."""

    source_cell_index: int
    source_row_index: int
    column_index: int
    row_span: int
    column_span: int
    raw_text: str
    bbox: dict
    words: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class PhysicalRow:
    """One Yandex source row; it is not a semantic Averon row."""

    source_table_index: int
    source_row_index: int
    bbox: dict
    cells: tuple[PhysicalCell, ...]


@dataclass(frozen=True, slots=True)
class PhysicalTable:
    """Provider-shaped table preserved before row segmentation."""

    table_index: int
    bbox: dict
    rows: tuple[PhysicalRow, ...]


@dataclass(frozen=True, slots=True)
class PhysicalSubrow:
    """A physical row or geometry-confirmed subrow."""

    source_table_index: int
    source_row_index: int
    source_subrow_index: int
    bbox: dict
    cells: tuple[PhysicalCell, ...]
    split_evidence: tuple[str, ...] = ()
    structural_ambiguity: bool = False


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
    for source_cell_index, raw in enumerate(table.get("cells") or []):
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
                source_cell_index=source_cell_index,
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
        bbox=dict(table.get("boundingBox") or {}),
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
        resolved: dict[int, list[str]] = {}
        ambiguous = False
        for column, keys in mapping.items():
            for key in keys:
                previous = inverse.get(key)
                if previous is not None and previous != column:
                    ambiguous = True
                    continue
                inverse[key] = column
                resolved.setdefault(column, []).append(key)
        if ambiguous:
            if "ambiguous_columns" not in table.review_reasons:
                table.review_reasons.append("ambiguous_columns")
        unique_keys = set(inverse)
        # A standard nine-column table gives us a deterministic local repair
        # for a missing/ambiguous anchor.  It fills only still-unmapped keys;
        # recognized neighboring anchors remain authoritative.
        if table.column_count == len(BASE_COLUMN_KEYS):
            for column, key in enumerate(BASE_COLUMN_KEYS):
                if key in inverse or column in resolved:
                    continue
                resolved[column] = [key]
                inverse[key] = column
            unique_keys = set(inverse)
        if len(unique_keys) < 3 or "name" not in unique_keys:
            continue
        return {column: tuple(keys) for column, keys in resolved.items()}, header_rows

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


def _row_bounds(row_cells: list[DetectedCell]) -> tuple[float, float]:
    vertices = [
        vertex
        for cell in row_cells
        for vertex in _vertices_of(cell.bbox)
    ]
    if not vertices:
        return 0.0, 0.0
    return min(y for _, y in vertices), max(y for _, y in vertices)


def _row_is_empty(row_cells: list[DetectedCell]) -> bool:
    return not any(cell.text.strip() for cell in row_cells)


def _mapped_column(mapping: dict[int, tuple[str, ...]] | None, key: str) -> int | None:
    for column, keys in (mapping or {}).items():
        if key in keys:
            return column
    return None


def _tail_service_rows(
    data_rows: list[DetectedRow],
    logical_cells,
    mapping: dict[int, tuple[str, ...]] | None,
    typical_height: float,
) -> set[int]:
    """Return trailing rows that are structurally outside the data band.

    Yandex may include a drawing title block in the same table object as the
    specification.  The decision is based on table geometry and column
    occupancy, not on words such as ``Примечание`` or ``Лист``.  A note row in
    the body still occupies the mapped name column and is therefore retained.
    """
    if not data_rows:
        return set()

    nonempty_positions = [
        index for index, row in enumerate(data_rows)
        if not _row_is_empty(logical_cells(row.row_index))
    ]
    if not nonempty_positions:
        return set()
    last_position = nonempty_positions[-1]
    last_row = data_rows[last_position]
    last_cells = logical_cells(last_row.row_index)
    nonempty_count = sum(bool(cell.text.strip()) for cell in last_cells)
    name_column = _mapped_column(mapping, "name")
    name_text = ""
    if name_column is not None:
        for cell in last_cells:
            if cell.column_index == name_column and cell.text.strip():
                name_text = cell.text.strip()
                break

    previous_blank_count = 0
    for index in range(last_position - 1, -1, -1):
        if _row_is_empty(logical_cells(data_rows[index].row_index)):
            previous_blank_count += 1
            continue
        break

    top, bottom = _row_bounds(last_cells)
    height = bottom - top
    is_large_tail = (
        previous_blank_count >= 2
        and typical_height > 0
        and height >= typical_height * 3.0
    )
    is_off_schema_tail = (
        last_position == len(data_rows) - 1
        and not name_text
        and nonempty_count <= 1
    )
    if is_large_tail or is_off_schema_tail:
        return {last_row.row_index}
    return set()


def _bbox_center(vertices: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not vertices:
        return None
    return (
        (min(x for x, _ in vertices) + max(x for x, _ in vertices)) / 2,
        (min(y for _, y in vertices) + max(y for _, y in vertices)) / 2,
    )


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


def _bounds_of_box(bbox: dict | None) -> tuple[float, float, float, float] | None:
    raw_vertices = (bbox or {}).get("vertices") if isinstance(bbox, dict) else None
    if raw_vertices and isinstance(raw_vertices[0], (tuple, list)):
        try:
            vertices = [(float(point[0]), float(point[1])) for point in raw_vertices]
        except (TypeError, ValueError, IndexError):
            vertices = []
    else:
        vertices = _vertices_of(bbox)
    if not vertices:
        return None
    return (
        min(x for x, _ in vertices),
        min(y for _, y in vertices),
        max(x for x, _ in vertices),
        max(y for _, y in vertices),
    )


def _box_from_bounds(bounds: tuple[float, float, float, float]) -> dict:
    left, top, right, bottom = bounds
    return {"vertices": [
        {"x": left, "y": top},
        {"x": right, "y": top},
        {"x": right, "y": bottom},
        {"x": left, "y": bottom},
    ]}


def _union_bounds(boxes: list[dict]) -> tuple[float, float, float, float] | None:
    bounds = [_bounds_of_box(box) for box in boxes]
    bounds = [item for item in bounds if item is not None]
    if not bounds:
        return None
    return (
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def _word_center(word: dict) -> tuple[float, float] | None:
    return _bbox_center(word.get("vertices") or [])


def _words_in_box(words: list[dict], bbox: dict) -> tuple[dict, ...]:
    bounds = _bounds_of_box(bbox)
    if bounds is None:
        return ()
    left, top, right, bottom = bounds
    result = []
    for word in words:
        center = _word_center(word)
        if center is None:
            continue
        x, y = center
        if left <= x <= right and top <= y <= bottom:
            result.append(word)
    return tuple(result)


def _physical_table_from_detected(
    table: DetectedTable, words: list[dict]
) -> PhysicalTable:
    row_indexes: set[int] = set()
    for row in table.rows:
        row_indexes.add(row.row_index)
        for cell in row.cells:
            row_indexes.update(
                range(cell.row_index, cell.row_index + max(1, cell.row_span))
            )

    physical_rows: list[PhysicalRow] = []
    for row_index in sorted(row_indexes):
        detected_cells = _logical_row_cells(table, row_index)
        cells = tuple(
            PhysicalCell(
                source_cell_index=cell.source_cell_index,
                source_row_index=cell.row_index,
                column_index=cell.column_index,
                row_span=cell.row_span,
                column_span=cell.column_span,
                raw_text=cell.text,
                bbox=dict(cell.bbox),
                words=_words_in_box(words, cell.bbox),
            )
            for cell in detected_cells
        )
        row_bounds = _union_bounds([cell.bbox for cell in cells])
        physical_rows.append(
            PhysicalRow(
                source_table_index=table.source_table_index,
                source_row_index=row_index,
                bbox=_box_from_bounds(row_bounds) if row_bounds else {},
                cells=cells,
            )
        )

    table_bounds = _bounds_of_box(table.bbox)
    if table_bounds is None:
        table_bounds = _union_bounds(
            [row.bbox for row in physical_rows if row.bbox]
        )
    return PhysicalTable(
        table_index=table.source_table_index,
        bbox=_box_from_bounds(table_bounds) if table_bounds else {},
        rows=tuple(physical_rows),
    )


def _secondary_row_bands(
    secondary_payload: dict | None,
    secondary_crop: dict | None,
    page_width: float,
    page_height: float,
    target_bbox: dict,
) -> list[tuple[float, float]]:
    """Project secondary table row boxes into primary page coordinates."""
    if not isinstance(secondary_payload, dict) or page_width <= 0 or page_height <= 0:
        return []
    annotation = secondary_payload.get("textAnnotation")
    if not isinstance(annotation, dict):
        return []
    try:
        secondary_width = float(annotation.get("width") or 0)
        secondary_height = float(annotation.get("height") or 0)
    except (TypeError, ValueError):
        return []
    if secondary_width <= 0 or secondary_height <= 0:
        return []
    tables = [item for item in annotation.get("tables") or [] if isinstance(item, dict)]
    table = max(tables, key=lambda item: len(item.get("cells") or []), default=None)
    if table is None:
        return []
    crop = secondary_crop or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    target = _bounds_of_box(target_bbox)
    if target is None:
        return []
    target_top, target_bottom = target[1], target[3]
    target_height = max(target_bottom - target_top, 1.0)
    by_row: dict[int, list[dict]] = {}
    for cell in table.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        try:
            row_index = int(cell.get("rowIndex", 0))
        except (TypeError, ValueError):
            continue
        box = _bounds_of_box(cell.get("boundingBox") or {})
        if box is None:
            continue
        local_x = crop["x"] + (box[0] / secondary_width) * crop["width"]
        local_y = crop["y"] + (box[1] / secondary_height) * crop["height"]
        local_right = crop["x"] + (box[2] / secondary_width) * crop["width"]
        local_bottom = crop["y"] + (box[3] / secondary_height) * crop["height"]
        by_row.setdefault(row_index, []).append({
            "vertices": [
                {"x": local_x * page_width, "y": local_y * page_height},
                {"x": local_right * page_width, "y": local_y * page_height},
                {"x": local_right * page_width, "y": local_bottom * page_height},
                {"x": local_x * page_width, "y": local_bottom * page_height},
            ]
        })

    result: list[tuple[float, float]] = []
    for row_index in sorted(by_row):
        bounds = _union_bounds(by_row[row_index])
        if bounds is None:
            continue
        top = max(target_top, bounds[1])
        bottom = min(target_bottom, bounds[3])
        if bottom - top >= min(target_height, max(bounds[3] - bounds[1], 1.0)) * 0.25:
            result.append((top, bottom))
    return result


def _primary_word_bands(
    row: PhysicalRow,
    words: list[dict],
    typical_height: float,
) -> list[tuple[float, float]]:
    """Find row bands from word geometry, never from raw newline count."""
    row_bounds = _bounds_of_box(row.bbox)
    if row_bounds is None:
        return []
    row_words = [word for word in words if _word_center(word) is not None]
    row_words = [
        word for word in row_words
        if _bounds_of_box({"vertices": word["vertices"]})
        and row_bounds[0] <= _word_center(word)[0] <= row_bounds[2]
        and row_bounds[1] <= _word_center(word)[1] <= row_bounds[3]
    ]
    if not row_words:
        return []
    line_heights: list[float] = []
    cell_lines: list[list[float]] = []
    for cell in row.cells:
        cell_words = [word for word in cell.words if _word_center(word) is not None]
        if not cell_words:
            continue
        lines = _cluster_lines(cell_words)
        centers: list[float] = []
        for line in lines:
            line_boxes = [
                _bounds_of_box({"vertices": word["vertices"]}) for word in line
            ]
            line_boxes = [box for box in line_boxes if box is not None]
            if not line_boxes:
                continue
            top = min(box[1] for box in line_boxes)
            bottom = max(box[3] for box in line_boxes)
            centers.append((top + bottom) / 2)
            line_heights.append(bottom - top)
        if len(centers) >= 2:
            cell_lines.append(centers)
    if not cell_lines:
        return [(row_bounds[1], row_bounds[3])]
    typical_line_height = median([height for height in line_heights if height > 0] or [1.0])
    gap_threshold = max(typical_line_height * 1.6, typical_height * 0.65)
    split_centers: list[float] = []
    for centers in cell_lines:
        split_centers.extend(
            (centers[index] + centers[index + 1]) / 2
            for index in range(len(centers) - 1)
            if centers[index + 1] - centers[index] >= gap_threshold
        )
    if split_centers:
        clustered: list[list[float]] = []
        merge_distance = max(typical_line_height * 0.75, 12.0)
        for center in sorted(split_centers):
            if not clustered or center - clustered[-1][-1] > merge_distance:
                clustered.append([center])
            else:
                clustered[-1].append(center)
        split_centers = [median(cluster) for cluster in clustered]
    if not split_centers:
        return [(row_bounds[1], row_bounds[3])]
    boundaries = [row_bounds[1], *split_centers, row_bounds[3]]
    return [
        (boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
        if boundaries[index + 1] > boundaries[index]
    ]


def _project_words_text(words: tuple[dict, ...]) -> str:
    ordered: list[dict] = []
    for line in _cluster_lines(list(words)):
        ordered.extend(line)
    return " ".join(str(word.get("text", "")).strip() for word in ordered if str(word.get("text", "")).strip())


def _clip_cell_bbox(cell_bbox: dict, top: float, bottom: float) -> dict:
    bounds = _bounds_of_box(cell_bbox)
    if bounds is None:
        return {}
    clipped_top = max(bounds[1], top)
    clipped_bottom = min(bounds[3], bottom)
    if clipped_bottom <= clipped_top:
        return {}
    return _box_from_bounds((bounds[0], clipped_top, bounds[2], clipped_bottom))


def _segment_physical_rows(
    physical_table: PhysicalTable,
    header_rows: set[int],
    words: list[dict],
    typical_height: float,
    *,
    secondary_payload: dict | None = None,
    secondary_crop: dict | None = None,
    page_width: float = 0.0,
    page_height: float = 0.0,
    dropped_rows: set[int] | None = None,
) -> list[PhysicalSubrow]:
    result: list[PhysicalSubrow] = []
    for row in physical_table.rows:
        if row.source_row_index in header_rows or row.source_row_index in (dropped_rows or set()):
            continue
        row_bounds = _bounds_of_box(row.bbox)
        raw_multiline = any("\n" in cell.raw_text for cell in row.cells)
        secondary_bands = _secondary_row_bands(
            secondary_payload,
            secondary_crop,
            page_width,
            page_height,
            row.bbox,
        )
        bands = secondary_bands or _primary_word_bands(row, words, typical_height)
        if len(bands) <= 1:
            evidence: tuple[str, ...] = ()
            ambiguity = raw_multiline
            result.append(
                PhysicalSubrow(
                    source_table_index=physical_table.table_index,
                    source_row_index=row.source_row_index,
                    source_subrow_index=0,
                    bbox=dict(row.bbox),
                    cells=row.cells,
                    split_evidence=evidence,
                    structural_ambiguity=ambiguity,
                )
            )
            continue

        evidence_list = []
        if secondary_bands:
            evidence_list.append("secondary_table_rows")
        if not secondary_bands:
            evidence_list.append("primary_word_bands")
        evidence = tuple(evidence_list)
        if row_bounds is None:
            continue
        row_ambiguous = any(
            cell.raw_text.strip() and not cell.words for cell in row.cells
        )
        for subrow_index, (top, bottom) in enumerate(bands):
            projected_cells: list[PhysicalCell] = []
            for cell in row.cells:
                selected = tuple(
                    word
                    for word in cell.words
                    if (center := _word_center(word)) is not None
                    and top <= center[1] < bottom
                )
                projected_cells.append(
                    PhysicalCell(
                        source_cell_index=cell.source_cell_index,
                        source_row_index=cell.source_row_index,
                        column_index=cell.column_index,
                        row_span=cell.row_span,
                        column_span=cell.column_span,
                        raw_text=_project_words_text(selected),
                        bbox=_clip_cell_bbox(cell.bbox, top, bottom),
                        words=selected,
                    )
                )
            result.append(
                PhysicalSubrow(
                    source_table_index=physical_table.table_index,
                    source_row_index=row.source_row_index,
                    source_subrow_index=subrow_index,
                    bbox=_box_from_bounds((row_bounds[0], top, row_bounds[2], bottom)),
                    cells=tuple(projected_cells),
                    split_evidence=evidence,
                    structural_ambiguity=row_ambiguous,
                )
            )
    return result


def _table_cell_keys(
    mapping: dict[int, tuple[str, ...]], cell: DetectedCell
) -> tuple[str, ...]:
    return mapping.get(cell.column_index, ())


def _record_diagnostic(diagnostics: dict | None, **entry) -> None:
    if diagnostics is None:
        return
    diagnostics.setdefault("events", []).append(entry)


def rows_from_tables(
    tables: list,
    denominator_x: float,
    denominator_y: float,
    provider_key: str,
    *,
    words: list[dict] | None = None,
    secondary_payload: dict | None = None,
    secondary_crop: dict | None = None,
    diagnostics: dict | None = None,
) -> list[OcrRow] | None:
    """PRIMARY path: preserve physical rows, segment them, then map fields."""
    candidates = [
        _build_detected_table(table, provider_key, index)
        for index, table in enumerate(tables)
        if isinstance(table, dict)
    ]
    candidates = [table for table in candidates if table and table.rows]
    if not candidates:
        return None
    table = max(candidates, key=lambda item: sum(len(row.cells) for row in item.rows))
    for candidate in candidates:
        if candidate is not table:
            _record_diagnostic(
                diagnostics,
                kind="table_dropped",
                source_table_index=candidate.source_table_index,
                drop_reason="table_not_selected",
            )
    mapping, header_rows = _table_header_mapping(table)
    table.header_rows = header_rows
    if mapping is None:
        _record_diagnostic(
            diagnostics,
            kind="table_dropped",
            source_table_index=table.source_table_index,
            drop_reason="semantic_mapping_unavailable",
        )
        return None
    table.column_mapping = mapping

    data_rows = [row for row in table.rows if row.row_index not in header_rows]
    heights = [
        max((_bbox_height(cell.bbox) for cell in _logical_row_cells(table, row.row_index)), default=0.0)
        for row in data_rows
    ]
    typical_height = median([height for height in heights if height > 0] or [0.0])
    tail_rows = _tail_service_rows(
        data_rows,
        lambda row_index: _logical_row_cells(table, row_index),
        mapping,
        typical_height,
    )
    for row_index in sorted(tail_rows):
        _record_diagnostic(
            diagnostics,
            kind="row_dropped",
            source_table_index=table.source_table_index,
            source_row_index=row_index,
            drop_reason="trailing_service_block",
        )
    physical_table = _physical_table_from_detected(table, words or [])
    subrows = _segment_physical_rows(
        physical_table,
        header_rows,
        words or [],
        typical_height,
        secondary_payload=secondary_payload,
        secondary_crop=secondary_crop,
        page_width=denominator_x,
        page_height=denominator_y,
        dropped_rows=tail_rows,
    )

    rows: list[OcrRow] = []
    for source_row, subrow in enumerate(subrows, start=1):
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        raw_values: dict[str, str] = {}
        normalization: dict[str, dict[str, object]] = {}
        cell_bboxes: dict[str, dict] = {}
        vertices: list[tuple[float, float]] = []
        for cell in subrow.cells:
            # A body columnSpan is projected to its start column only. The
            # source span remains in provenance; one raw cell must not be
            # silently copied into several semantic fields.
            keys = mapping.get(cell.column_index, ())
            if not keys:
                continue
            raw_text = cell.raw_text
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
        review_reasons = list(table.review_reasons)
        if subrow.structural_ambiguity and "structural_ambiguity" not in review_reasons:
            review_reasons.append("structural_ambiguity")
        for details in normalization.values():
            if details.get("numeric_suspect") and "numeric_suspect" not in review_reasons:
                review_reasons.append("numeric_suspect")
        if not values and not subrow.structural_ambiguity:
            _record_diagnostic(
                diagnostics,
                kind="row_dropped",
                source_table_index=subrow.source_table_index,
                source_row_index=subrow.source_row_index,
                source_subrow_index=subrow.source_subrow_index,
                drop_reason="empty_after_semantic_projection",
            )
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
                    "structured_table": True,
                    "provider_has_explicit_rows": True,
                    "source_table_index": subrow.source_table_index,
                    "source_row_index": subrow.source_row_index,
                    "source_subrow_index": subrow.source_subrow_index,
                    "source_cell_refs": sorted({
                        cell.source_cell_index for cell in subrow.cells
                        if cell.source_cell_index >= 0
                    }),
                    "split_evidence": list(subrow.split_evidence),
                    "structural_ambiguity": subrow.structural_ambiguity,
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


def _is_numbering_word_line(line_words: list[dict]) -> bool:
    texts = [str(word.get("text", "")).strip() for word in line_words]
    texts = [text for text in texts if text]
    return len(texts) >= 3 and all(re.fullmatch(r"[1-9]", text) for text in texts)


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
        elif anchors and _is_numbering_word_line(line):
            header_line_count += 1
            continue
        elif anchors:
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


def reconstruct_page_rows(
    payload: dict,
    provider_key: str,
    *,
    secondary_payload: dict | None = None,
    secondary_crop: dict | None = None,
    diagnostics: dict | None = None,
) -> list[OcrRow]:
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
        rows = rows_from_tables(
            tables,
            width,
            height,
            provider_key,
            words=collect_words(text_annotation),
            secondary_payload=secondary_payload,
            secondary_crop=secondary_crop,
            diagnostics=diagnostics,
        )
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
        return _rows_from_geometry(
            geometric, page_view, provider_key, fallback_reasons
        )
    return _rows_from_plain_text(page_view, provider_key, fallback_reasons)
