"""Provider-neutral physical grid contracts and spatial word assignment.

Coordinates in this module are normalized to the page (0..1).  A raster,
vector or future detector can therefore produce the same ``PhysicalGrid``
without coupling reconstruction to pixels or to an OCR provider response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
import math
from pathlib import Path
from typing import Protocol


Bounds = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class PhysicalGridCell:
    row_index: int
    column_index: int
    bounds: Bounds

    @property
    def center(self) -> tuple[float, float]:
        left, top, right, bottom = self.bounds
        return (left + right) / 2, (top + bottom) / 2

    def as_bbox(self) -> dict[str, float]:
        left, top, right, bottom = self.bounds
        return {
            "x": round(left, 6),
            "y": round(top, 6),
            "width": round(right - left, 6),
            "height": round(bottom - top, 6),
        }


@dataclass(frozen=True, slots=True)
class PhysicalGrid:
    source: str
    x_boundaries: tuple[float, ...]
    y_boundaries: tuple[float, ...]
    cells: tuple[PhysicalGridCell, ...]
    confidence: float
    high_confidence: bool
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return max(0, len(self.y_boundaries) - 1)

    @property
    def column_count(self) -> int:
        return max(0, len(self.x_boundaries) - 1)

    @property
    def bounds(self) -> Bounds:
        if len(self.x_boundaries) < 2 or len(self.y_boundaries) < 2:
            return 0.0, 0.0, 0.0, 0.0
        return (
            self.x_boundaries[0],
            self.y_boundaries[0],
            self.x_boundaries[-1],
            self.y_boundaries[-1],
        )

    def cell(self, row_index: int, column_index: int) -> PhysicalGridCell | None:
        if not (0 <= row_index < self.row_count):
            return None
        if not (0 <= column_index < self.column_count):
            return None
        offset = row_index * self.column_count + column_index
        if 0 <= offset < len(self.cells):
            candidate = self.cells[offset]
            if (
                candidate.row_index == row_index
                and candidate.column_index == column_index
            ):
                return candidate
        return next(
            (
                item
                for item in self.cells
                if item.row_index == row_index
                and item.column_index == column_index
            ),
            None,
        )

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "confidence": round(self.confidence, 4),
            "high_confidence": self.high_confidence,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "x_boundaries": [round(value, 6) for value in self.x_boundaries],
            "y_boundaries": [round(value, 6) for value in self.y_boundaries],
        }


@dataclass(frozen=True, slots=True)
class PhysicalGridDetection:
    grid: PhysicalGrid | None
    source: str
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    candidate_count: int = 0
    selected_candidate: int | None = None
    candidate_diagnostics: tuple[dict, ...] = ()

    @property
    def high_confidence(self) -> bool:
        return bool(self.grid and self.grid.high_confidence)

    def as_dict(self) -> dict:
        result = {
            "source": self.source,
            "high_confidence": self.high_confidence,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "candidate_count": self.candidate_count,
            "selected_candidate": self.selected_candidate,
            "candidate_diagnostics": [dict(item) for item in self.candidate_diagnostics],
        }
        if self.grid:
            result["grid"] = self.grid.as_dict()
        return result


class PhysicalGridDetector(Protocol):
    """Common detector boundary; vector detectors can implement this later."""

    def detect_page(self, pdf_path: Path, page_number: int) -> PhysicalGridDetection:
        ...


@dataclass(frozen=True, slots=True)
class SpatialWord:
    text: str
    bounds: Bounds
    source_index: int = -1

    @property
    def center(self) -> tuple[float, float]:
        left, top, right, bottom = self.bounds
        return (left + right) / 2, (top + bottom) / 2


@dataclass(frozen=True, slots=True)
class AmbiguousWord:
    """A word whose geometry is not safe to project into one physical cell."""

    word: SpatialWord
    candidates: tuple[tuple[float, PhysicalGridCell], ...]
    reason: str
    evidence: dict[str, float | bool] = field(default_factory=dict)


def _intersection_area(first: Bounds, second: Bounds) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    return max(0.0, right - left) * max(0.0, bottom - top)


def word_assignment_evidence(
    grid: PhysicalGrid,
    word: SpatialWord,
    target: PhysicalGridCell | None = None,
) -> dict[str, float | bool]:
    """Return spatial evidence while preserving the existing tuple API."""
    word_area = max(
        1e-12,
        (word.bounds[2] - word.bounds[0]) * (word.bounds[3] - word.bounds[1]),
    )
    overlaps = sorted(
        [
            (
                _intersection_area(word.bounds, cell.bounds) / word_area,
                cell,
            )
            for cell in grid.cells
            if _intersection_area(word.bounds, cell.bounds) > 0
        ],
        key=lambda item: item[0],
        reverse=True,
    )
    best = float(overlaps[0][0]) if overlaps else 0.0
    second = float(overlaps[1][0]) if len(overlaps) > 1 else 0.0
    center_x, center_y = word.center
    target = target or (overlaps[0][1] if overlaps else None)
    center_in_target = bool(
        target
        and target.bounds[0] <= center_x < target.bounds[2]
        and target.bounds[1] <= center_y < target.bounds[3]
    )
    boundary_distance = 1.0
    if target:
        boundary_distance = min(
            abs(center_x - target.bounds[0]),
            abs(target.bounds[2] - center_x),
            abs(center_y - target.bounds[1]),
            abs(target.bounds[3] - center_y),
        )
    return {
        "best_overlap": best,
        "second_overlap": second,
        "overlap_margin": best - second,
        "center_in_target": center_in_target,
        "boundary_distance": boundary_distance,
    }


def assign_words_to_cells(
    grid: PhysicalGrid,
    words: list[SpatialWord],
    *,
    minimum_overlap: float = 0.20,
) -> tuple[
    dict[tuple[int, int], list[SpatialWord]],
    list[AmbiguousWord],
    list[SpatialWord],
]:
    """Assign words conservatively, explicitly separating ambiguity.

    A small touch of a neighbouring border remains assigned to the cell that
    contains the word center.  A substantial overlap with a second cell, a
    tie, or a near-tie is returned as ``AmbiguousWord`` and is never silently
    projected into a semantic field.
    """
    assigned: dict[tuple[int, int], list[SpatialWord]] = {}
    ambiguous: list[AmbiguousWord] = []
    unassigned: list[SpatialWord] = []
    for word in words:
        center_x, center_y = word.center
        word_area = max(
            1e-12,
            (word.bounds[2] - word.bounds[0])
            * (word.bounds[3] - word.bounds[1]),
        )
        overlaps = sorted(
            (
                (
                    _intersection_area(word.bounds, cell.bounds) / word_area,
                    cell,
                )
                for cell in grid.cells
                if _intersection_area(word.bounds, cell.bounds) > 0
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not overlaps or overlaps[0][0] < minimum_overlap:
            unassigned.append(word)
            continue
        center_target = next(
            (
                cell
                for cell in grid.cells
                if cell.bounds[0] <= center_x < cell.bounds[2]
                and cell.bounds[1] <= center_y < cell.bounds[3]
            ),
            None,
        )
        top_ratio, target = overlaps[0]
        second_ratio = overlaps[1][0] if len(overlaps) > 1 else 0.0
        near_tie = (
            second_ratio >= minimum_overlap
            and (
                second_ratio >= top_ratio * 0.75
                or top_ratio - second_ratio <= 0.15
            )
        )
        if near_tie:
            ambiguous.append(
                AmbiguousWord(
                    word=word,
                    candidates=tuple(overlaps[:4]),
                    reason=(
                        "overlap_tie"
                        if abs(top_ratio - second_ratio) <= 1e-9
                        else "overlap_near_tie"
                    ),
                    evidence=word_assignment_evidence(grid, word, target),
                )
            )
            continue
        if center_target is None and len(overlaps) > 1 and top_ratio < 0.55:
            ambiguous.append(
                AmbiguousWord(
                    word=word,
                    candidates=tuple(overlaps[:4]),
                    reason="edge_overlap_without_center",
                    evidence=word_assignment_evidence(grid, word, target),
                )
            )
            continue
        assigned.setdefault((target.row_index, target.column_index), []).append(word)
    for values in assigned.values():
        values.sort(key=lambda item: (item.bounds[1], item.bounds[0]))
    return assigned, ambiguous, unassigned


def validate_physical_grid(grid: PhysicalGrid | None) -> tuple[str, ...]:
    """Validate an external detector result before semantic reconstruction."""
    if grid is None:
        return ("grid_missing",)
    reasons: list[str] = []
    x_boundaries = getattr(grid, "x_boundaries", None)
    y_boundaries = getattr(grid, "y_boundaries", None)
    cells = getattr(grid, "cells", None)
    if not isinstance(x_boundaries, (list, tuple)):
        reasons.append("invalid_x_boundaries_container")
        x_boundaries = ()
    if not isinstance(y_boundaries, (list, tuple)):
        reasons.append("invalid_y_boundaries_container")
        y_boundaries = ()
    if not isinstance(cells, (list, tuple)):
        reasons.append("invalid_cells_container")
        cells = ()
    if len(x_boundaries) < 2 or len(y_boundaries) < 2:
        reasons.append("grid_boundaries_missing")
    for axis_name, boundaries in (
        ("x", x_boundaries),
        ("y", y_boundaries),
    ):
        previous = -math.inf
        for value in boundaries:
            if isinstance(value, (str, bytes, bool)):
                reasons.append(f"invalid_{axis_name}_boundary")
                break
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                reasons.append(f"invalid_{axis_name}_boundary")
                break
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                reasons.append(f"invalid_{axis_name}_boundary")
                break
            if numeric <= previous:
                reasons.append(f"non_monotonic_{axis_name}_boundaries")
                break
            previous = numeric
    try:
        confidence = float(getattr(grid, "confidence", math.nan))
    except (TypeError, ValueError):
        confidence = math.nan
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        reasons.append("invalid_grid_confidence")
    if bool(getattr(grid, "high_confidence", False)) and getattr(grid, "reasons", ()):
        reasons.append("high_confidence_with_fatal_reasons")
    row_count = max(0, len(y_boundaries) - 1)
    column_count = max(0, len(x_boundaries) - 1)
    expected = row_count * column_count
    if len(cells) != expected:
        reasons.append("grid_cell_count_mismatch")
    seen: set[tuple[int, int]] = set()
    for cell in cells:
        if not isinstance(cell, PhysicalGridCell):
            reasons.append("invalid_grid_cell")
            continue
        if isinstance(cell.row_index, (str, bytes, bool)) or isinstance(
            cell.column_index, (str, bytes, bool)
        ):
            reasons.append("invalid_grid_cell_index")
            continue
        try:
            row_index = int(cell.row_index)
            column_index = int(cell.column_index)
        except (AttributeError, TypeError, ValueError):
            reasons.append("invalid_grid_cell_index")
            continue
        if isinstance(cell.row_index, float) and not cell.row_index.is_integer():
            reasons.append("invalid_grid_cell_index")
            continue
        if isinstance(cell.column_index, float) and not cell.column_index.is_integer():
            reasons.append("invalid_grid_cell_index")
            continue
        key = (row_index, column_index)
        if not (
            0 <= row_index < row_count
            and 0 <= column_index < column_count
        ):
            reasons.append("invalid_grid_cell_index")
            break
        if key in seen:
            reasons.append("duplicate_grid_cell")
            break
        seen.add(key)
        try:
            if any(isinstance(value, (str, bytes, bool)) for value in cell.bounds):
                raise TypeError("non-numeric bbox")
            left, top, right, bottom = (float(value) for value in cell.bounds)
        except (AttributeError, TypeError, ValueError):
            reasons.append("invalid_cell_bbox")
            continue
        if not (
            all(math.isfinite(value) for value in (left, top, right, bottom))
            and 0.0 <= left <= right <= 1.0
            and 0.0 <= top <= bottom <= 1.0
            and right > left
            and bottom > top
        ):
            reasons.append("invalid_cell_bbox")
            break
        try:
            expected_bounds = (
                float(x_boundaries[column_index]),
                float(y_boundaries[row_index]),
                float(x_boundaries[column_index + 1]),
                float(y_boundaries[row_index + 1]),
            )
        except (IndexError, TypeError, ValueError):
            reasons.append("invalid_grid_boundaries")
            continue
        if any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-6)
            for actual, expected in zip((left, top, right, bottom), expected_bounds)
        ):
            reasons.append("cell_bbox_not_aligned_to_boundaries")
            break
    if not reasons and seen != {
        (row, column)
        for row in range(row_count)
        for column in range(column_count)
    }:
        reasons.append("incomplete_grid_cell_index_set")
    return tuple(dict.fromkeys(reasons))


def spatial_cell_text(words: list[SpatialWord]) -> str:
    """Preserve visual line order inside one physical cell."""
    if not words:
        return ""
    ordered = sorted(words, key=lambda item: (item.bounds[1], item.bounds[0]))
    heights = [
        word.bounds[3] - word.bounds[1]
        for word in ordered
        if word.bounds[3] > word.bounds[1]
    ]
    tolerance = max(median(heights or [0.01]) * 0.65, 1e-6)
    lines: list[list[SpatialWord]] = []
    centers: list[float] = []
    for word in ordered:
        center_y = (word.bounds[1] + word.bounds[3]) / 2
        if not lines or abs(center_y - centers[-1]) > tolerance:
            lines.append([word])
            centers.append(center_y)
            continue
        lines[-1].append(word)
        centers[-1] = sum(
            (item.bounds[1] + item.bounds[3]) / 2 for item in lines[-1]
        ) / len(lines[-1])
    return "\n".join(
        " ".join(item.text for item in sorted(line, key=lambda item: item.bounds[0]))
        for line in lines
    )
