"""Provider-neutral multi-region physical page scene.

This module is shadow-only.  It preserves every physically proposed ruled
region on a page and never turns OCR text into geometry.  Semantic schema
decisions remain downstream and are evaluated per associated region.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from bisect import bisect_left, bisect_right
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import fitz

from averon_import.services.ocr.physical_grid import (
    Bounds,
    PhysicalGrid,
    PhysicalGridCell,
    PhysicalGridDetection,
)
from averon_import.services.ocr.raster_grid import (
    RasterGridPage,
    RasterRuledTableGridDetector,
)
from averon_import.services.ocr.semantics import (
    BoundedFamilyContext,
    ContextRegionEvidence,
    DEFAULT_SCHEMA_GATE,
    DEFAULT_SCHEMA_PROFILE_MATCHER,
    DEFAULT_TABLE_FAMILY_CLASSIFIER,
    HeaderSourceCell,
    ObservedSchema,
    map_semantic_header,
)
from averon_import.services.ocr.semantics.context_evidence import bounded_context_from_words


TRUSTED = "TRUSTED"
REVIEW = "REVIEW"
REJECTED = "REJECTED"
RASTER = "raster"
VECTOR = "vector"
PROVIDER = "provider"


def _bounds(value: Iterable[float]) -> Bounds:
    values = tuple(float(item) for item in value)
    if len(values) != 4 or not all(math.isfinite(item) for item in values):
        raise ValueError("region bounds must contain four finite coordinates")
    left, top, right, bottom = values
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError("region bounds must be ordered and non-empty")
    return left, top, right, bottom


def _area(value: Bounds) -> float:
    return max(0.0, value[2] - value[0]) * max(0.0, value[3] - value[1])


def _intersection(first: Bounds, second: Bounds) -> Bounds | None:
    value = (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )
    return value if value[2] > value[0] and value[3] > value[1] else None


def _iou(first: Bounds, second: Bounds) -> float:
    overlap = _intersection(first, second)
    if overlap is None:
        return 0.0
    union = _area(first) + _area(second) - _area(overlap)
    return _area(overlap) / max(1e-12, union)


def _containment(first: Bounds, second: Bounds) -> float:
    overlap = _intersection(first, second)
    if overlap is None:
        return 0.0
    return _area(overlap) / max(1e-12, min(_area(first), _area(second)))


def _boundary_distance(first: Bounds, second: Bounds) -> float:
    return max(
        abs(first[0] - second[0]),
        abs(first[1] - second[1]),
        abs(first[2] - second[2]),
        abs(first[3] - second[3]),
    )


def _union(first: Bounds, second: Bounds) -> Bounds:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


@dataclass(frozen=True, slots=True)
class LocalGridHypothesis:
    source: str
    bounds: Bounds
    grid: PhysicalGrid | None = None
    confidence: float = 0.0
    status: str = REVIEW
    evidence: Mapping[str, Any] = field(default_factory=dict)
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bounds", _bounds(self.bounds))
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "evidence", _freeze(self.evidence))
        object.__setattr__(self, "rejection_reasons", tuple(str(item) for item in self.rejection_reasons))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "bounds": list(self.bounds),
            "confidence": round(self.confidence, 4),
            "status": self.status,
            "grid": self.grid.as_dict() if self.grid is not None else None,
            "evidence": dict(self.evidence),
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class VectorLineSegment:
    orientation: str
    start: tuple[float, float]
    end: tuple[float, float]
    source_ref: str = ""

    @property
    def bounds(self) -> Bounds:
        return (
            min(self.start[0], self.end[0]),
            min(self.start[1], self.end[1]),
            max(self.start[0], self.end[0]),
            max(self.start[1], self.end[1]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "orientation": self.orientation,
            "start": list(self.start),
            "end": list(self.end),
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class TableRegionCandidate:
    ref: str
    page_bounds: Bounds
    proposal_sources: tuple[str, ...] = ()
    raster_evidence: Mapping[str, Any] | None = None
    vector_evidence: Mapping[str, Any] | None = None
    provider_proposal_evidence: Mapping[str, Any] | None = None
    local_grid_hypotheses: tuple[LocalGridHypothesis, ...] = ()
    physical_status: str = REVIEW
    rejection_reasons: tuple[str, ...] = ()
    provenance: tuple[Mapping[str, Any], ...] = ()
    provider_refs: tuple[str, ...] = ()
    provider_association: str = "none"
    schema_status: str | None = None
    profile_status: str | None = None
    schema_evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_bounds", _bounds(self.page_bounds))
        object.__setattr__(self, "proposal_sources", tuple(sorted(set(self.proposal_sources))))
        object.__setattr__(self, "local_grid_hypotheses", tuple(self.local_grid_hypotheses))
        object.__setattr__(self, "rejection_reasons", tuple(dict.fromkeys(str(item) for item in self.rejection_reasons)))
        object.__setattr__(self, "provenance", tuple(_freeze(item) for item in self.provenance))
        object.__setattr__(self, "provider_refs", tuple(sorted(set(self.provider_refs))))
        if self.raster_evidence is not None:
            object.__setattr__(self, "raster_evidence", _freeze(self.raster_evidence))
        if self.vector_evidence is not None:
            object.__setattr__(self, "vector_evidence", _freeze(self.vector_evidence))
        if self.provider_proposal_evidence is not None:
            object.__setattr__(self, "provider_proposal_evidence", _freeze(self.provider_proposal_evidence))
        if self.schema_evidence is not None:
            object.__setattr__(self, "schema_evidence", _freeze(self.schema_evidence))

    @property
    def trusted_grid(self) -> PhysicalGrid | None:
        for hypothesis in self.local_grid_hypotheses:
            if hypothesis.status == TRUSTED and hypothesis.grid is not None:
                return hypothesis.grid
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "page_bounds": list(self.page_bounds),
            "proposal_sources": list(self.proposal_sources),
            "raster_evidence": dict(self.raster_evidence) if self.raster_evidence is not None else None,
            "vector_evidence": dict(self.vector_evidence) if self.vector_evidence is not None else None,
            "provider_proposal_evidence": dict(self.provider_proposal_evidence) if self.provider_proposal_evidence is not None else None,
            "local_grid_hypotheses": [item.as_dict() for item in self.local_grid_hypotheses],
            "physical_status": self.physical_status,
            "rejection_reasons": list(self.rejection_reasons),
            "provenance": [dict(item) for item in self.provenance],
            "provider_refs": list(self.provider_refs),
            "provider_association": self.provider_association,
            "schema_status": self.schema_status,
            "profile_status": self.profile_status,
            "schema_evidence": dict(self.schema_evidence) if self.schema_evidence is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PageSceneIR:
    page_ref: str
    region_candidates: tuple[TableRegionCandidate, ...] = ()
    physical_sources: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "region_candidates", tuple(self.region_candidates))
        object.__setattr__(self, "physical_sources", tuple(sorted(set(self.physical_sources))))
        object.__setattr__(self, "diagnostics", _freeze(self.diagnostics))

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_ref": self.page_ref,
            "region_candidates": [item.as_dict() for item in self.region_candidates],
            "physical_sources": list(self.physical_sources),
            "diagnostics": dict(self.diagnostics),
        }


def _bounds_from_detection(
    detection: PhysicalGridDetection,
    width: int,
    height: int,
) -> Bounds | None:
    if detection.grid is not None:
        return detection.grid.bounds
    metrics = detection.metrics
    try:
        x = float(metrics["region_x_px"]) / width
        y = float(metrics["region_y_px"]) / height
        right = (float(metrics["region_x_px"]) + float(metrics["region_width_px"])) / width
        bottom = (float(metrics["region_y_px"]) + float(metrics["region_height_px"])) / height
        return _bounds((x, y, right, bottom))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


class RasterRegionProposalSource:
    """Expose all disconnected raster ruled-line regions, not just the best."""

    source = RASTER

    def __init__(self, detector: RasterRuledTableGridDetector | None = None) -> None:
        self.detector = detector or RasterRuledTableGridDetector()

    def propose(self, raster: RasterGridPage | Any) -> tuple[TableRegionCandidate, ...]:
        grayscale = raster.grayscale if isinstance(raster, RasterGridPage) else raster
        detect_regions = getattr(self.detector, "detect_region_detections", None)
        if callable(detect_regions):
            detections = detect_regions(grayscale)
        elif isinstance(raster, RasterGridPage):
            # Preserve compatibility with injected legacy detectors. The
            # legacy detector exposes only its selected hypothesis, so retain
            # it as one bounded proposal rather than inventing regions.
            detections = (raster.detection,)
        else:
            raise AttributeError("raster detector does not expose region detections")
        height, width = grayscale.shape
        result: list[TableRegionCandidate] = []
        for index, detection in enumerate(detections):
            bounds = _bounds_from_detection(detection, width, height)
            if bounds is None:
                continue
            grid = detection.grid
            status = TRUSTED if grid is not None and grid.high_confidence else REVIEW if grid is not None else REJECTED
            hypothesis = LocalGridHypothesis(
                source=RASTER,
                bounds=bounds,
                grid=grid,
                confidence=grid.confidence if grid is not None else 0.0,
                status=status,
                evidence=detection.metrics,
                rejection_reasons=detection.reasons,
            )
            result.append(TableRegionCandidate(
                ref=f"raster:{index}",
                page_bounds=bounds,
                proposal_sources=(RASTER,),
                raster_evidence={
                    "detection": detection.as_dict(),
                    "candidate_index": index,
                },
                local_grid_hypotheses=(hypothesis,),
                physical_status=status,
                rejection_reasons=detection.reasons,
                provenance=({"source": RASTER, "candidate_index": index},),
            ))
        return tuple(result)


def _point(value: Any) -> tuple[float, float] | None:
    try:
        return float(value.x), float(value.y)
    except AttributeError:
        try:
            return float(value[0]), float(value[1])
        except (IndexError, TypeError, ValueError):
            return None


class VectorTableRegionDetector:
    """Find rectangular physical components in a PDF line-segment graph."""

    source = VECTOR

    def __init__(self, *, axis_tolerance: float = 1.2, join_tolerance: float = 2.5) -> None:
        self.axis_tolerance = float(axis_tolerance)
        self.join_tolerance = float(join_tolerance)

    def extract_segments(self, page: Any) -> tuple[VectorLineSegment, ...]:
        segments: list[VectorLineSegment] = []
        for drawing_index, drawing in enumerate(page.get_drawings() or []):
            for item_index, item in enumerate(drawing.get("items") or ()):
                if not item:
                    continue
                operator = item[0]
                pairs: list[tuple[Any, Any]] = []
                if operator == "l" and len(item) >= 3:
                    pairs.append((item[1], item[2]))
                elif operator == "re" and len(item) >= 2:
                    rect = item[1]
                    pairs.extend(((rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)))
                for pair_index, (first_raw, second_raw) in enumerate(pairs):
                    first = _point(first_raw)
                    second = _point(second_raw)
                    if first is None or second is None:
                        continue
                    dx = abs(second[0] - first[0])
                    dy = abs(second[1] - first[1])
                    if dx <= self.axis_tolerance and dy <= self.axis_tolerance:
                        continue
                    if dy <= self.axis_tolerance:
                        orientation = "horizontal"
                    elif dx <= self.axis_tolerance:
                        orientation = "vertical"
                    else:
                        continue
                    segments.append(VectorLineSegment(
                        orientation=orientation,
                        start=first,
                        end=second,
                        source_ref=f"drawing:{drawing_index}:item:{item_index}:part:{pair_index}",
                    ))
        return tuple(segments)

    @staticmethod
    def _expanded_intersects(first: Bounds, second: Bounds, tolerance: float) -> bool:
        return not (
            first[2] + tolerance < second[0]
            or second[2] + tolerance < first[0]
            or first[3] + tolerance < second[1]
            or second[3] + tolerance < first[1]
        )

    def _components(self, segments: tuple[VectorLineSegment, ...]) -> tuple[tuple[VectorLineSegment, ...], ...]:
        parents = list(range(len(segments)))

        def root(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def join(first: int, second: int) -> None:
            left, right = root(first), root(second)
            if left != right:
                parents[right] = left

        # Same-orientation fragments are grouped by their supporting axis and
        # joined only when their intervals touch.  This avoids a page-wide
        # O(n^2) bbox comparison for drawing sheets with many line segments.
        for orientation, coordinate_index, start_index, end_index in (
            ("horizontal", 1, 0, 2),
            ("vertical", 0, 1, 3),
        ):
            oriented = sorted(
                (
                    index,
                    segment,
                    (segment.start[coordinate_index] + segment.end[coordinate_index]) / 2,
                )
                for index, segment in enumerate(segments)
                if segment.orientation == orientation
            )
            bands: list[list[tuple[int, VectorLineSegment, float]]] = []
            for item in oriented:
                if not bands or item[2] - sum(value[2] for value in bands[-1]) / len(bands[-1]) > self.join_tolerance:
                    bands.append([item])
                else:
                    bands[-1].append(item)
            for band in bands:
                ordered = sorted(band, key=lambda item: item[1].bounds[start_index])
                active_index: int | None = None
                active_end = -math.inf
                for index, segment, _coordinate in ordered:
                    start = segment.bounds[start_index]
                    end = segment.bounds[end_index]
                    if active_index is not None and start <= active_end + self.join_tolerance:
                        join(active_index, index)
                    if active_index is None or end > active_end:
                        active_index = index
                        active_end = end

        # Perpendicular segments are joined through actual line intersections
        # using a sorted x index, rather than comparing every pair.
        vertical = sorted(
            (segment.bounds[0], index, segment)
            for index, segment in enumerate(segments)
            if segment.orientation == "vertical"
        )
        vertical_x = [item[0] for item in vertical]
        for horizontal_index, horizontal in (
            (index, segment)
            for index, segment in enumerate(segments)
            if segment.orientation == "horizontal"
        ):
            left, _top, right, _bottom = horizontal.bounds
            y = (horizontal.start[1] + horizontal.end[1]) / 2
            for _x, vertical_index, vertical_segment in vertical[
                bisect_left(vertical_x, left - self.join_tolerance):
                bisect_right(vertical_x, right + self.join_tolerance)
            ]:
                vtop, vbottom = vertical_segment.bounds[1], vertical_segment.bounds[3]
                if vtop - self.join_tolerance <= y <= vbottom + self.join_tolerance:
                    join(horizontal_index, vertical_index)
        groups: dict[int, list[VectorLineSegment]] = {}
        for index, segment in enumerate(segments):
            groups.setdefault(root(index), []).append(segment)
        return tuple(
            tuple(sorted(group, key=lambda item: item.source_ref))
            for group in sorted(groups.values(), key=lambda item: min(segment.bounds[1] for segment in item))
        )

    @staticmethod
    def _cluster(values: Iterable[float], tolerance: float) -> tuple[float, ...]:
        groups: list[list[float]] = []
        for value in sorted(values):
            if not groups or value - groups[-1][-1] > tolerance:
                groups.append([value])
            else:
                groups[-1].append(value)
        return tuple(sum(group) / len(group) for group in groups)

    @staticmethod
    def _horizontal_support(segments: tuple[VectorLineSegment, ...], y: float, left: float, right: float, tolerance: float) -> float:
        span = max(1e-9, right - left)
        coverage = 0.0
        for segment in segments:
            if segment.orientation != "horizontal" or abs(segment.start[1] - y) > tolerance:
                continue
            coverage += max(0.0, min(right, max(segment.start[0], segment.end[0])) - max(left, min(segment.start[0], segment.end[0])))
        return min(1.0, coverage / span)

    @staticmethod
    def _vertical_support(segments: tuple[VectorLineSegment, ...], x: float, top: float, bottom: float, tolerance: float) -> float:
        span = max(1e-9, bottom - top)
        coverage = 0.0
        for segment in segments:
            if segment.orientation != "vertical" or abs(segment.start[0] - x) > tolerance:
                continue
            coverage += max(0.0, min(bottom, max(segment.start[1], segment.end[1])) - max(top, min(segment.start[1], segment.end[1])))
        return min(1.0, coverage / span)

    def _component_candidate(
        self,
        component: tuple[VectorLineSegment, ...],
        component_index: int,
        page_width: float,
        page_height: float,
    ) -> TableRegionCandidate | None:
        horizontals = tuple(item for item in component if item.orientation == "horizontal")
        verticals = tuple(item for item in component if item.orientation == "vertical")
        if not horizontals or not verticals:
            return None
        left = min(item.bounds[0] for item in component)
        top = min(item.bounds[1] for item in component)
        right = max(item.bounds[2] for item in component)
        bottom = max(item.bounds[3] for item in component)
        bounds = _bounds((left / page_width, top / page_height, right / page_width, bottom / page_height))
        tolerance = max(self.axis_tolerance, min(page_width, page_height) * 0.0015)
        xs = self._cluster((item.start[0] for item in verticals), tolerance)
        ys = self._cluster((item.start[1] for item in horizontals), tolerance)
        reasons: list[str] = []
        if len(xs) < 3:
            reasons.append("too_few_vertical_rules")
        if len(ys) < 3:
            reasons.append("too_few_horizontal_rules")
        intersections = 0
        possible_intersections = max(1, len(xs) * len(ys))
        # Engineering PDFs may contain thousands of drawing fragments. A
        # page-wide connected line graph is not a local table hypothesis and
        # must not trigger quadratic cell probing. Retain the component and
        # its axes as review diagnostics, but bound the detector work.
        complexity_limited = len(component) > 2000 or possible_intersections > 5000
        if complexity_limited:
            reasons.append("line_graph_complexity_requires_local_partition")
        else:
            for x in xs:
                for y in ys:
                    if any(
                        item.orientation == "vertical"
                        and min(item.start[1], item.end[1]) - tolerance <= y <= max(item.start[1], item.end[1]) + tolerance
                        and abs(item.start[0] - x) <= tolerance
                        for item in verticals
                    ) and any(
                        item.orientation == "horizontal"
                        and min(item.start[0], item.end[0]) - tolerance <= x <= max(item.start[0], item.end[0]) + tolerance
                        and abs(item.start[1] - y) <= tolerance
                        for item in horizontals
                    ):
                        intersections += 1
        intersection_ratio = intersections / possible_intersections
        if complexity_limited:
            horizontal_support = []
            vertical_support = []
        else:
            horizontal_support = [self._horizontal_support(component, y, left, right, tolerance) for y in ys]
            vertical_support = [self._vertical_support(component, x, top, bottom, tolerance) for x in xs]
        outer_support = min(
            horizontal_support[0] if horizontal_support else 0.0,
            horizontal_support[-1] if horizontal_support else 0.0,
            vertical_support[0] if vertical_support else 0.0,
            vertical_support[-1] if vertical_support else 0.0,
        )
        closed_cells = 0
        possible_cells = max(0, (len(xs) - 1) * (len(ys) - 1))
        if not complexity_limited:
            for row in range(len(ys) - 1):
                for column in range(len(xs) - 1):
                    if min(
                        self._horizontal_support(component, ys[row], xs[column], xs[column + 1], tolerance),
                        self._horizontal_support(component, ys[row + 1], xs[column], xs[column + 1], tolerance),
                        self._vertical_support(component, xs[column], ys[row], ys[row + 1], tolerance),
                        self._vertical_support(component, xs[column + 1], ys[row], ys[row + 1], tolerance),
                    ) >= 0.55:
                        closed_cells += 1
        closed_ratio = closed_cells / max(1, possible_cells)
        if intersection_ratio < 0.55:
            reasons.append("weak_intersection_graph")
        if outer_support < 0.65:
            reasons.append("weak_outer_boundary_support")
        if closed_cells == 0:
            reasons.append("poor_closed_cell_topology")
        if closed_ratio < 0.20 and possible_cells:
            reasons.append("weak_closed_cell_topology")
        confidence = (
            0.35 * intersection_ratio
            + 0.35 * closed_ratio
            + 0.20 * outer_support
            + 0.10 * min(1.0, len(component) / 12.0)
        )
        status = TRUSTED if confidence >= 0.82 and not reasons else REVIEW
        if len(xs) < 3 or len(ys) < 3 or closed_cells == 0:
            status = REJECTED if len(xs) < 3 or len(ys) < 3 else REVIEW
        x_normalized = tuple(value / page_width for value in xs)
        y_normalized = tuple(value / page_height for value in ys)
        grid = None
        if len(x_normalized) >= 2 and len(y_normalized) >= 2:
            from averon_import.services.ocr.physical_grid import PhysicalGridCell

            grid = PhysicalGrid(
                source=VECTOR,
                x_boundaries=x_normalized,
                y_boundaries=y_normalized,
                cells=tuple(
                    PhysicalGridCell(row, column, (x_normalized[column], y_normalized[row], x_normalized[column + 1], y_normalized[row + 1]))
                    for row in range(len(y_normalized) - 1)
                    for column in range(len(x_normalized) - 1)
                ),
                confidence=confidence,
                high_confidence=status == TRUSTED,
                reasons=tuple(dict.fromkeys(reasons)),
                metrics={
                    "vector_segment_count": len(component),
                    "horizontal_segment_count": len(horizontals),
                    "vertical_segment_count": len(verticals),
                    "intersection_ratio": round(intersection_ratio, 4),
                    "closed_cell_count": closed_cells,
                    "closed_cell_ratio": round(closed_ratio, 4),
                    "outer_boundary_support": round(outer_support, 4),
                },
            )
        if len(component) <= 128:
            line_graph_sample = [item.as_dict() for item in component]
        else:
            line_graph_sample = [
                item.as_dict() for item in component[:32] + component[-32:]
            ]
        evidence = {
            "segment_count": len(component),
            "horizontal_segment_count": len(horizontals),
            "vertical_segment_count": len(verticals),
            "intersection_ratio": round(intersection_ratio, 4),
            "closed_cell_count": closed_cells,
            "closed_cell_ratio": round(closed_ratio, 4),
            "outer_boundary_support": round(outer_support, 4),
            "line_graph": line_graph_sample,
            "line_graph_sampled": len(component) > len(line_graph_sample),
        }
        hypothesis = LocalGridHypothesis(
            source=VECTOR,
            bounds=bounds,
            grid=grid,
            confidence=confidence,
            status=status,
            evidence=evidence,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )
        return TableRegionCandidate(
            ref=f"vector:{component_index}",
            page_bounds=bounds,
            proposal_sources=(VECTOR,),
            vector_evidence=evidence,
            local_grid_hypotheses=(hypothesis,),
            physical_status=status,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            provenance=({"source": VECTOR, "component_index": component_index},),
        )

    @staticmethod
    def _clip_segment(
        segment: VectorLineSegment,
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> VectorLineSegment | None:
        """Clip an axis-aligned drawing segment to a candidate window."""
        if segment.orientation == "horizontal":
            y = (segment.start[1] + segment.end[1]) / 2
            if not top <= y <= bottom:
                return None
            start = max(left, min(segment.start[0], segment.end[0]))
            end = min(right, max(segment.start[0], segment.end[0]))
            if end - start <= 1.0:
                return None
            return VectorLineSegment(segment.orientation, (start, y), (end, y), segment.source_ref)
        x = (segment.start[0] + segment.end[0]) / 2
        if not left <= x <= right:
            return None
        start = max(top, min(segment.start[1], segment.end[1]))
        end = min(bottom, max(segment.start[1], segment.end[1]))
        if end - start <= 1.0:
            return None
        return VectorLineSegment(segment.orientation, (x, start), (x, end), segment.source_ref)

    def propose_region_object(
        self,
        page: Any,
        bounds: Bounds,
        proposal_index: int,
    ) -> TableRegionCandidate | None:
        """Build a vector hypothesis in a bounded association window.

        The window is only an association aid.  The result still requires
        independent axis-aligned drawing evidence and retains REVIEW when
        that evidence is incomplete; a provider bbox is never sufficient.
        """
        normalized = _bounds(bounds)
        rect = page.rect
        page_width, page_height = float(rect.width), float(rect.height)
        left, top = normalized[0] * page_width, normalized[1] * page_height
        right, bottom = normalized[2] * page_width, normalized[3] * page_height
        clipped = tuple(
            candidate
            for segment in self.extract_segments(page)
            if (candidate := self._clip_segment(segment, left, top, right, bottom)) is not None
        )
        if not clipped:
            return None
        candidate = self._component_candidate(clipped, proposal_index, page_width, page_height)
        if candidate is None:
            return None
        return replace(
            candidate,
            ref=f"vector:bounded:{proposal_index}",
            provenance=candidate.provenance + ({"bounded_window": list(normalized)},),
        )

    def propose_page_regions(self, pdf_path: Path, page_number: int, bounds: Iterable[Bounds]) -> tuple[TableRegionCandidate, ...]:
        with fitz.open(pdf_path) as document:
            if not 1 <= int(page_number) <= document.page_count:
                raise ValueError(f"Page {page_number} is outside the PDF")
            page = document[int(page_number) - 1]
            normalized_bounds = tuple(_bounds(item) for item in bounds)
            segments = self.extract_segments(page)
            result: list[TableRegionCandidate] = []
            for index, item in enumerate(normalized_bounds):
                rect = page.rect
                width, height = float(rect.width), float(rect.height)
                left, top = item[0] * width, item[1] * height
                right, bottom = item[2] * width, item[3] * height
                clipped = tuple(
                    candidate
                    for segment in segments
                    if (candidate := self._clip_segment(segment, left, top, right, bottom)) is not None
                )
                if not clipped:
                    continue
                candidate = self._component_candidate(clipped, index, width, height)
                if candidate is not None:
                    result.append(replace(
                        candidate,
                        ref=f"vector:bounded:{index}",
                        provenance=candidate.provenance + ({"bounded_window": list(item)},),
                    ))
            return tuple(result)

    def propose_page_object(self, page: Any) -> tuple[TableRegionCandidate, ...]:
        rect = page.rect
        segments = self.extract_segments(page)
        return tuple(
            candidate
            for index, component in enumerate(self._components(segments))
            if (candidate := self._component_candidate(component, index, float(rect.width), float(rect.height))) is not None
        )

    def propose_page(self, pdf_path: Path, page_number: int) -> tuple[TableRegionCandidate, ...]:
        with fitz.open(pdf_path) as document:
            if not 1 <= int(page_number) <= document.page_count:
                raise ValueError(f"Page {page_number} is outside the PDF")
            return self.propose_page_object(document[int(page_number) - 1])


def _provider_bounds(table: Mapping[str, Any], page_width: float, page_height: float) -> Bounds | None:
    raw = table.get("boundingBox")
    points = []
    if isinstance(raw, Mapping):
        for point in raw.get("vertices") or ():
            try:
                points.append((float(point["x"]), float(point["y"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not points:
        for cell in table.get("cells") or ():
            if not isinstance(cell, Mapping):
                continue
            cell_raw = cell.get("boundingBox") or {}
            for point in cell_raw.get("vertices") or ():
                try:
                    points.append((float(point["x"]), float(point["y"])))
                except (KeyError, TypeError, ValueError):
                    continue
    if not points or page_width <= 0 or page_height <= 0:
        return None
    return _bounds((
        min(point[0] for point in points) / page_width,
        min(point[1] for point in points) / page_height,
        max(point[0] for point in points) / page_width,
        max(point[1] for point in points) / page_height,
    ))


def provider_region_proposals(payload: Mapping[str, Any]) -> tuple[TableRegionCandidate, ...]:
    page = payload.get("page") if isinstance(payload, Mapping) else None
    annotation = payload.get("textAnnotation") if isinstance(payload, Mapping) else None
    annotation = annotation if isinstance(annotation, Mapping) else {}
    page_data = page if isinstance(page, Mapping) else {}
    page_width = float(page_data.get("width") or annotation.get("width") or 0.0)
    page_height = float(page_data.get("height") or annotation.get("height") or 0.0)
    result: list[TableRegionCandidate] = []
    for index, table in enumerate(annotation.get("tables") or ()):
        if not isinstance(table, Mapping):
            continue
        bounds = _provider_bounds(table, page_width, page_height)
        if bounds is None:
            continue
        evidence = {
            "table_index": index,
            "row_count": table.get("rowCount"),
            "column_count": table.get("columnCount"),
            "bbox": list(bounds),
            "page_size": {"width": page_width, "height": page_height},
        }
        result.append(TableRegionCandidate(
            ref=f"provider:{index}",
            page_bounds=bounds,
            proposal_sources=(PROVIDER,),
            provider_proposal_evidence=evidence,
            physical_status=REVIEW,
            rejection_reasons=("provider_only_no_physical_corroboration",),
            provenance=({"source": PROVIDER, "table_index": index},),
            provider_refs=(f"provider:{index}",),
            provider_association="unassociated",
        ))
    return tuple(result)


def _annotation_words(annotation: Mapping[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for block in annotation.get("blocks") or ():
        if not isinstance(block, Mapping):
            continue
        for line in block.get("lines") or ():
            if not isinstance(line, Mapping):
                continue
            for word in line.get("words") or ():
                if not isinstance(word, Mapping):
                    continue
                text = str(word.get("text") or "").strip()
                vertices = (word.get("boundingBox") or {}).get("vertices") if isinstance(word.get("boundingBox"), Mapping) else ()
                if text and vertices:
                    words.append({"text": text, "vertices": list(vertices)})
    return words


def _raw_bounds(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    points: list[tuple[float, float]] = []
    for point in raw.get("vertices") or ():
        if not isinstance(point, Mapping):
            continue
        try:
            points.append((float(point["x"]), float(point["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _region_family_context(
    candidate: TableRegionCandidate,
    table: Mapping[str, Any],
    mapping: Any,
    payload: Mapping[str, Any],
) -> BoundedFamilyContext:
    """Build family evidence only from the selected table's local geometry."""
    annotation = payload.get("textAnnotation") if isinstance(payload, Mapping) else None
    annotation = annotation if isinstance(annotation, Mapping) else {}
    page = payload.get("page") if isinstance(payload, Mapping) else None
    page = page if isinstance(page, Mapping) else {}
    try:
        width = float(page.get("width") or annotation.get("width") or 0.0)
        height = float(page.get("height") or annotation.get("height") or 0.0)
    except (TypeError, ValueError):
        width = height = 0.0
    table_bounds = (
        candidate.page_bounds[0] * width,
        candidate.page_bounds[1] * height,
        candidate.page_bounds[2] * width,
        candidate.page_bounds[3] * height,
    ) if width > 0 and height > 0 else candidate.page_bounds
    context = bounded_context_from_words(
        table_bounds,
        _annotation_words(annotation),
        page_height=height or 1.0,
    )
    header_rows = set(getattr(mapping, "header_rows", ()) or ())
    header_regions: list[ContextRegionEvidence] = []
    for index, raw_cell in enumerate(table.get("cells") or ()):
        if not isinstance(raw_cell, Mapping):
            continue
        try:
            row_index = int(raw_cell.get("rowIndex", 0))
        except (TypeError, ValueError):
            row_index = 0
        if header_rows and row_index not in header_rows:
            continue
        text = str(raw_cell.get("text") or "").strip()
        bounds = _raw_bounds(raw_cell.get("boundingBox"))
        if not text or bounds is None:
            continue
        header_regions.append(ContextRegionEvidence(
            kind="header",
            bounds=bounds,
            text=text,
            source="provider_table_cell",
            provenance=({
                "scope": "selected_table",
                "candidate_ref": candidate.ref,
                "table_index": (candidate.provider_proposal_evidence or {}).get("table_index"),
                "cell_index": index,
                "row_index": row_index,
            },),
        ))
    return BoundedFamilyContext(
        regions=tuple(context.regions) + tuple(header_regions),
        provenance=(
            {
                "scope": "selected_table",
                "candidate_ref": candidate.ref,
                "source": "provider_cells_and_bounded_words",
                "whole_page_full_text_used": False,
            },
        ),
    )


def _cluster_numeric(values: Iterable[float], tolerance: float) -> tuple[float, ...]:
    groups: list[list[float]] = []
    for value in sorted(float(item) for item in values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(sum(group) / len(group) for group in groups)


def _provider_cell_grid(
    candidate: TableRegionCandidate,
    table: Mapping[str, Any],
) -> PhysicalGrid | None:
    """Recover a regular local grid from provider cell geometry only when a
    physical vector/raster proposal also exists.

    The provider cells are secondary evidence here: provider-only candidates
    are intentionally excluded, and irregular/partial cell topology remains
    unresolved.  This adapter is useful for vector PDFs whose ruled lines are
    fragmented by the drawing stream but whose physical table boxes are
    complete.
    """
    if PROVIDER not in candidate.proposal_sources or not (
        RASTER in candidate.proposal_sources or VECTOR in candidate.proposal_sources
    ):
        return None
    if candidate.provider_association != "unique":
        return None
    evidence = candidate.provider_proposal_evidence or {}
    page_size = evidence.get("page_size") or {}
    try:
        width = float(page_size.get("width") or 0.0)
        height = float(page_size.get("height") or 0.0)
        row_count = int(table.get("rowCount") or 0)
        column_count = int(table.get("columnCount") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or row_count < 2 or column_count < 2:
        return None
    records: list[tuple[int, int, tuple[float, float, float, float], int, int]] = []
    x_values: list[float] = []
    y_values: list[float] = []
    widths: list[float] = []
    heights: list[float] = []
    for raw_cell in table.get("cells") or ():
        if not isinstance(raw_cell, Mapping):
            continue
        raw_bounds = _raw_bounds(raw_cell.get("boundingBox"))
        if raw_bounds is None:
            return None
        bounds = (
            raw_bounds[0] / width,
            raw_bounds[1] / height,
            raw_bounds[2] / width,
            raw_bounds[3] / height,
        )
        try:
            row_index = int(raw_cell.get("rowIndex", 0))
            column_index = int(raw_cell.get("columnIndex", 0))
            row_span = max(1, int(raw_cell.get("rowSpan", 1) or 1))
            column_span = max(1, int(raw_cell.get("columnSpan", 1) or 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= row_index < row_count and 0 <= column_index < column_count):
            return None
        records.append((row_index, column_index, bounds, row_span, column_span))
        x_values.extend((bounds[0], bounds[2]))
        y_values.extend((bounds[1], bounds[3]))
        widths.append(bounds[2] - bounds[0])
        heights.append(bounds[3] - bounds[1])
    if len(records) < row_count * column_count:
        return None
    median_width = sorted(widths)[len(widths) // 2]
    median_height = sorted(heights)[len(heights) // 2]
    x_boundaries = _cluster_numeric(x_values, max(0.006, median_width * 0.08))
    y_boundaries = _cluster_numeric(y_values, max(0.006, median_height * 0.20))
    if len(x_boundaries) != column_count + 1 or len(y_boundaries) != row_count + 1:
        return None

    def boundary_index(value: float, boundaries: tuple[float, ...], tolerance: float) -> int | None:
        index = min(range(len(boundaries)), key=lambda item: abs(boundaries[item] - value))
        return index if abs(boundaries[index] - value) <= tolerance else None

    occupied: set[tuple[int, int]] = set()
    tolerance_x = max(0.006, median_width * 0.08)
    tolerance_y = max(0.006, median_height * 0.20)
    for row_index, column_index, bounds, row_span, column_span in records:
        left = boundary_index(bounds[0], x_boundaries, tolerance_x)
        right = boundary_index(bounds[2], x_boundaries, tolerance_x)
        top = boundary_index(bounds[1], y_boundaries, tolerance_y)
        bottom = boundary_index(bounds[3], y_boundaries, tolerance_y)
        if left is None or right is None or top is None or bottom is None:
            return None
        if right - left != column_span or bottom - top != row_span:
            return None
        for row in range(top, bottom):
            for column in range(left, right):
                occupied.add((row, column))
    expected = {(row, column) for row in range(row_count) for column in range(column_count)}
    if occupied != expected:
        return None
    return PhysicalGrid(
        source="provider_secondary_physical_grid",
        x_boundaries=x_boundaries,
        y_boundaries=y_boundaries,
        cells=tuple(
            PhysicalGridCell(
                row,
                column,
                (x_boundaries[column], y_boundaries[row], x_boundaries[column + 1], y_boundaries[row + 1]),
            )
            for row in range(row_count)
            for column in range(column_count)
        ),
        confidence=0.86,
        high_confidence=True,
        metrics={
            "provider_cell_geometry": True,
            "provider_row_count": row_count,
            "provider_column_count": column_count,
            "physical_source_corroborated": True,
        },
    )


def _region_provider_table(
    candidate: TableRegionCandidate,
    payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    evidence = candidate.provider_proposal_evidence or {}
    try:
        table_index = int(evidence["table_index"])
    except (KeyError, TypeError, ValueError):
        return None
    annotation = payload.get("textAnnotation") if isinstance(payload, Mapping) else None
    tables = (annotation or {}).get("tables") or ()
    if not 0 <= table_index < len(tables) or not isinstance(tables[table_index], Mapping):
        return None
    return tables[table_index]


def _evaluate_region_schema(
    candidate: TableRegionCandidate,
    payload: Mapping[str, Any],
) -> TableRegionCandidate:
    table = _region_provider_table(candidate, payload)
    if table is None:
        return candidate
    cells: list[HeaderSourceCell] = []
    for raw_cell in table.get("cells") or ():
        if not isinstance(raw_cell, Mapping):
            continue
        try:
            row_index = int(raw_cell.get("rowIndex", 0))
            column_index = int(raw_cell.get("columnIndex", 0))
        except (TypeError, ValueError):
            continue
        cells.append(HeaderSourceCell(
            physical_row=row_index,
            physical_column=column_index,
            bbox=dict(raw_cell.get("boundingBox") or {}),
            raw_text=str(raw_cell.get("text") or ""),
            row_span=max(1, int(raw_cell.get("rowSpan", 1) or 1)),
            column_span=max(1, int(raw_cell.get("columnSpan", 1) or 1)),
            provenance=({"source": PROVIDER},),
        ))
    if not cells:
        return replace(candidate, rejection_reasons=tuple(dict.fromkeys(candidate.rejection_reasons + ("provider_schema_cells_missing",))))
    column_count = max(
        int(table.get("columnCount") or 0),
        max((cell.physical_column + cell.column_span for cell in cells), default=0),
    )
    mapping = map_semantic_header(cells, column_count)
    observed = ObservedSchema.from_mapping_result(mapping, column_count)
    family_context = _region_family_context(candidate, table, mapping, payload)
    family = DEFAULT_TABLE_FAMILY_CLASSIFIER.assess(family_context)
    profile = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=family)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=column_count,
        mapping=mapping,
        family=family,
        observed_schema=observed,
        profile_match=profile,
    )
    evidence = {
        "family_context": family_context.as_dict(),
        "family": family.as_dict(),
        "header_mapping": mapping.as_dict(),
        "observed_schema": observed.as_dict(),
        "profile_match": profile.as_dict(),
        "schema": assessment.as_dict(),
        "provider_cells": [dict(raw_cell) for raw_cell in (table.get("cells") or ()) if isinstance(raw_cell, Mapping)],
        "provider_page_size": dict((candidate.provider_proposal_evidence or {}).get("page_size") or {}),
    }
    reasons = list(candidate.rejection_reasons)
    if assessment.status == "unsupported":
        reasons.append("not_schema_supported")
    return replace(
        candidate,
        schema_status=assessment.status,
        profile_status=profile.status,
        schema_evidence=evidence,
        rejection_reasons=tuple(dict.fromkeys(reasons)),
    )


def _same_physical_region(first: TableRegionCandidate, second: TableRegionCandidate) -> bool:
    return bool(
        _iou(first.page_bounds, second.page_bounds) >= 0.30
        or (
            _boundary_distance(first.page_bounds, second.page_bounds) <= 0.012
            and _iou(first.page_bounds, second.page_bounds) >= 0.12
        )
    )


def _merge_physical(first: TableRegionCandidate, second: TableRegionCandidate) -> TableRegionCandidate:
    sources = tuple(sorted(set(first.proposal_sources + second.proposal_sources)))
    hypotheses = first.local_grid_hypotheses + second.local_grid_hypotheses
    statuses = {first.physical_status, second.physical_status}
    trusted = any(item.status == TRUSTED for item in hypotheses)
    physical_status = TRUSTED if trusted and REVIEW not in statuses else TRUSTED if trusted else REVIEW
    reasons = tuple(dict.fromkeys(first.rejection_reasons + second.rejection_reasons))
    if PROVIDER in sources:
        reasons = tuple(item for item in reasons if item != "provider_only_no_physical_corroboration")
    raster = first.raster_evidence or second.raster_evidence
    vector = first.vector_evidence or second.vector_evidence
    if raster is not None and vector is not None:
        relation = "RASTER_VECTOR_AGREE" if _iou(first.page_bounds, second.page_bounds) >= 0.50 else "RASTER_VECTOR_CONFLICT"
        raster = {**dict(raster), "vector_consensus": relation}
        vector = {**dict(vector), "raster_consensus": relation}
        if relation == "RASTER_VECTOR_CONFLICT":
            physical_status = REVIEW
            reasons = tuple(dict.fromkeys(reasons + ("raster_vector_conflict",)))
    return TableRegionCandidate(
        ref="region:" + "+".join(sorted(set((first.ref, second.ref)))),
        page_bounds=_union(first.page_bounds, second.page_bounds),
        proposal_sources=sources,
        raster_evidence=raster,
        vector_evidence=vector,
        local_grid_hypotheses=hypotheses,
        physical_status=physical_status,
        rejection_reasons=reasons,
        provenance=first.provenance + second.provenance,
    )


def fuse_physical_region_proposals(
    proposals: Iterable[TableRegionCandidate],
) -> tuple[TableRegionCandidate, ...]:
    """Fuse only physically corroborated proposals; preserve all clusters."""
    clusters: list[TableRegionCandidate] = []
    for proposal in sorted(proposals, key=lambda item: item.ref):
        matches = [index for index, current in enumerate(clusters) if _same_physical_region(current, proposal)]
        if not matches:
            clusters.append(proposal)
            continue
        first = matches[0]
        clusters[first] = _merge_physical(clusters[first], proposal)
    return tuple(sorted(clusters, key=lambda item: (item.page_bounds[1], item.page_bounds[0], item.ref)))


def _associate_provider(
    physical: tuple[TableRegionCandidate, ...],
    providers: tuple[TableRegionCandidate, ...],
) -> tuple[TableRegionCandidate, ...]:
    result = list(physical)
    for provider in providers:
        scored = sorted(
            [
                (
                    # IoU prefers a local physical hypothesis over a
                    # page-sized connected drawing that merely contains the
                    # provider bbox.  Containment alone would silently bind
                    # every provider table to an unrelated page frame.
                    _iou(provider.page_bounds, item.page_bounds),
                    index,
                )
                for index, item in enumerate(result)
            ],
            reverse=True,
        )
        plausible = [item for item in scored if item[0] >= 0.20]
        if not plausible:
            result.append(provider)
            continue
        if len(plausible) > 1 and plausible[0][0] - plausible[1][0] < 0.15:
            result.append(replace(provider, rejection_reasons=("association_ambiguous",), provider_association="ambiguous"))
            continue
        score, index = plausible[0]
        candidate = result[index]
        result[index] = replace(
            candidate,
            page_bounds=_union(candidate.page_bounds, provider.page_bounds),
            proposal_sources=tuple(sorted(set(candidate.proposal_sources + (PROVIDER,)))),
            provider_proposal_evidence=provider.provider_proposal_evidence,
            provider_refs=tuple(sorted(set(candidate.provider_refs + provider.provider_refs))),
            provider_association="unique",
            rejection_reasons=tuple(item for item in candidate.rejection_reasons if item != "provider_only_no_physical_corroboration"),
        )
    return tuple(sorted(result, key=lambda item: (item.page_bounds[1], item.page_bounds[0], item.ref)))


def compose_page_scene(
    page_ref: str,
    *,
    raster_proposals: Iterable[TableRegionCandidate] = (),
    vector_proposals: Iterable[TableRegionCandidate] = (),
    provider_proposals: Iterable[TableRegionCandidate] = (),
    payload: Mapping[str, Any] | None = None,
) -> PageSceneIR:
    """Compose a scene from independent proposal sources deterministically."""
    raster_values = tuple(raster_proposals)
    vector_values = tuple(vector_proposals)
    provider_values = tuple(provider_proposals)
    fused = fuse_physical_region_proposals(raster_values + vector_values)
    candidates = _associate_provider(fused, provider_values)
    if payload is not None:
        recovered: list[TableRegionCandidate] = []
        for candidate in candidates:
            table = _region_provider_table(candidate, payload)
            grid = _provider_cell_grid(candidate, table) if table is not None else None
            if grid is not None and candidate.trusted_grid is None:
                recovered.append(replace(
                    candidate,
                    local_grid_hypotheses=candidate.local_grid_hypotheses + (
                        LocalGridHypothesis(
                            source="provider_secondary_physical_grid",
                            bounds=grid.bounds,
                            grid=grid,
                            confidence=grid.confidence,
                            status=TRUSTED,
                            evidence=grid.metrics,
                        ),
                    ),
                    physical_status=TRUSTED,
                ))
            else:
                recovered.append(candidate)
        candidates = tuple(recovered)
    if payload is not None:
        candidates = tuple(_evaluate_region_schema(item, payload) for item in candidates)
    diagnostics = {
        "table_region_candidate_count": len(candidates),
        "table_region_trusted_count": sum(item.physical_status == TRUSTED for item in candidates),
        "table_region_review_count": sum(item.physical_status == REVIEW for item in candidates),
        "table_region_rejected_count": sum(item.physical_status == REJECTED for item in candidates),
        "raster_region_proposal_count": len(raster_values),
        "vector_region_proposal_count": len(vector_values),
        "provider_region_proposal_count": len(provider_values),
        "fused_region_count": max(0, len(raster_values) + len(vector_values) - len(fused)),
        "supported_schema_region_count": sum(item.schema_status == "supported" for item in candidates),
        "unknown_schema_region_count": sum(item.schema_status == "unknown_spec_schema" for item in candidates),
        "non_spec_region_count": sum(item.schema_status == "unsupported" for item in candidates),
        "multi_table_page": len(candidates) > 1,
        "multi_table_page_count": len(candidates) if len(candidates) > 1 else 0,
    }
    physical_sources = tuple(sorted({
        source
        for item in candidates
        for source in item.proposal_sources
        if source != PROVIDER
    }))
    return PageSceneIR(
        page_ref=page_ref,
        region_candidates=candidates,
        physical_sources=physical_sources,
        diagnostics=diagnostics,
    )


class PageSceneDetector:
    """Build a page scene while retaining physical candidates in shadow mode."""

    def __init__(
        self,
        *,
        raster_detector: RasterRuledTableGridDetector | None = None,
        vector_detector: VectorTableRegionDetector | None = None,
    ) -> None:
        self.raster_source = RasterRegionProposalSource(raster_detector)
        self.vector_detector = vector_detector or VectorTableRegionDetector()

    def analyze_page(
        self,
        pdf_path: Path | None,
        page_number: int,
        *,
        payload: Mapping[str, Any] | None = None,
        raster: RasterGridPage | None = None,
    ) -> PageSceneIR:
        raster_proposals: tuple[TableRegionCandidate, ...] = ()
        vector_proposals: tuple[TableRegionCandidate, ...] = ()
        if raster is None and pdf_path is not None:
            raster = self.raster_source.detector.analyze_page(pdf_path, page_number)
        if raster is not None:
            raster_proposals = self.raster_source.propose(raster)
        provider_proposals = provider_region_proposals(payload or {})
        if pdf_path is not None:
            vector_proposals = self.vector_detector.propose_page(pdf_path, page_number)
            # A provider bbox can narrow a vector association window, but it
            # cannot create a physical candidate by itself.  Each bounded
            # proposal below still requires independent PDF line evidence.
            propose_regions = getattr(self.vector_detector, "propose_page_regions", None)
            if callable(propose_regions) and provider_proposals:
                vector_proposals += propose_regions(
                    pdf_path,
                    page_number,
                    (item.page_bounds for item in provider_proposals),
                )
        return compose_page_scene(
            f"page:{int(page_number)}",
            raster_proposals=raster_proposals,
            vector_proposals=vector_proposals,
            provider_proposals=provider_proposals,
            payload=payload,
        )


def build_page_scene(
    pdf_path: Path | None,
    page_number: int,
    *,
    payload: Mapping[str, Any] | None = None,
    raster: RasterGridPage | None = None,
    detector: PageSceneDetector | None = None,
) -> PageSceneIR:
    return (detector or PageSceneDetector()).analyze_page(
        pdf_path,
        page_number,
        payload=payload,
        raster=raster,
    )


__all__ = [
    "PageSceneDetector",
    "PageSceneIR",
    "PROVIDER",
    "RASTER",
    "REJECTED",
    "REVIEW",
    "TRUSTED",
    "LocalGridHypothesis",
    "RasterRegionProposalSource",
    "TableRegionCandidate",
    "VectorLineSegment",
    "VectorTableRegionDetector",
    "build_page_scene",
    "compose_page_scene",
    "fuse_physical_region_proposals",
    "provider_region_proposals",
    "_evaluate_region_schema",
]
