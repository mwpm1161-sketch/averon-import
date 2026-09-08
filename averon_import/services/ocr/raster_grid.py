"""OpenCV implementation of ruled-table physical grid detection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import time

import cv2
import fitz
import numpy as np

from averon_import.services.ocr.physical_grid import (
    PhysicalGrid,
    PhysicalGridCell,
    PhysicalGridDetection,
)


DEFAULT_GRID_DPI = 300
DEFAULT_MAX_GRID_PIXELS = 20_000_000


@dataclass(slots=True)
class RasterGridPage:
    detection: PhysicalGridDetection
    grayscale: np.ndarray
    line_mask: np.ndarray

    @property
    def retained_bytes(self) -> int:
        return int(self.grayscale.nbytes + self.line_mask.nbytes)


def _cluster(values: list[float], tolerance: float) -> tuple[int, ...]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(int(round(sum(group) / len(group))) for group in groups)


def _horizontal_coverage(mask: np.ndarray, y: int, left: int, right: int) -> float:
    radius = 3
    strip = mask[max(0, y - radius) : min(mask.shape[0], y + radius + 1), left:right]
    if strip.size == 0 or right <= left:
        return 0.0
    occupied = np.any(strip > 0, axis=0)
    return float(np.count_nonzero(occupied) / max(1, right - left))


def _vertical_coverage(mask: np.ndarray, x: int, top: int, bottom: int) -> float:
    radius = 3
    strip = mask[top:bottom, max(0, x - radius) : min(mask.shape[1], x + radius + 1)]
    if strip.size == 0 or bottom <= top:
        return 0.0
    occupied = np.any(strip > 0, axis=1)
    return float(np.count_nonzero(occupied) / max(1, bottom - top))


def physical_row_raster_witness(
    raster: RasterGridPage,
    grid: PhysicalGrid,
    body_row_indexes: set[int] | list[int] | tuple[int, ...] | None,
    covered_row_indexes: set[int] | list[int] | tuple[int, ...] | None,
) -> dict:
    """Compare non-line raster evidence with reconstructed physical rows.

    This is deliberately a witness, not an OCR decision.  It only says that
    a selected body band contains visible ink after the detected rules are
    removed and that no geometry output row references that band.
    """
    grayscale = raster.grayscale
    line_mask = raster.line_mask
    if grayscale.ndim != 2 or line_mask.shape != grayscale.shape:
        return {
            "available": False,
            "body_row_indexes": [],
            "raster_nonempty_rows": [],
            "covered_row_indexes": [],
            "suspected_loss_rows": [],
        }
    body_rows = {
        int(value) for value in (body_row_indexes or [])
        if isinstance(value, (int, np.integer)) or str(value).lstrip("-").isdigit()
    }
    covered_rows = {
        int(value) for value in (covered_row_indexes or [])
        if isinstance(value, (int, np.integer)) or str(value).lstrip("-").isdigit()
    }
    if not body_rows:
        return {
            "available": True,
            "body_row_indexes": [],
            "raster_nonempty_rows": [],
            "covered_row_indexes": sorted(covered_rows),
            "suspected_loss_rows": [],
        }
    height, width = grayscale.shape
    ink = grayscale < 220
    # The detector's line mask is intentionally expanded a little so a rule's
    # antialiased edge cannot be mistaken for a glyph component.
    expanded_lines = cv2.dilate(
        (line_mask > 0).astype(np.uint8),
        np.ones((3, 3), dtype=np.uint8),
    ) > 0
    ink[expanded_lines] = False
    row_metrics: dict[str, dict[str, int | float | bool]] = {}
    nonempty: set[int] = set()
    for row_index in sorted(body_rows):
        if not 0 <= row_index < grid.row_count:
            continue
        left, top, right, bottom = grid.bounds
        x0 = max(0, min(width, int(round(left * width))))
        x1 = max(x0, min(width, int(round(right * width))))
        y0 = max(0, min(height, int(round(grid.y_boundaries[row_index] * height))))
        y1 = max(y0, min(height, int(round(grid.y_boundaries[row_index + 1] * height))))
        crop = (ink[y0:y1, x0:x1]).astype(np.uint8) * 255
        component_count = 0
        meaningful_area = 0
        if crop.size:
            count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
                crop, 8
            )
            cell_area = max(1, (x1 - x0) * (y1 - y0))
            minimum_area = max(4, int(cell_area * 0.00001))
            for component in stats[1:count]:
                component_width = int(component[cv2.CC_STAT_WIDTH])
                component_height = int(component[cv2.CC_STAT_HEIGHT])
                area = int(component[cv2.CC_STAT_AREA])
                if area < minimum_area:
                    continue
                # A remaining page-sized stroke is more likely a detector
                # defect than a glyph.  Normal text components stay well
                # inside the physical row band.
                if component_width >= (x1 - x0) * 0.85 and component_height <= 4:
                    continue
                if component_height >= (y1 - y0) * 0.85 and component_width <= 4:
                    continue
                component_count += 1
                meaningful_area += area
        is_nonempty = bool(
            component_count
            and meaningful_area >= max(8, int(max(1, (x1 - x0) * (y1 - y0)) * 0.00002))
        )
        row_metrics[str(row_index)] = {
            "component_count": component_count,
            "meaningful_ink_area": meaningful_area,
            "nonempty": is_nonempty,
        }
        if is_nonempty:
            nonempty.add(row_index)
    suspected = sorted(nonempty - covered_rows)
    return {
        "available": True,
        "body_row_indexes": sorted(body_rows),
        "raster_nonempty_rows": sorted(nonempty),
        "covered_row_indexes": sorted(covered_rows),
        "suspected_loss_rows": suspected,
        "row_metrics": row_metrics,
    }


class RasterRuledTableGridDetector:
    """Detect a dominant ruled table without assuming a schema column count."""

    source = "raster_cv"

    def __init__(
        self,
        *,
        dpi: int = DEFAULT_GRID_DPI,
        max_pixels: int = DEFAULT_MAX_GRID_PIXELS,
        high_confidence_threshold: float = 0.82,
    ) -> None:
        self.dpi = int(dpi)
        self.max_pixels = int(max_pixels)
        self.high_confidence_threshold = float(high_confidence_threshold)

    def detect_page(self, pdf_path: Path, page_number: int) -> PhysicalGridDetection:
        return self.analyze_page(pdf_path, page_number).detection

    def analyze_page(self, pdf_path: Path, page_number: int) -> RasterGridPage:
        started = time.perf_counter()
        with fitz.open(pdf_path) as document:
            if not 1 <= int(page_number) <= document.page_count:
                raise ValueError(f"Page {page_number} is outside the PDF")
            page = document[int(page_number) - 1]
            scale = self.dpi / 72.0
            projected_pixels = page.rect.width * scale * page.rect.height * scale
            if projected_pixels > self.max_pixels:
                scale *= math.sqrt(self.max_pixels / projected_pixels) * 0.995
            effective_dpi = scale * 72.0
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                alpha=False,
                colorspace=fitz.csGRAY,
            )
        grayscale = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width
        ).copy()
        render_ms = (time.perf_counter() - started) * 1000
        detection, line_mask = self.detect_image(grayscale)
        metrics = dict(detection.metrics)
        metrics["requested_dpi"] = self.dpi
        metrics["dpi"] = round(effective_dpi, 3)
        metrics["render_ms"] = round(render_ms, 3)
        grid = (
            replace(detection.grid, metrics=metrics)
            if detection.grid is not None
            else None
        )
        detection = PhysicalGridDetection(
            grid=grid,
            source=detection.source,
            reasons=detection.reasons,
            metrics=metrics,
            candidate_count=detection.candidate_count,
            selected_candidate=detection.selected_candidate,
            candidate_diagnostics=detection.candidate_diagnostics,
        )
        return RasterGridPage(detection, grayscale, line_mask)

    @staticmethod
    def _spacing_score(values: tuple[int, ...]) -> float:
        gaps = np.diff(np.asarray(values, dtype=float))
        if len(gaps) < 2:
            return 0.0
        typical = float(np.median(gaps))
        if typical <= 0:
            return 0.0
        deviation = np.abs(gaps - typical) / typical
        return float(max(0.0, min(1.0, 1.0 - np.mean(np.minimum(1.0, deviation)))))

    @staticmethod
    def _spacing_anomalies(values: tuple[int, ...]) -> list[int]:
        """Find gaps that look like two neighbouring gaps merged.

        Edge gaps are checked against the robust distribution of the other
        gaps as well.  This is deliberately evidence-based: it does not
        infer an expected row/column count, and only rejects a candidate when
        the observed edge gap is materially larger than the remaining grid
        topology.
        """
        gaps = np.diff(np.asarray(values, dtype=float))
        anomalies: list[int] = []
        for index, gap in enumerate(gaps):
            neighbours = []
            if index > 0:
                neighbours.append(gaps[index - 1])
            if index + 1 < len(gaps):
                neighbours.append(gaps[index + 1])
            if len(neighbours) < 2:
                # At the first/last gap, use the distribution of all other
                # gaps.  A two-boundary candidate has no robust reference and
                # is therefore left unchanged rather than guessed.
                reference = np.delete(gaps, index)
                if len(reference) < 2:
                    continue
                neighbours = [float(value) for value in reference]
            if max(neighbours) > min(neighbours) * 1.5:
                continue
            typical = float(np.median(np.asarray(neighbours, dtype=float)))
            if typical > 0 and gap > typical * 1.75 and gap - typical > 8.0:
                # A broad edge band followed by a second, consistently wider
                # band is a normal stepped header topology.  A missing first
                # or last internal rule instead produces one merged edge gap
                # followed immediately by the ordinary body distribution.
                # Use only the observed neighbouring distribution; do not
                # infer a page-specific row/column count.
                if index in {0, len(gaps) - 1} and gap > typical * 2.75:
                    # One missing rule merges two adjacent intervals; a much
                    # larger, continuously supported edge band is more safely
                    # treated as a legitimate header/outer-band transition.
                    continue
                if index == 0 and len(gaps) >= 3:
                    adjacent_typical = float(np.median(gaps[1:]))
                    if gaps[1] > adjacent_typical * 1.15:
                        continue
                elif index == len(gaps) - 1 and len(gaps) >= 3:
                    adjacent_typical = float(np.median(gaps[:-1]))
                    if gaps[-2] > adjacent_typical * 1.15:
                        continue
                anomalies.append(index + 1)
        return anomalies

    @staticmethod
    def _small_gap_anomalies(values: tuple[int, ...]) -> list[int]:
        """Find a second boundary hidden at a suspiciously narrow gap.

        The line clustering tolerance already merges raster duplicates a few
        pixels apart.  A surviving gap that is only a small fraction of its
        neighbours is therefore treated as topology uncertainty, not as a
        trusted narrow semantic column.
        """
        gaps = np.diff(np.asarray(values, dtype=float))
        if len(gaps) < 3:
            return []
        typical = float(np.median(gaps))
        if typical <= 0:
            return []
        anomalies: list[int] = []
        for index in range(1, len(gaps) - 1):
            gap = float(gaps[index])
            neighbours = (float(gaps[index - 1]), float(gaps[index + 1]))
            if gap < typical * 0.35 and min(neighbours) > typical * 0.55:
                anomalies.append(index + 1)
        return anomalies

    @staticmethod
    def _candidate_regions(
        line_mask: np.ndarray,
        width: int,
        height: int,
    ) -> list[tuple[int, int, int, int, np.ndarray]]:
        """Return disconnected ruled regions, excluding a page-sized frame."""
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            line_mask, 8
        )
        page_area = max(1, width * height)
        minimum_component_area = max(2000, int(page_area * 0.0002))
        regions: list[tuple[int, int, int, int, np.ndarray]] = []
        for index in range(1, count):
            x, y, region_width, region_height, area = stats[index]
            if int(area) < minimum_component_area:
                continue
            page_sized_frame = (
                int(x) <= 1
                and int(y) <= 1
                and int(region_width) >= width * 0.90
                and int(region_height) >= height * 0.90
            )
            if page_sized_frame:
                continue
            if region_width < max(80, width * 0.05) or region_height < max(50, height * 0.02):
                continue
            component = np.where(labels[y : y + region_height, x : x + region_width] == index, 255, 0).astype(np.uint8)
            regions.append((int(x), int(y), int(region_width), int(region_height), component))
        return regions

    def _detect_region(
        self,
        horizontal: np.ndarray,
        vertical: np.ndarray,
        region: tuple[int, int, int, int, np.ndarray],
        image_width: int,
        image_height: int,
    ) -> PhysicalGridDetection:
        offset_x, offset_y, region_width, region_height, component = region
        horizontal = cv2.bitwise_and(
            horizontal[offset_y : offset_y + region_height, offset_x : offset_x + region_width],
            component,
        )
        vertical = cv2.bitwise_and(
            vertical[offset_y : offset_y + region_height, offset_x : offset_x + region_width],
            component,
        )
        contours, _hierarchy = cv2.findContours(
            horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        y_values = [
            y + item_height / 2
            for contour in contours
            for _x, y, item_width, item_height in [cv2.boundingRect(contour)]
            if item_width >= region_width * 0.65
            and image_height * 0.01
            < offset_y + y + item_height / 2
            < image_height * 0.95
        ]
        # A divider with one missing physical column segment can be split
        # into contour fragments too short for the broad-contour filter
        # above. Recover only its Y candidate from the horizontal projection;
        # per-column support below remains the trust decision. Text cannot
        # enter this mask because it was opened with a long horizontal kernel.
        horizontal_projection = np.count_nonzero(horizontal > 0, axis=1)
        projection_threshold = max(1, int(region_width * 0.45))
        projection_mask = (horizontal_projection >= projection_threshold).astype(np.uint8)
        existing_y_min = min(y_values, default=None)
        existing_y_max = max(y_values, default=None)
        projection_count, projection_labels, _projection_stats, _ = cv2.connectedComponentsWithStats(
            projection_mask, 8
        )
        for projection_index in range(1, projection_count):
            _x, projection_y, _projection_width, projection_height = cv2.boundingRect(
                np.uint8(projection_labels == projection_index)
            )
            if projection_height <= 0:
                continue
            center = projection_y + projection_height / 2
            if (
                existing_y_min is not None
                and existing_y_max is not None
                and existing_y_min <= center <= existing_y_max
                and image_height * 0.01 < offset_y + center < image_height * 0.95
            ):
                y_values.append(center)
        contours, _hierarchy = cv2.findContours(
            vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        x_values = [
            x + item_width / 2
            for contour in contours
            for x, _y, item_width, item_height in [cv2.boundingRect(contour)]
            if item_height >= region_height * 0.45
            and image_width * 0.02
            < offset_x + x + item_width / 2
            < image_width * 0.99
        ]
        tolerance = max(3.0, min(region_width, region_height) * 0.0015)
        x_lines = _cluster(x_values, tolerance)
        y_lines = _cluster(y_values, tolerance)
        reasons: list[str] = []
        if len(x_lines) < 3:
            reasons.append("too_few_vertical_lines")
        if len(y_lines) < 3:
            reasons.append("too_few_horizontal_lines")
        metrics: dict[str, float | int | str] = {
            "region_x_px": offset_x,
            "region_y_px": offset_y,
            "region_width_px": region_width,
            "region_height_px": region_height,
            "component_area_ratio": round(
                float(np.count_nonzero(component)) / max(1, image_width * image_height),
                6,
            ),
            "vertical_line_count": len(x_lines),
            "horizontal_line_count": len(y_lines),
        }
        if reasons:
            return PhysicalGridDetection(
                grid=None,
                source=self.source,
                reasons=tuple(reasons),
                metrics=metrics,
            )

        left, right = x_lines[0], x_lines[-1]
        top, bottom = y_lines[0], y_lines[-1]
        horizontal_scores = [
            _horizontal_coverage(horizontal, y, left, right) for y in y_lines
        ]
        vertical_scores = [
            _vertical_coverage(vertical, x, top, bottom) for x in x_lines
        ]
        intersections = [
            bool(
                np.any(
                    horizontal[
                        max(0, y - 4) : min(region_height, y + 5),
                        max(0, x - 4) : min(region_width, x + 5),
                    ]
                )
                and np.any(
                    vertical[
                        max(0, y - 4) : min(region_height, y + 5),
                        max(0, x - 4) : min(region_width, x + 5),
                    ]
                )
            )
            for y in y_lines
            for x in x_lines
        ]
        intersection_ratio = sum(intersections) / max(1, len(intersections))
        horizontal_continuity = float(np.mean(horizontal_scores))
        vertical_continuity = float(np.mean(vertical_scores))
        table_area_ratio = (
            max(0, right - left) * max(0, bottom - top)
            / max(1, image_width * image_height)
        )
        minimum_cell_width = min(np.diff(np.array(x_lines, dtype=float)))
        minimum_cell_height = min(np.diff(np.array(y_lines, dtype=float)))
        area_score = min(1.0, table_area_ratio / 0.25)
        support_score = min(
            1.0,
            ((len(x_lines) - 1) * (len(y_lines) - 1)) / 20.0,
        )
        confidence = (
            0.40 * intersection_ratio
            + 0.20 * horizontal_continuity
            + 0.20 * vertical_continuity
            + 0.10 * area_score
            + 0.10 * support_score
        )
        if intersection_ratio < 0.85:
            reasons.append("weak_intersections")
        if horizontal_continuity < 0.75:
            reasons.append("broken_horizontal_lines")
        if vertical_continuity < 0.65:
            reasons.append("broken_vertical_lines")
        if table_area_ratio < 0.05:
            reasons.append("table_area_too_small")
        if minimum_cell_width < max(4.0, region_width * 0.002):
            reasons.append("narrow_cells")
        if minimum_cell_height < max(4.0, region_height * 0.002):
            reasons.append("short_cells")
        row_spacing_anomalies = self._spacing_anomalies(y_lines)
        column_spacing_anomalies = self._spacing_anomalies(x_lines)
        small_row_gaps = self._small_gap_anomalies(y_lines)
        small_column_gaps = self._small_gap_anomalies(x_lines)
        if row_spacing_anomalies:
            reasons.append("row_spacing_anomaly")
        if column_spacing_anomalies:
            reasons.append("column_spacing_anomaly")
        if small_row_gaps:
            reasons.append("small_internal_row_gap")
        if small_column_gaps:
            reasons.append("small_internal_column_gap")
        # A high score is not sufficient for a physical grid.  The declared
        # topology must also be internally supported: every boundary needs
        # broad line support and no unexplained cell-sized hole may be hidden
        # behind an otherwise regular-looking candidate.
        if min(horizontal_scores, default=0.0) < 0.75:
            reasons.append("weak_declared_horizontal_boundary")
        if min(vertical_scores, default=0.0) < 0.65:
            reasons.append("weak_declared_vertical_boundary")

        edge_tolerance_x = max(8.0, region_width * 0.04)
        edge_tolerance_y = max(8.0, region_height * 0.04)
        if (
            left > edge_tolerance_x
            and offset_x > image_width * 0.02
        ):
            reasons.append("missing_outer_left_boundary")
        if (
            region_width - right > edge_tolerance_x
            and offset_x + region_width < image_width * 0.98
        ):
            reasons.append("missing_outer_right_boundary")
        if (
            top > edge_tolerance_y
            and offset_y > image_height * 0.02
        ):
            reasons.append("missing_outer_top_boundary")
        if (
            region_height - bottom > edge_tolerance_y
            and offset_y + region_height < image_height * 0.98
        ):
            reasons.append("missing_outer_bottom_boundary")

        row_topology_anomalies = self._spacing_anomalies(y_lines)
        column_topology_anomalies = self._spacing_anomalies(x_lines)
        if row_topology_anomalies:
            reasons.append("unexplained_row_spacing_topology")
        if column_topology_anomalies:
            reasons.append("unexplained_column_spacing_topology")
        expected_intersections = len(x_lines) * len(y_lines)
        if expected_intersections and intersection_ratio < 0.98:
            reasons.append("grid_intersection_integrity_failure")
        segment_support = [
            _vertical_coverage(
                vertical,
                x,
                y_lines[row_index],
                y_lines[row_index + 1],
            )
            for x in x_lines[1:-1]
            for row_index in range(len(y_lines) - 1)
        ]
        unsupported_span_segments = sum(1 for value in segment_support if value < 0.55)
        if unsupported_span_segments:
            reasons.append("merged_cell_span_ambiguity")
        horizontal_segment_support = [
            _horizontal_coverage(
                horizontal,
                y,
                x_lines[column_index],
                x_lines[column_index + 1],
            )
            for y in y_lines[1:-1]
            for column_index in range(len(x_lines) - 1)
        ]
        unsupported_horizontal_span_segments = sum(
            1 for value in horizontal_segment_support if value < 0.55
        )
        if unsupported_horizontal_span_segments:
            reasons.append("merged_cell_span_ambiguity")
        high_confidence = confidence >= self.high_confidence_threshold and not reasons
        if not high_confidence and not reasons:
            reasons.append("confidence_below_threshold")
        x_normalized = tuple(
            float((offset_x + value) / image_width) for value in x_lines
        )
        y_normalized = tuple(
            float((offset_y + value) / image_height) for value in y_lines
        )
        cells = tuple(
            PhysicalGridCell(
                row_index=row,
                column_index=column,
                bounds=(
                    x_normalized[column],
                    y_normalized[row],
                    x_normalized[column + 1],
                    y_normalized[row + 1],
                ),
            )
            for row in range(len(y_normalized) - 1)
            for column in range(len(x_normalized) - 1)
        )
        metrics.update({
            "row_count": len(y_lines) - 1,
            "column_count": len(x_lines) - 1,
            "intersection_ratio": round(intersection_ratio, 4),
            "horizontal_continuity": round(horizontal_continuity, 4),
            "vertical_continuity": round(vertical_continuity, 4),
            "table_area_ratio": round(table_area_ratio, 4),
            "minimum_cell_width_px": round(float(minimum_cell_width), 2),
            "minimum_cell_height_px": round(float(minimum_cell_height), 2),
            "row_spacing_anomaly_count": len(row_spacing_anomalies),
            "column_spacing_anomaly_count": len(column_spacing_anomalies),
            "row_topology_anomaly_count": len(row_topology_anomalies),
            "column_topology_anomaly_count": len(column_topology_anomalies),
            "small_row_gap_count": len(small_row_gaps),
            "small_column_gap_count": len(small_column_gaps),
            "unsupported_span_segment_count": unsupported_span_segments,
            "unsupported_horizontal_span_segment_count": unsupported_horizontal_span_segments,
            "minimum_horizontal_boundary_support": round(
                min(horizontal_scores, default=0.0), 4
            ),
            "minimum_vertical_boundary_support": round(
                min(vertical_scores, default=0.0), 4
            ),
            "layout_confidence": round(confidence, 4),
            "layout_trusted": high_confidence,
            "spacing_score": round(
                (self._spacing_score(x_lines) + self._spacing_score(y_lines)) / 2,
                4,
            ),
            "confidence": round(confidence, 4),
        })
        grid = PhysicalGrid(
            source=self.source,
            x_boundaries=x_normalized,
            y_boundaries=y_normalized,
            cells=cells,
            confidence=confidence,
            high_confidence=high_confidence,
            reasons=tuple(reasons),
            metrics=metrics,
        )
        return PhysicalGridDetection(
            grid=grid,
            source=self.source,
            reasons=tuple(reasons),
            metrics=metrics,
        )

    def detect_image(
        self, grayscale: np.ndarray
    ) -> tuple[PhysicalGridDetection, np.ndarray]:
        started = time.perf_counter()
        if grayscale.ndim != 2 or not grayscale.size:
            raise ValueError("Grid detector expects a non-empty grayscale image")
        height, width = grayscale.shape
        _threshold, binary = cv2.threshold(
            grayscale,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        horizontal = cv2.morphologyEx(
            binary,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(
                cv2.MORPH_RECT, (max(40, width // 30), 1)
            ),
        )
        horizontal = cv2.morphologyEx(
            horizontal,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(
                cv2.MORPH_RECT, (max(3, (width // 300) * 2 + 1), 1)
            ),
        )
        vertical = cv2.morphologyEx(
            binary,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(
                cv2.MORPH_RECT, (1, max(40, height // 30))
            ),
        )
        vertical = cv2.morphologyEx(
            vertical,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(
                cv2.MORPH_RECT, (1, max(3, (height // 300) * 2 + 1))
            ),
        )
        line_mask = cv2.bitwise_or(horizontal, vertical)

        basic_metrics: dict[str, float | int | str] = {
            "image_width_px": int(width),
            "image_height_px": int(height),
            "dpi": self.dpi,
            "peak_array_bytes_estimate": int(
                grayscale.nbytes
                + binary.nbytes
                + horizontal.nbytes
                + vertical.nbytes
                + line_mask.nbytes
            ),
            "retained_array_bytes": int(grayscale.nbytes + line_mask.nbytes),
        }
        regions = self._candidate_regions(line_mask, width, height)
        detections = [
            self._detect_region(horizontal, vertical, region, width, height)
            for region in regions
        ]
        viable = [
            (index, detection)
            for index, detection in enumerate(detections)
            if detection.grid is not None
        ]
        candidate_diagnostics = tuple(
            {
                "index": index,
                "confidence": round(
                    float(detection.grid.confidence if detection.grid else 0.0), 4
                ),
                "high_confidence": bool(
                    detection.grid and detection.grid.high_confidence
                ),
                "reasons": list(detection.reasons),
                "candidate_score": None,
                "spacing_score": (
                    round(float(detection.grid.metrics.get("spacing_score", 0.0) or 0.0), 4)
                    if detection.grid else None
                ),
                "area_score": (
                    round(min(1.0, float(detection.grid.metrics.get("table_area_ratio", 0.0) or 0.0) / 0.25), 4)
                    if detection.grid else None
                ),
                "selected": False,
                "rejected_reason": (
                    detection.reasons[0] if detection.reasons else "not_viable"
                ),
                "row_count": int(detection.grid.row_count) if detection.grid else 0,
                "column_count": int(detection.grid.column_count) if detection.grid else 0,
                "bounds": (
                    {
                        "x": round(float(detection.grid.bounds[0]), 6),
                        "y": round(float(detection.grid.bounds[1]), 6),
                        "width": round(float(detection.grid.bounds[2] - detection.grid.bounds[0]), 6),
                        "height": round(float(detection.grid.bounds[3] - detection.grid.bounds[1]), 6),
                    }
                    if detection.grid
                    else {}
                ),
            }
            for index, detection in enumerate(detections)
        )
        metrics = {
            **basic_metrics,
            "candidate_count": len(detections),
        }
        if not viable:
            metrics["detect_ms"] = round((time.perf_counter() - started) * 1000, 3)
            return (
                PhysicalGridDetection(
                    grid=None,
                    source=self.source,
                    reasons=("no_table_candidate",),
                    metrics=metrics,
                    candidate_count=len(detections),
                    candidate_diagnostics=candidate_diagnostics,
                ),
                line_mask,
            )

        def candidate_score(item: tuple[int, PhysicalGridDetection]) -> float:
            _index, detection = item
            grid = detection.grid
            assert grid is not None
            spacing = float(grid.metrics.get("spacing_score", 0.0) or 0.0)
            area = float(grid.metrics.get("table_area_ratio", 0.0) or 0.0)
            return (
                0.70 * grid.confidence
                + 0.15 * spacing
                + 0.15 * min(1.0, area / 0.25)
            )

        ranked = sorted(viable, key=candidate_score, reverse=True)
        candidate_scores = {
            index: candidate_score((index, item)) for index, item in viable
        }

        def scored_diagnostics(
            *, ambiguous: bool = False, selected_index: int | None = None
        ) -> tuple[dict, ...]:
            result: list[dict] = []
            for index, detection in enumerate(detections):
                item = dict(candidate_diagnostics[index])
                if detection.grid is None:
                    item.setdefault("rejected_reason", detection.reasons[0] if detection.reasons else "not_viable")
                else:
                    area = float(detection.grid.metrics.get("table_area_ratio", 0.0) or 0.0)
                    item.update({
                        "spacing_score": round(float(detection.grid.metrics.get("spacing_score", 0.0) or 0.0), 4),
                        "area_score": round(min(1.0, area / 0.25), 4),
                        "candidate_score": round(candidate_scores[index], 4),
                        "selected": bool(index == selected_index and not ambiguous),
                        "rejected_reason": (
                            "ambiguous_table_candidates"
                            if ambiguous
                            else (None if index == selected_index else "lower_candidate_score")
                        ),
                    })
                result.append(item)
            return tuple(result)

        selected_index, selected = ranked[0]
        selected_grid = selected.grid
        assert selected_grid is not None
        top_score = candidate_score(ranked[0])
        if len(ranked) > 1:
            _second_index, second = ranked[1]
            second_grid = second.grid
            assert second_grid is not None
            second_score = candidate_score(ranked[1])
            area_ratio = min(
                float(selected_grid.metrics.get("table_area_ratio", 0.0) or 0.0),
                float(second_grid.metrics.get("table_area_ratio", 0.0) or 0.0),
            ) / max(
                1e-9,
                max(
                    float(selected_grid.metrics.get("table_area_ratio", 0.0) or 0.0),
                    float(second_grid.metrics.get("table_area_ratio", 0.0) or 0.0),
                ),
            )
            if (
                selected_grid.high_confidence
                and second_grid.high_confidence
                and area_ratio >= 0.50
                and top_score - second_score < 0.08
            ):
                metrics["detect_ms"] = round((time.perf_counter() - started) * 1000, 3)
                return (
                    PhysicalGridDetection(
                        grid=None,
                        source=self.source,
                        reasons=("ambiguous_table_candidates",),
                        metrics=metrics,
                        candidate_count=len(detections),
                        candidate_diagnostics=scored_diagnostics(ambiguous=True),
                    ),
                    line_mask,
                )

        selected_metrics = dict(selected_grid.metrics)
        selected_metrics.update({
            **basic_metrics,
            "candidate_count": len(detections),
            "selected_candidate": selected_index,
            "candidate_score": round(top_score, 4),
            "detect_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        selected_grid = PhysicalGrid(
            source=selected_grid.source,
            x_boundaries=selected_grid.x_boundaries,
            y_boundaries=selected_grid.y_boundaries,
            cells=selected_grid.cells,
            confidence=selected_grid.confidence,
            high_confidence=selected_grid.high_confidence,
            reasons=selected_grid.reasons,
            metrics=selected_metrics,
        )
        metrics.update(selected_metrics)
        return (
            PhysicalGridDetection(
                grid=selected_grid,
                source=self.source,
                reasons=selected_grid.reasons,
                metrics=metrics,
                candidate_count=len(detections),
                selected_candidate=selected_index,
                candidate_diagnostics=scored_diagnostics(selected_index=selected_index),
            ),
            line_mask,
        )


def prepare_exact_cell_crop(
    raster: RasterGridPage,
    cell: PhysicalGridCell,
    *,
    scale: int = 2,
    padding_px: int = 24,
) -> np.ndarray:
    """Remove detected rules from an exact cell and normalize glyph scale."""
    height, width = raster.grayscale.shape
    left = max(0, min(width - 1, int(round(cell.bounds[0] * width))))
    top = max(0, min(height - 1, int(round(cell.bounds[1] * height))))
    right = max(left + 1, min(width, int(round(cell.bounds[2] * width))))
    bottom = max(top + 1, min(height, int(round(cell.bounds[3] * height))))
    crop = raster.grayscale[top:bottom, left:right].copy()
    cell_line_mask = raster.line_mask[top:bottom, left:right]
    crop[cell_line_mask > 0] = 255
    crop = cv2.copyMakeBorder(
        crop,
        padding_px,
        padding_px,
        padding_px,
        padding_px,
        cv2.BORDER_CONSTANT,
        value=255,
    )
    if scale != 1:
        crop = cv2.resize(
            crop,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC,
        )
    return crop


def crop_has_glyph(
    crop: np.ndarray,
    *,
    minimum_area: int = 8,
    minimum_width: int = 2,
    minimum_height: int = 4,
) -> bool:
    """Reject visually empty cells before any paid verification request."""
    _threshold, binary = cv2.threshold(
        crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, 8)
    return any(
        int(stat[cv2.CC_STAT_AREA]) >= minimum_area
        and int(stat[cv2.CC_STAT_WIDTH]) >= minimum_width
        and int(stat[cv2.CC_STAT_HEIGHT]) >= minimum_height
        for stat in stats[1:count]
    )


def crop_has_isolated_glyph(crop: np.ndarray) -> bool:
    """Require an interior glyph component for identity-cell loss evidence.

    This is intentionally stricter than the paid exact-cell precheck: a long
    border remnant or text leaking across a neighboring cell must not create
    an identity blocker.
    """
    _threshold, binary = cv2.threshold(
        crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    height, width = crop.shape[:2]
    for index, stat in enumerate(stats[1:count], start=1):
        component_width = int(stat[cv2.CC_STAT_WIDTH])
        component_height = int(stat[cv2.CC_STAT_HEIGHT])
        area = int(stat[cv2.CC_STAT_AREA])
        center_x, center_y = centroids[index]
        if (
            area >= 8
            and component_width >= 2
            and component_height >= 4
            and component_width <= width * 0.45
            and component_height <= height * 0.60
            and width * 0.10 <= center_x <= width * 0.90
            and height * 0.10 <= center_y <= height * 0.90
        ):
            return True
    return False


def encode_png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("Unable to encode exact cell as PNG")
    return encoded.tobytes()
