"""Immutable provider-neutral physical table intermediate representation.

This module deliberately contains physical evidence only.  Header, family and
schema decisions are represented by :class:`TableAnalysisContext` in the
semantic layer and never become part of ``PhysicalTableIR`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


BBox = tuple[float, float, float, float]


def _freeze(value: Any) -> Any:
    """Recursively copy mutable input into immutable value containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in sorted(value, key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset)):
        return [_thaw(item) for item in value]
    return value


def _bbox(value: Any) -> BBox:
    """Normalize common OCR/grid bbox shapes into immutable bounds."""

    if value is None:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(value, Mapping):
        if "vertices" in value:
            return _bbox(value.get("vertices"))
        if {"x", "y", "width", "height"}.issubset(value):
            left = float(value["x"])
            top = float(value["y"])
            return (left, top, left + float(value["width"]), top + float(value["height"]))
        if {"left", "top", "right", "bottom"}.issubset(value):
            return (
                float(value["left"]),
                float(value["top"]),
                float(value["right"]),
                float(value["bottom"]),
            )
    if isinstance(value, (list, tuple)):
        if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
            return tuple(float(item) for item in value)  # type: ignore[return-value]
        points = []
        for point in value:
            if isinstance(point, Mapping) and {"x", "y"}.issubset(point):
                points.append((float(point["x"]), float(point["y"])))
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
        if points:
            return (
                min(point[0] for point in points),
                min(point[1] for point in points),
                max(point[0] for point in points),
                max(point[1] for point in points),
            )
    raise ValueError(f"Unsupported bbox value: {value!r}")


def _bbox_dict(value: BBox) -> dict[str, float]:
    return {
        "x": round(value[0], 6),
        "y": round(value[1], 6),
        "width": round(max(0.0, value[2] - value[0]), 6),
        "height": round(max(0.0, value[3] - value[1]), 6),
    }


def _validate_bbox(value: BBox, label: str) -> None:
    if not all(math.isfinite(number) for number in value):
        raise ValueError(f"{label} must contain finite coordinates")
    left, top, right, bottom = value
    if left < 0 or top < 0 or right < left or bottom < top:
        raise ValueError(f"{label} must have a non-negative, ordered extent")


def _validate_boundaries(values: tuple[float, ...], label: str) -> None:
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain finite values")
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError(f"{label} must be strictly increasing")


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _table_ref(value: PhysicalTableRef | Mapping[str, Any]) -> PhysicalTableRef:
    if isinstance(value, PhysicalTableRef):
        return value
    return PhysicalTableRef(
        page_number=int(value["page_number"]),
        table_index=int(value["table_index"]),
        document_id=value.get("document_id"),
        detector_source=str(value.get("detector_source", "")),
    )


def _row_ref(value: PhysicalRowRef | Mapping[str, Any]) -> PhysicalRowRef:
    if isinstance(value, PhysicalRowRef):
        return value
    return PhysicalRowRef(
        table=_table_ref(value["table"]),
        row_index=int(value["row_index"]),
    )


def _cell_ref(value: PhysicalCellRef | Mapping[str, Any]) -> PhysicalCellRef:
    if isinstance(value, PhysicalCellRef):
        return value
    return PhysicalCellRef(
        row=_row_ref(value["row"]),
        column_index=int(value["column_index"]),
        source_cell_index=int(value.get("source_cell_index", -1)),
    )


def _word_ref(value: PhysicalWordRef | Mapping[str, Any]) -> PhysicalWordRef:
    if isinstance(value, PhysicalWordRef):
        return value
    return PhysicalWordRef(
        table=_table_ref(value["table"]),
        source_word_index=int(value["source_word_index"]),
    )


@dataclass(frozen=True, slots=True)
class PhysicalTableRef:
    page_number: int
    table_index: int
    document_id: str | None = None
    detector_source: str = ""

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be positive")
        if self.table_index < 0:
            raise ValueError("table_index must be non-negative")
        if self.document_id and ("/" in self.document_id or "\\" in self.document_id):
            raise ValueError("document_id must not contain a filesystem path")

    @property
    def key(self) -> str:
        """Canonical identity for this table.

        The JSON tuple is length-safe for arbitrary document/detector text and
        includes every equality field.  ``document_id=None`` is an explicit
        runtime-local scope: callers must not use it to correlate tables from
        separate document lifecycles without supplying a document id.
        """

        identity = [self.document_id, self.page_number, self.table_index, self.detector_source]
        return "table:" + json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "table_index": self.table_index,
            "document_id": self.document_id,
            "detector_source": self.detector_source,
        }


@dataclass(frozen=True, slots=True)
class PhysicalRowRef:
    table: PhysicalTableRef
    row_index: int

    def __post_init__(self) -> None:
        if self.row_index < 0:
            raise ValueError("row_index must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {"table": self.table.as_dict(), "row_index": self.row_index}

    @property
    def key(self) -> str:
        return f"{self.table.key}|row:{self.row_index}"


@dataclass(frozen=True, slots=True)
class PhysicalCellRef:
    row: PhysicalRowRef
    column_index: int
    source_cell_index: int = -1

    def __post_init__(self) -> None:
        if self.column_index < 0:
            raise ValueError("column_index must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row.as_dict(),
            "column_index": self.column_index,
            "source_cell_index": self.source_cell_index,
        }

    @property
    def key(self) -> str:
        return f"{self.row.key}|cell:{self.column_index}:{self.source_cell_index}"


@dataclass(frozen=True, slots=True)
class PhysicalWordRef:
    table: PhysicalTableRef
    source_word_index: int

    def __post_init__(self) -> None:
        if self.source_word_index < 0:
            raise ValueError("source_word_index must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table.as_dict(),
            "source_word_index": self.source_word_index,
        }

    @property
    def key(self) -> str:
        return f"{self.table.key}|word:{self.source_word_index}"


@dataclass(frozen=True, slots=True)
class PhysicalWordIR:
    ref: PhysicalWordRef
    text: str
    bbox: BBox
    provider_word_ref: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.as_dict(),
            "text": self.text,
            "bbox": list(self.bbox),
            "provider_word_ref": self.provider_word_ref,
            "provenance": _thaw(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PhysicalWordIR:
        return cls(
            ref=_word_ref(value["ref"]),
            text=str(value.get("text", "")),
            bbox=value.get("bbox"),
            provider_word_ref=value.get("provider_word_ref"),
            provenance=value.get("provenance", {}),
        )


@dataclass(frozen=True, slots=True)
class PhysicalCellIR:
    ref: PhysicalCellRef
    bbox: BBox
    raw_text: str = ""
    row_span: int = 1
    column_span: int = 1
    word_refs: tuple[PhysicalWordRef, ...] = ()
    provider_cell_refs: tuple[str, ...] = ()
    raster_evidence: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.row_span < 1 or self.column_span < 1:
            raise ValueError("cell spans must be positive")
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "word_refs", tuple(_word_ref(ref) for ref in self.word_refs))
        object.__setattr__(self, "provider_cell_refs", _strings(self.provider_cell_refs))
        object.__setattr__(self, "raster_evidence", _freeze(self.raster_evidence))
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.as_dict(),
            "bbox": list(self.bbox),
            "raw_text": self.raw_text,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "word_refs": [ref.as_dict() for ref in self.word_refs],
            "provider_cell_refs": list(self.provider_cell_refs),
            "raster_evidence": _thaw(self.raster_evidence),
            "provenance": _thaw(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PhysicalCellIR:
        return cls(
            ref=_cell_ref(value["ref"]),
            bbox=value.get("bbox"),
            raw_text=str(value.get("raw_text", "")),
            row_span=int(value.get("row_span", 1)),
            column_span=int(value.get("column_span", 1)),
            word_refs=tuple(value.get("word_refs", ())),
            provider_cell_refs=tuple(value.get("provider_cell_refs", ())),
            raster_evidence=value.get("raster_evidence", {}),
            provenance=value.get("provenance", {}),
        )


@dataclass(frozen=True, slots=True)
class PhysicalRowIR:
    ref: PhysicalRowRef
    bbox: BBox
    cells: tuple[PhysicalCellIR, ...] = ()
    raster_nonempty: bool = False
    raster_metrics: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "raster_metrics", _freeze(self.raster_metrics))
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    @property
    def nonempty(self) -> bool:
        return bool(
            self.raster_nonempty
            or any(cell.raw_text.strip() or cell.word_refs for cell in self.cells)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.as_dict(),
            "bbox": list(self.bbox),
            "cells": [cell.as_dict() for cell in self.cells],
            "raster_nonempty": self.raster_nonempty,
            "raster_metrics": _thaw(self.raster_metrics),
            "provenance": _thaw(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PhysicalRowIR:
        return cls(
            ref=_row_ref(value["ref"]),
            bbox=value.get("bbox"),
            cells=tuple(PhysicalCellIR.from_dict(cell) for cell in value.get("cells", ())),
            raster_nonempty=bool(value.get("raster_nonempty", False)),
            raster_metrics=value.get("raster_metrics", {}),
            provenance=value.get("provenance", {}),
        )


@dataclass(frozen=True, slots=True)
class PhysicalRowFragmentIR:
    """A geometry fragment that retains its parent physical row identity."""

    parent_row_ref: PhysicalRowRef
    fragment_index: int
    bbox: BBox
    cells: tuple[PhysicalCellIR, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fragment_index < 0:
            raise ValueError("fragment_index must be non-negative")
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "provenance", _freeze(self.provenance))

    @property
    def row_ref(self) -> PhysicalRowRef:
        return self.parent_row_ref

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent_row_ref": self.parent_row_ref.as_dict(),
            "fragment_index": self.fragment_index,
            "bbox": list(self.bbox),
            "cells": [cell.as_dict() for cell in self.cells],
            "provenance": _thaw(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PhysicalRowFragmentIR:
        return cls(
            parent_row_ref=_row_ref(value["parent_row_ref"]),
            fragment_index=int(value["fragment_index"]),
            bbox=value.get("bbox"),
            cells=tuple(PhysicalCellIR.from_dict(cell) for cell in value.get("cells", ())),
            provenance=value.get("provenance", {}),
        )


@dataclass(frozen=True, slots=True)
class PhysicalTableIR:
    """Immutable physical truth shared by all future semantic providers."""

    ref: PhysicalTableRef
    bounds: BBox
    x_boundaries: tuple[float, ...] = ()
    y_boundaries: tuple[float, ...] = ()
    rows: tuple[PhysicalRowIR, ...] = ()
    cells: tuple[PhysicalCellIR, ...] = ()
    words: tuple[PhysicalWordIR, ...] = ()
    assigned_word_refs: Mapping[str, tuple[PhysicalWordRef, ...]] = field(default_factory=dict)
    grid_source: str = ""
    grid_evidence: Mapping[str, Any] = field(default_factory=dict)
    structural_evidence: Mapping[str, Any] = field(default_factory=dict)
    raster_witness: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    row_fragments: tuple[PhysicalRowFragmentIR, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", _bbox(self.bounds))
        object.__setattr__(self, "x_boundaries", tuple(float(value) for value in self.x_boundaries))
        object.__setattr__(self, "y_boundaries", tuple(float(value) for value in self.y_boundaries))
        rows = tuple(self.rows)
        explicit_cells = tuple(self.cells)
        words = tuple(self.words)
        fragments = tuple(self.row_fragments)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "cells", explicit_cells or tuple(cell for row in rows for cell in row.cells))
        object.__setattr__(self, "words", words)
        normalized_assignments = {
            str(key): tuple(_word_ref(ref) for ref in value)
            for key, value in self.assigned_word_refs.items()
        }
        object.__setattr__(self, "assigned_word_refs", _freeze(normalized_assignments))
        object.__setattr__(self, "grid_evidence", _freeze(self.grid_evidence))
        object.__setattr__(self, "structural_evidence", _freeze(self.structural_evidence))
        object.__setattr__(self, "raster_witness", _freeze(self.raster_witness))
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "row_fragments", fragments)

        _validate_bbox(self.bounds, "table bounds")
        _validate_boundaries(self.x_boundaries, "x boundaries")
        _validate_boundaries(self.y_boundaries, "y boundaries")

        row_by_key: dict[str, PhysicalRowIR] = {}
        row_indexes: set[int] = set()
        for row in rows:
            if not isinstance(row, PhysicalRowIR):
                raise TypeError("rows must contain PhysicalRowIR values")
            if row.ref.table != self.ref:
                raise ValueError("row reference belongs to a different physical table")
            if row.ref.key in row_by_key or row.ref.row_index in row_indexes:
                raise ValueError("duplicate physical row identity")
            _validate_bbox(row.bbox, f"row {row.ref.row_index} bounds")
            row_by_key[row.ref.key] = row
            row_indexes.add(row.ref.row_index)

        row_cells: dict[str, PhysicalCellIR] = {}
        row_columns: set[tuple[str, int]] = set()
        for row in rows:
            for cell in row.cells:
                if not isinstance(cell, PhysicalCellIR):
                    raise TypeError("row cells must contain PhysicalCellIR values")
                if cell.ref.row != row.ref or cell.ref.row.table != self.ref:
                    raise ValueError("cell reference belongs to an unrelated row/table")
                if cell.ref.key in row_cells or (row.ref.key, cell.ref.column_index) in row_columns:
                    raise ValueError("duplicate or conflicting physical cell identity")
                _validate_bbox(cell.bbox, f"cell {cell.ref.key} bounds")
                row_cells[cell.ref.key] = cell
                row_columns.add((row.ref.key, cell.ref.column_index))

        explicit_cell_keys = [cell.ref.key for cell in explicit_cells]
        if len(set(explicit_cell_keys)) != len(explicit_cell_keys):
            raise ValueError("duplicate physical cell identity in flat cells")
        if explicit_cells and set(explicit_cell_keys) != set(row_cells):
            raise ValueError("flat cells disagree with row-owned cells")
        for cell in self.cells:
            if cell.ref.key not in row_cells:
                raise ValueError("flat cell is not owned by a physical row")

        word_by_key: dict[str, PhysicalWordIR] = {}
        word_indexes: set[int] = set()
        for word in words:
            if not isinstance(word, PhysicalWordIR):
                raise TypeError("words must contain PhysicalWordIR values")
            if word.ref.table != self.ref:
                raise ValueError("word reference belongs to a different physical table")
            if word.ref.key in word_by_key or word.ref.source_word_index in word_indexes:
                raise ValueError("duplicate physical word identity")
            _validate_bbox(word.bbox, f"word {word.ref.key} bounds")
            word_by_key[word.ref.key] = word
            word_indexes.add(word.ref.source_word_index)

        for cell in row_cells.values():
            for word_ref in cell.word_refs:
                if word_ref.table != self.ref or word_ref.key not in word_by_key:
                    raise ValueError("cell word reference is not a known word in this table")

        for assignment in self.assigned_word_refs.values():
            for word_ref in assignment:
                if word_ref.table != self.ref or word_ref.key not in word_by_key:
                    raise ValueError("assigned word reference is not a known word in this table")

        fragment_keys: set[tuple[str, int]] = set()
        fragment_cells: set[str] = set()
        for fragment in fragments:
            if not isinstance(fragment, PhysicalRowFragmentIR):
                raise TypeError("row_fragments must contain PhysicalRowFragmentIR values")
            if fragment.parent_row_ref.table != self.ref:
                raise ValueError("row fragment parent belongs to a different table")
            fragment_identity = (fragment.parent_row_ref.key, fragment.fragment_index)
            if fragment_identity in fragment_keys:
                raise ValueError("duplicate row fragment identity")
            _validate_bbox(fragment.bbox, f"fragment {fragment_identity} bounds")
            fragment_keys.add(fragment_identity)
            for cell in fragment.cells:
                if cell.ref.row != fragment.parent_row_ref or cell.ref.row.table != self.ref:
                    raise ValueError("row fragment cell claims an unrelated row")
                if cell.ref.key in fragment_cells:
                    raise ValueError("duplicate row fragment cell identity")
                fragment_cells.add(cell.ref.key)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max(0, len(self.x_boundaries) - 1)

    def row(self, row_index: int) -> PhysicalRowIR | None:
        return next((row for row in self.rows if row.ref.row_index == row_index), None)

    @property
    def fragments(self) -> tuple[PhysicalRowFragmentIR, ...]:
        return self.row_fragments

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.as_dict(),
            "bounds": list(self.bounds),
            "x_boundaries": list(self.x_boundaries),
            "y_boundaries": list(self.y_boundaries),
            "rows": [row.as_dict() for row in self.rows],
            "cells": [cell.as_dict() for cell in self.cells],
            "words": [word.as_dict() for word in self.words],
            "assigned_word_refs": {
                key: [ref.as_dict() for ref in refs]
                for key, refs in self.assigned_word_refs.items()
            },
            "grid_source": self.grid_source,
            "grid_evidence": _thaw(self.grid_evidence),
            "structural_evidence": _thaw(self.structural_evidence),
            "raster_witness": _thaw(self.raster_witness),
            "provenance": _thaw(self.provenance),
            "row_fragments": [fragment.as_dict() for fragment in self.row_fragments],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PhysicalTableIR:
        return cls(
            ref=_table_ref(value["ref"]),
            bounds=value.get("bounds"),
            x_boundaries=tuple(value.get("x_boundaries", ())),
            y_boundaries=tuple(value.get("y_boundaries", ())),
            rows=tuple(PhysicalRowIR.from_dict(row) for row in value.get("rows", ())),
            cells=tuple(PhysicalCellIR.from_dict(cell) for cell in value.get("cells", ())),
            words=tuple(PhysicalWordIR.from_dict(word) for word in value.get("words", ())),
            assigned_word_refs=value.get("assigned_word_refs", {}),
            grid_source=str(value.get("grid_source", "")),
            grid_evidence=value.get("grid_evidence", {}),
            structural_evidence=value.get("structural_evidence", {}),
            raster_witness=value.get("raster_witness", {}),
            provenance=value.get("provenance", {}),
            row_fragments=tuple(
                PhysicalRowFragmentIR.from_dict(fragment)
                for fragment in value.get("row_fragments", ())
            ),
        )


def bbox_as_dict(bbox: BBox) -> dict[str, float]:
    """Public helper for compatibility adapters."""

    return _bbox_dict(_bbox(bbox))


def freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Expose the same defensive freeze used by the IR constructors."""

    return _freeze(value or {})


def thaw_value(value: Any) -> Any:
    """Return a JSON-friendly defensive copy of a frozen IR value."""

    return _thaw(value)


__all__ = [
    "BBox",
    "PhysicalCellIR",
    "PhysicalCellRef",
    "PhysicalRowFragmentIR",
    "PhysicalRowIR",
    "PhysicalRowRef",
    "PhysicalTableIR",
    "PhysicalTableRef",
    "PhysicalWordIR",
    "PhysicalWordRef",
    "bbox_as_dict",
    "freeze_mapping",
    "thaw_value",
]
