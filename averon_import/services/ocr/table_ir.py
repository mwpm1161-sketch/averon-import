"""Immutable provider-neutral physical table intermediate representation.

This module deliberately contains physical evidence only.  Header, family and
schema decisions are represented by :class:`TableAnalysisContext` in the
semantic layer and never become part of ``PhysicalTableIR`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
        return (
            f"{self.table.document_id or 'document'}:p{self.table.page_number}:"
            f"t{self.table.table_index}:r{self.row_index}"
        )


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
        return f"{self.row.key}:c{self.column_index}:s{self.source_cell_index}"


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
        return f"{self.table.document_id or 'document'}:p{self.table.page_number}:w{self.source_word_index}"


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
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "cells", tuple(self.cells) or tuple(cell for row in self.rows for cell in row.cells))
        object.__setattr__(self, "words", tuple(self.words))
        normalized_assignments = {
            str(key): tuple(_word_ref(ref) for ref in value)
            for key, value in self.assigned_word_refs.items()
        }
        object.__setattr__(self, "assigned_word_refs", _freeze(normalized_assignments))
        object.__setattr__(self, "grid_evidence", _freeze(self.grid_evidence))
        object.__setattr__(self, "structural_evidence", _freeze(self.structural_evidence))
        object.__setattr__(self, "raster_witness", _freeze(self.raster_witness))
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "row_fragments", tuple(self.row_fragments))

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
