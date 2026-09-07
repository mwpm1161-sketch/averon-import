from __future__ import annotations

import base64
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from averon_import.services.ocr.base import OcrProviderError, OcrRow
from averon_import.services.ocr.critical_verification import (
    attach_exact_cell_candidate,
)
from averon_import.services.ocr.physical_grid import (
    PhysicalGrid,
    PhysicalGridCell,
    PhysicalGridDetection,
    SpatialWord,
    assign_words_to_cells,
    validate_physical_grid,
)
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.raster_grid import (
    RasterGridPage,
    RasterRuledTableGridDetector,
    crop_has_glyph,
    prepare_exact_cell_crop,
)
from averon_import.services.ocr.reconstruction import (
    reconstruct_page_rows,
    rows_from_physical_grid,
)
from averon_import.services.ocr.schema_recognizer import (
    AMBIGUOUS,
    SUPPORTED,
    UNSUPPORTED,
    SupportedSpecificationSchemaRecognizer,
)
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.review_policy import critical_blockers_for_row
from averon_import.services.ocr.yandex_vision import (
    YandexVisionProvider,
    _HttpResponse,
)
from averon_import.services.secrets import MemorySecretStore


def _normalized_grid(
    rows: int,
    columns: int,
    *,
    confidence: float = 1.0,
    high_confidence: bool = True,
) -> PhysicalGrid:
    xs = tuple(index / columns for index in range(columns + 1))
    ys = tuple(index / rows for index in range(rows + 1))
    cells = tuple(
        PhysicalGridCell(
            row,
            column,
            (xs[column], ys[row], xs[column + 1], ys[row + 1]),
        )
        for row in range(rows)
        for column in range(columns)
    )
    return PhysicalGrid(
        source="test_grid",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=cells,
        confidence=confidence,
        high_confidence=high_confidence,
        reasons=() if high_confidence else ("weak_intersections",),
    )


def _ruled_image(rows: int = 6, columns: int = 4) -> np.ndarray:
    image = np.full((700, 1000), 255, dtype=np.uint8)
    left, top, right, bottom = 80, 70, 920, 630
    for column in range(columns + 1):
        x = round(left + (right - left) * column / columns)
        cv2.line(image, (x, top), (x, bottom), 0, 3)
    for row in range(rows + 1):
        y = round(top + (bottom - top) * row / rows)
        cv2.line(image, (left, y), (right, y), 0, 3)
    return image


def _draw_table(
    image: np.ndarray,
    bounds: tuple[int, int, int, int],
    *,
    rows: int,
    columns: int,
    thickness: int = 3,
) -> None:
    left, top, right, bottom = bounds
    for column in range(columns + 1):
        x = round(left + (right - left) * column / columns)
        cv2.line(image, (x, top), (x, bottom), 0, thickness)
    for row in range(rows + 1):
        y = round(top + (bottom - top) * row / rows)
        cv2.line(image, (left, y), (right, y), 0, thickness)


def _draw_grid_with_boundaries(
    x_boundaries: list[int], y_boundaries: list[int]
) -> np.ndarray:
    image = np.full((700, 1000), 255, dtype=np.uint8)
    for x in x_boundaries:
        cv2.line(image, (x, y_boundaries[0]), (x, y_boundaries[-1]), 0, 3)
    for y in y_boundaries:
        cv2.line(image, (x_boundaries[0], y), (x_boundaries[-1], y), 0, 3)
    return image


def _word(text: str, row: int, column: int, *, rows: int = 4, columns: int = 9) -> dict:
    cell_width = 1000 / columns
    cell_height = 1000 / rows
    left = column * cell_width + 8
    top = row * cell_height + 12
    right = min((column + 1) * cell_width - 8, left + max(16, len(text) * 7))
    bottom = top + 28
    return {
        "text": text,
        "boundingBox": {
            "vertices": [
                {"x": left, "y": top},
                {"x": right, "y": top},
                {"x": right, "y": bottom},
                {"x": left, "y": bottom},
            ]
        },
    }


def _geometry_payload() -> dict:
    header = [
        "Поз.",
        "Наименование",
        "Тип, марка",
        "Код",
        "Изготовитель",
        "Ед. изм.",
        "Количество",
        "Масса",
        "Примечание",
    ]
    words = [_word(text, 0, column) for column, text in enumerate(header)]
    # Incomplete numbering is intentional: narrow digits are often absent.
    words.extend(_word(text, 1, column) for column, text in ((2, "3"), (4, "5"), (5, "6"), (6, "7")))
    words.extend([
        _word("Установка", 2, 1),
        _word("компл.", 2, 5),
        _word("1", 2, 6),
        _word("Моноблок", 3, 1),
        _word("M-1", 3, 2),
    ])
    # Provider table deliberately collapses the last two physical rows and
    # the mass/note columns.  It is evidence only for geometry-first.
    table_cells = [
        {
            "text": "Наименование",
            "rowIndex": 0,
            "columnIndex": 1,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": {"vertices": [
                {"x": 0, "y": 0}, {"x": 1000, "y": 0},
                {"x": 1000, "y": 250}, {"x": 0, "y": 250},
            ]},
        },
        {
            "text": "Установка\nМоноблок",
            "rowIndex": 1,
            "columnIndex": 1,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": {"vertices": [
                {"x": 0, "y": 500}, {"x": 1000, "y": 500},
                {"x": 1000, "y": 1000}, {"x": 0, "y": 1000},
            ]},
        },
    ]
    return {
        "page": {"width": 1000, "height": 1000},
        "textAnnotation": {
            "width": 1000,
            "height": 1000,
            "blocks": [{"lines": [{"words": words}]}],
            "tables": [{"rowCount": 2, "columnCount": 8, "cells": table_cells}],
        },
    }


def test_raster_detector_is_generic_and_high_confidence_for_four_columns():
    detection, _mask = RasterRuledTableGridDetector().detect_image(
        _ruled_image(rows=6, columns=4)
    )
    assert detection.high_confidence is True
    assert detection.grid is not None
    assert detection.grid.row_count == 6
    assert detection.grid.column_count == 4
    assert detection.grid.confidence >= 0.82


def test_raster_detector_rejects_image_without_a_grid():
    image = np.full((700, 1000), 255, dtype=np.uint8)
    cv2.line(image, (80, 100), (920, 100), 0, 3)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is False
    assert detection.grid is None
    assert detection.reasons == ("no_table_candidate",)


def test_spatial_assignment_uses_overlap_when_center_is_just_outside():
    grid = _normalized_grid(1, 2)
    word = SpatialWord("42", (0.95, 0.2, 1.08, 0.8))
    assigned, ambiguous, unassigned = assign_words_to_cells(grid, [word])
    assert ambiguous == []
    assert unassigned == []
    assert assigned[(0, 1)][0].text == "42"


def test_spatial_assignment_keeps_fully_inside_word_unambiguous():
    grid = _normalized_grid(2, 2)
    assigned, ambiguous, unassigned = assign_words_to_cells(
        grid, [SpatialWord("42", (0.60, 0.60, 0.70, 0.70))]
    )
    assert list(assigned) == [(1, 1)]
    assert ambiguous == []
    assert unassigned == []


def test_spatial_assignment_allows_a_small_border_touch():
    grid = _normalized_grid(1, 2)
    assigned, ambiguous, unassigned = assign_words_to_cells(
        grid, [SpatialWord("42", (0.40, 0.20, 0.505, 0.80))]
    )
    assert list(assigned) == [(0, 0)]
    assert ambiguous == []
    assert unassigned == []


@pytest.mark.parametrize(
    "bounds,reason",
    [
        ((0.45, 0.10, 0.55, 0.20), "overlap_tie"),
        ((0.45, 0.45, 0.55, 0.55), "overlap_tie"),
        ((0.40, 0.10, 0.58, 0.20), "overlap_near_tie"),
    ],
)
def test_spatial_assignment_returns_cross_border_words_as_ambiguous(bounds, reason):
    grid = _normalized_grid(2, 2)
    assigned, ambiguous, unassigned = assign_words_to_cells(
        grid, [SpatialWord("critical", bounds)]
    )
    assert assigned == {}
    assert len(ambiguous) == 1
    assert ambiguous[0].reason == reason
    assert len(ambiguous[0].candidates) >= 2
    assert unassigned == []


def test_ambiguous_quantity_word_creates_structural_review_evidence():
    payload = _geometry_payload()
    quantity_word = next(
        word
        for word in payload["textAnnotation"]["blocks"][0]["lines"][0]["words"]
        if word["text"] == "1"
    )
    quantity_word["boundingBox"] = {
        "vertices": [
            {"x": 750, "y": 512},
            {"x": 810, "y": 512},
            {"x": 810, "y": 540},
            {"x": 750, "y": 540},
        ]
    }
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    row = next(row for row in rows if row.values.get("name") == "Установка")
    assert row.values.get("quantity", "") == ""
    assert row.metadata["structural_ambiguity"] is True
    assert "structural_ambiguity" in row.metadata["review_reasons"]
    assert diagnostics["word_assignment"]["ambiguous_word_count"] == 1


def test_invalid_physical_grid_is_rejected_before_reconstruction():
    valid = _normalized_grid(2, 2)
    invalid = PhysicalGrid(
        source=valid.source,
        x_boundaries=(-0.1, 0.5, 1.0),
        y_boundaries=valid.y_boundaries,
        cells=valid.cells,
        confidence=1.0,
        high_confidence=True,
    )
    assert "invalid_x_boundary" in validate_physical_grid(invalid)
    diagnostics: dict = {}
    result = reconstruct_page_rows(
        _geometry_payload(),
        "yandex_vision",
        physical_grid=invalid,
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert result == []
    assert diagnostics["selected_mode"] == "unsupported_schema"
    assert diagnostics["fallback_reason"] == "unsupported_table_schema"


def test_two_independent_similar_tables_are_not_merged():
    image = np.full((900, 1400), 255, dtype=np.uint8)
    _draw_table(image, (60, 60, 600, 660), rows=8, columns=5)
    _draw_table(image, (800, 80, 1340, 680), rows=8, columns=5)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.candidate_count == 2
    assert detection.grid is None
    assert detection.reasons == ("ambiguous_table_candidates",)
    assert detection.selected_candidate is None


def test_main_table_beats_smaller_title_block_candidate():
    image = np.full((900, 1400), 255, dtype=np.uint8)
    _draw_table(image, (60, 60, 1280, 600), rows=8, columns=5)
    _draw_table(image, (850, 680, 1300, 850), rows=3, columns=4)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.candidate_count == 2
    assert detection.high_confidence is True
    assert detection.selected_candidate == 0
    assert detection.grid is not None
    assert detection.grid.row_count == 8
    assert detection.grid.column_count == 5


def test_small_drawing_table_is_detected_but_falls_back_conservatively():
    image = np.full((900, 1400), 255, dtype=np.uint8)
    _draw_table(image, (520, 370, 740, 530), rows=5, columns=4)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.candidate_count == 1
    assert detection.grid is not None
    assert detection.high_confidence is False
    assert "table_area_too_small" in detection.reasons


@pytest.mark.parametrize("columns", [5, 12])
def test_detector_supports_non_nine_column_tables(columns):
    detection, _mask = RasterRuledTableGridDetector().detect_image(
        _ruled_image(rows=6, columns=columns)
    )
    assert detection.high_confidence is True
    assert detection.grid is not None
    assert detection.grid.column_count == columns


@pytest.mark.parametrize("axis", ["horizontal", "vertical"])
def test_broken_internal_boundary_is_not_high_confidence(axis):
    image = _ruled_image(rows=6, columns=4)
    if axis == "horizontal":
        cv2.rectangle(image, (280, 330), (720, 370), 255, -1)
    else:
        cv2.rectangle(image, (480, 150), (520, 550), 255, -1)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is False
    assert detection.grid is None or detection.grid.high_confidence is False
    assert detection.reasons != ()


@pytest.mark.parametrize(
    "axis,position,expected_reason",
    [
        ("horizontal", 163, "row_spacing_anomaly"),
        ("horizontal", 537, "row_spacing_anomaly"),
        ("vertical", 290, "column_spacing_anomaly"),
        ("vertical", 710, "column_spacing_anomaly"),
    ],
)
def test_missing_first_or_last_internal_rule_is_not_trusted(axis, position, expected_reason):
    """Edge gaps are integrity evidence, not permission to merge a row/column."""
    image = _ruled_image(rows=6, columns=4)
    if axis == "horizontal":
        cv2.line(image, (250, position), (750, position), 255, 12)
    else:
        cv2.line(image, (position, 170), (position, 530), 255, 12)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is False
    assert expected_reason in detection.reasons


def test_natural_edge_spacing_is_trusted_when_grid_evidence_is_clean():
    image = _draw_grid_with_boundaries(
        [80, 240, 400, 560, 720, 920],
        [70, 250, 380, 510, 630],
    )
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is True
    assert detection.reasons == ()


def test_edge_defect_uses_table_fallback_and_preserves_export_rows():
    image = _ruled_image(rows=6, columns=4)
    cv2.line(image, (250, 163), (750, 163), 255, 12)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.grid is not None
    headers = ["Наименование", "Тип, марка", "Ед. изм.", "Количество"]
    body_rows = [
        ["Насос-1", "Н-1", "шт.", "1"],
        ["Насос-2", "Н-2", "шт.", "2"],
        ["Насос-3", "Н-3", "шт.", "3"],
        ["Насос-4", "Н-4", "шт.", "4"],
    ]
    cells = [
        _schema_table_cell(text, row, column, len(headers))
        for row, values in enumerate([headers, *body_rows])
        for column, text in enumerate(values)
    ]
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        {
            "page": {"width": 1200, "height": 500},
            "textAnnotation": {
                "fullText": "Спецификация оборудования",
                "tables": [{"rowCount": 5, "columnCount": 4, "cells": cells}],
            },
        },
        "yandex_vision",
        physical_grid=detection.grid,
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert diagnostics["selected_mode"] == "table_fallback"
    assembled = SpecificationRowAssembler().build_page(1, rows)
    assert [row["name"] for row in assembled] == [item[0] for item in body_rows]
    status = page_status_from_diagnostics(1, diagnostics, row_count=len(rows))
    assert status.output_status == "REVIEW_REQUIRED"


def test_short_line_defect_is_bridged_without_changing_grid():
    image = _ruled_image(rows=6, columns=4)
    cv2.rectangle(image, (400, 345), (405, 355), 255, -1)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is True
    assert detection.grid is not None
    assert (detection.grid.row_count, detection.grid.column_count) == (6, 4)


def test_unsupported_geometry_header_uses_traceable_fallback():
    payload = _geometry_payload()
    payload["textAnnotation"]["blocks"][0]["lines"][0]["words"] = [
        _word("Кабель", 0, 1),
        _word("Сечение", 0, 2),
        _word("Марка", 0, 3),
    ]
    diagnostics: dict = {}
    result = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert result == []
    assert diagnostics["selected_mode"] == "unsupported_schema"
    assert diagnostics["fallback_reason"] == "unsupported_table_schema"
    assert diagnostics["geometry_source"] == "test_grid"


def test_geometry_mode_uses_grid_words_and_skips_incomplete_numbering_row():
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        _geometry_payload(),
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert diagnostics["selected_mode"] == "geometry_first"
    assert [row.values.get("name") for row in rows] == ["Установка", "Моноблок"]
    assert rows[0].values["quantity"] == "1"
    assert all(row.metadata["reconstruction_mode"] == "geometry_first" for row in rows)
    assert all("structural_disagreement" in row.metadata["review_reasons"] for row in rows)
    assert diagnostics["structural_evidence"]["column_count_conflict"] is True
    assert diagnostics["schema"]["status"] == SUPPORTED
    assert page_status_from_diagnostics(18, diagnostics, row_count=len(rows)).schema_status == SUPPORTED.upper()


def test_shadow_mode_returns_legacy_rows_and_records_diff():
    payload = _geometry_payload()
    legacy = reconstruct_page_rows(payload, "yandex_vision")
    diagnostics: dict = {}
    shadow = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="shadow",
        diagnostics=diagnostics,
    )
    assert [row.values for row in shadow] == [row.values for row in legacy]
    assert diagnostics["selected_mode"] == "table_shadow"
    assert diagnostics["shadow_diff"]["geometry_row_count"] == 2


def test_low_confidence_grid_uses_legacy_fallback():
    payload = _geometry_payload()
    legacy = reconstruct_page_rows(payload, "yandex_vision")
    diagnostics: dict = {}
    result = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9, confidence=0.4, high_confidence=False),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert [row.values for row in result] == [row.values for row in legacy]
    assert diagnostics["selected_mode"] == "unsupported_schema"
    assert diagnostics["fallback_reason"] == "unsupported_table_schema"


def test_high_grid_without_semantic_mapping_uses_legacy_fallback():
    payload = _geometry_payload()
    payload["textAnnotation"]["blocks"] = [{
        "lines": [{"words": [_word("Неизвестная строка", 2, 1)]}]
    }]
    legacy = reconstruct_page_rows(payload, "yandex_vision")
    diagnostics: dict = {}
    result = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 4),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert [row.values for row in result] == [row.values for row in legacy]
    assert diagnostics["selected_mode"] == "unsupported_schema"
    assert diagnostics["fallback_reason"] == "unsupported_table_schema"


def test_provider_accepts_future_non_raster_detector_contract(tmp_path: Path):
    expected = PhysicalGridDetection(
        grid=_normalized_grid(4, 5), source="vector_test"
    )

    class VectorDetector:
        def detect_page(self, pdf_path, page_number):
            assert pdf_path == tmp_path / "source.pdf"
            assert page_number == 7
            return expected

    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    provider = YandexVisionProvider(
        _Settings(),
        secrets,
        grid_detector=VectorDetector(),
        reconstruction_mode="geometry",
    )
    detection, raster = provider._detect_grid_page(
        tmp_path / "source.pdf", 7, _geometry_payload(), []
    )
    assert detection is expected
    assert raster is None


def test_exact_crop_removes_rules_and_preserves_glyph_at_two_x():
    detector = RasterRuledTableGridDetector()
    image = _ruled_image(rows=2, columns=4)
    detection, line_mask = detector.detect_image(image)
    assert detection.grid is not None
    cell = detection.grid.cell(1, 2)
    assert cell is not None
    left = int(cell.bounds[0] * image.shape[1])
    top = int(cell.bounds[1] * image.shape[0])
    cv2.putText(image, "1", (left + 80, top + 100), cv2.FONT_HERSHEY_SIMPLEX, 2, 0, 4)
    raster = RasterGridPage(detection, image, line_mask)
    crop = prepare_exact_cell_crop(raster, cell, scale=2)
    assert crop.shape[0] > (cell.bounds[3] - cell.bounds[1]) * image.shape[0] * 2
    assert crop_has_glyph(crop) is True


def test_exact_candidate_never_fills_primary_value():
    row = OcrRow(
        source_row=1,
        values={"name": "Клапан", "unit": "шт."},
        confidences={},
        sources={"name": "yandex_vision", "unit": "yandex_vision"},
        bbox={},
        metadata={},
    )
    assert attach_exact_cell_candidate(
        row, "quantity", "1", bbox={"x": 0.6, "y": 0.2, "width": 0.1, "height": 0.1}
    )
    assert row.values.get("quantity", "") == ""
    candidate = row.metadata["value_candidates"]["quantity"]
    assert candidate["value_candidate"] == "1"
    assert candidate["auto_trusted"] is False


class _Settings:
    def __init__(self):
        self.settings = SimpleNamespace(yandex=SimpleNamespace(
            folder_id="folder-test",
            vision_model="table",
            vision_base_url="https://ocr.test",
            language_codes=["ru", "en"],
            chunk_pages=1,
            request_timeout_s=5.0,
            operation_timeout_s=30.0,
        ))


class _ExactCellHttp:
    def __init__(self, values: list[str]):
        self.values = list(values)
        self.calls: list[dict] = []

    def request(self, method, url, *, body=None, headers=None, timeout=30.0):
        payload = json.loads(body)
        assert payload["model"] == "page"
        assert payload["mimeType"] == "image/png"
        assert base64.b64decode(payload["content"]).startswith(b"\x89PNG")
        self.calls.append({"method": method, "url": url, "body": body})
        value = self.values.pop(0)
        response = {
            "result": {
                "textAnnotation": {
                    "width": 200,
                    "height": 200,
                    "fullText": value,
                }
            }
        }
        return _HttpResponse(200, json.dumps(response).encode("utf-8"))


def _benchmark_raster(values: list[str]) -> tuple[RasterGridPage, list[OcrRow]]:
    rows = len(values)
    columns = 9
    grid = _normalized_grid(rows, columns)
    height, width = rows * 100, columns * 100
    image = np.full((height, width), 255, dtype=np.uint8)
    line_mask = np.zeros_like(image)
    ocr_rows = []
    for row_index, value in enumerate(values):
        cell = grid.cell(row_index, 6)
        assert cell is not None
        left = int(cell.bounds[0] * width)
        top = int(cell.bounds[1] * height)
        cv2.putText(
            image,
            value,
            (left + 35, top + 68),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            0,
            3,
        )
        ocr_rows.append(OcrRow(
            source_row=row_index + 1,
            values={"name": f"item-{row_index}", "unit": "шт."},
            confidences={},
            sources={"name": "yandex_vision", "unit": "yandex_vision"},
            bbox={},
            metadata={
                "schema_assessment": {"status": SUPPORTED},
                "physical_grid_cells": {
                    "quantity": {
                        "row_index": row_index,
                        "column_index": 6,
                        "bbox": cell.as_bbox(),
                    },
                    "mass": {
                        "row_index": row_index,
                        "column_index": 7,
                        "bbox": grid.cell(row_index, 7).as_bbox(),
                    },
                }
            },
        ))
    detection = PhysicalGridDetection(grid=grid, source="test_grid")
    return RasterGridPage(detection, image, line_mask), ocr_rows


def test_benchmark_critical_cells_produce_29_of_29_review_candidates():
    benchmark = {
        18: ["1"],
        30: ["1"] * 8,
        37: ["1", "1", "1", "4", "1", "1", "1"],
        47: ["1"] * 6,
        50: ["1"] * 5,
        58: ["4", "1"],
    }
    expected = [value for values in benchmark.values() for value in values]
    http = _ExactCellHttp(expected)
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    provider = YandexVisionProvider(
        _Settings(),
        secrets,
        cache_dir=None,
        http=http,
        sleep_fn=lambda _seconds: None,
        reconstruction_mode="geometry",
    )
    config = provider._yandex_settings()
    stats = {
        "exact_cell_checked": 0,
        "exact_cell_requests": 0,
        "exact_cell_candidates": 0,
    }
    all_rows: list[OcrRow] = []
    for page, values in benchmark.items():
        raster, rows = _benchmark_raster(values)
        provider._exact_cell_verify_page(
            page,
            raster,
            rows,
            config,
            "test-key",
            None,
            [],
            stats,
        )
        all_rows.extend(rows)
    assert len(http.calls) == 29
    assert stats == {
        "exact_cell_checked": 29,
        "exact_cell_requests": 29,
        "exact_cell_candidates": 29,
    }
    assert [
        row.metadata["value_candidates"]["quantity"]["value_candidate"]
        for row in all_rows
    ] == expected
    assert all(row.values.get("quantity", "") == "" for row in all_rows)


def test_exact_cell_verification_does_not_swallow_cancel():
    raster, rows = _benchmark_raster(["1"])
    http = _ExactCellHttp(["1"])
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    provider = YandexVisionProvider(
        _Settings(),
        secrets,
        cache_dir=None,
        http=http,
        sleep_fn=lambda _seconds: None,
        reconstruction_mode="geometry",
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OcrProviderError, match="отменено"):
        provider._exact_cell_verify_page(
            1,
            raster,
            rows,
            provider._yandex_settings(),
            "test-key",
            cancel,
            [],
            {
                "exact_cell_checked": 0,
                "exact_cell_requests": 0,
                "exact_cell_candidates": 0,
            },
        )
    assert http.calls == []


def test_exact_cell_cache_hit_avoids_second_http_request(tmp_path: Path):
    raster, rows = _benchmark_raster(["1"])
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    http = _ExactCellHttp(["1"])
    provider = YandexVisionProvider(
        _Settings(),
        secrets,
        cache_dir=tmp_path / "ocr_cache",
        http=http,
        sleep_fn=lambda _seconds: None,
        reconstruction_mode="geometry",
    )
    stats = {
        "exact_cell_checked": 0,
        "exact_cell_requests": 0,
        "exact_cell_candidates": 0,
    }
    provider._exact_cell_verify_page(
        1,
        raster,
        rows,
        provider._yandex_settings(),
        "test-key",
        None,
        [],
        stats,
    )
    assert len(http.calls) == 1
    provider._exact_cell_verify_page(
        1,
        raster,
        rows,
        provider._yandex_settings(),
        "test-key",
        None,
        [],
        stats,
    )
    assert len(http.calls) == 1
    assert stats["exact_cell_requests"] == 1


def _schema_table_cell(text: str, row: int, column: int, columns: int) -> dict:
    width = 3000 / columns
    left = column * width
    top = row * 100
    return {
        "text": text,
        "rowIndex": row,
        "columnIndex": column,
        "rowSpan": 1,
        "columnSpan": 1,
        "boundingBox": {"vertices": [
            {"x": left, "y": top},
            {"x": left + width, "y": top},
            {"x": left + width, "y": top + 90},
            {"x": left, "y": top + 90},
        ]},
    }


def test_schema_recognizer_has_explicit_supported_ambiguous_unsupported_states():
    recognizer = SupportedSpecificationSchemaRecognizer()
    supported = recognizer.assess(
        column_count=8,
        mapping={
            0: ("position",), 1: ("name",), 2: ("type_mark",),
            3: ("code",), 4: ("manufacturer",), 5: ("unit",),
            6: ("quantity",), 7: ("mass", "note"),
        },
        header_rows={0},
    )
    ambiguous = recognizer.assess(
        column_count=8,
        mapping={
            0: ("position",), 1: ("name",), 2: ("type_mark",),
            3: ("code",), 4: ("manufacturer",), 5: ("unit", "quantity"),
        },
        header_rows={0},
    )
    unsupported = recognizer.assess(
        column_count=30,
        mapping={0: ("name",), 1: ("type_mark",), 2: ("code",), 3: ("unit",), 4: ("quantity",)},
        header_rows={0},
    )
    assert supported.status == SUPPORTED
    assert ambiguous.status == AMBIGUOUS
    assert unsupported.status == UNSUPPORTED
    assert "semantic_coverage_too_low" in unsupported.reasons


def test_unsupported_wide_journal_is_not_projected_through_base_columns():
    columns = 30
    headers = ["Наименование кабеля", "Марка", "Материал", "Ед. изм.", "Количество"]
    cells = [
        _schema_table_cell(text, 0, column, columns)
        for column, text in enumerate(headers)
    ]
    cells += [
        _schema_table_cell("Кабель силовой", 1, 0, columns),
        _schema_table_cell("ВВГнг", 1, 1, columns),
        _schema_table_cell("Медь", 1, 2, columns),
        _schema_table_cell("м", 1, 3, columns),
        _schema_table_cell("100", 1, 4, columns),
    ]
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        {
            "page": {"width": 3000, "height": 300},
            "textAnnotation": {"tables": [{
                "rowCount": 2, "columnCount": columns, "cells": cells,
            }]},
        },
        "yandex_vision",
        diagnostics=diagnostics,
    )
    assert rows == []
    assert diagnostics["schema"]["status"] in {UNSUPPORTED, AMBIGUOUS}
    assert diagnostics["selected_mode"] == "unsupported_schema"


def test_header_scan_stops_before_body_word_that_contains_an_anchor():
    headers = [
        "Позиция", "Наименование", "Тип, марка", "Код", "Изготовитель",
        "Ед. изм.", "Количество", "Масса", "Примечание",
    ]
    body = ["1", "Изделие специальное", "ВВГ", "М-1", "Завод", "шт.", "2", "", ""]
    cells = [
        _schema_table_cell(text, row, column, 9)
        for row, values in enumerate((headers, body))
        for column, text in enumerate(values)
    ]
    rows = reconstruct_page_rows(
        {
            "page": {"width": 3000, "height": 300},
            "textAnnotation": {"tables": [{
                "rowCount": 2, "columnCount": 9, "cells": cells,
            }]},
        },
        "yandex_vision",
    )
    assert len(rows) == 1
    assert rows[0].values["name"] == "Изделие специальное"
    assert rows[0].metadata["source_row_index"] == 1


def test_first_physical_body_rows_are_not_consumed_as_header_rows():
    headers = ["Позиция", "Наименование", "Тип, марка", "Ед. изм.", "Количество"]
    body_rows = [
        ["1", "Насос-1", "Н-1", "шт.", "1"],
        ["2", "Насос-2", "Н-2", "шт.", "2"],
        ["3", "Насос-3", "Н-3", "шт.", "3"],
    ]
    cells = [
        _schema_table_cell(text, row, column, len(headers))
        for row, values in enumerate([headers, *body_rows])
        for column, text in enumerate(values)
    ]
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        {
            "page": {"width": 1500, "height": 400},
            "textAnnotation": {
                "fullText": "Спецификация оборудования",
                "tables": [{
                    "rowCount": 4,
                    "columnCount": len(headers),
                    "cells": cells,
                }],
            },
        },
        "yandex_vision",
        diagnostics=diagnostics,
    )
    assert [row.values["name"] for row in rows] == ["Насос-1", "Насос-2", "Насос-3"]
    assert [row.values["quantity"] for row in rows] == ["1", "2", "3"]
    assert [row.metadata["source_row_index"] for row in rows] == [1, 2, 3]
    assert len(SpecificationRowAssembler().build_page(1, rows)) == 3
    assert page_status_from_diagnostics(1, diagnostics, row_count=len(rows)).output_status == "REVIEW_REQUIRED"


@pytest.mark.parametrize("product_word", ["Изделие", "Оборудование", "Материал"])
def test_product_words_in_first_body_row_do_not_make_it_a_header(product_word):
    headers = ["Наименование", "Тип, марка", "Ед. изм.", "Количество"]
    values = [product_word, "Т-1", "шт.", "1"]
    cells = [
        _schema_table_cell(text, row, column, len(headers))
        for row, row_values in enumerate((headers, values))
        for column, text in enumerate(row_values)
    ]
    rows = reconstruct_page_rows(
        {
            "page": {"width": 1200, "height": 200},
            "textAnnotation": {
                "fullText": "Спецификация оборудования",
                "tables": [{"rowCount": 2, "columnCount": 4, "cells": cells}],
            },
        },
        "yandex_vision",
    )
    assert len(rows) == 1
    assert rows[0].values["name"] == product_word


def test_combined_critical_header_is_ambiguous_and_not_trusted():
    headers = [
        "Позиция", "Наименование", "Тип, марка", "Код", "Изготовитель",
        "Ед. изм. Количество", "Масса", "Примечание",
    ]
    body = ["1", "Клапан", "V-1", "C-1", "Завод", "шт. 2", "1.5", ""]
    cells = [
        _schema_table_cell(text, row, column, 8)
        for row, values in enumerate((headers, body))
        for column, text in enumerate(values)
    ]
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        {
            "page": {"width": 2400, "height": 300},
            "textAnnotation": {"tables": [{
                "rowCount": 2, "columnCount": 8, "cells": cells,
            }]},
        },
        "yandex_vision",
        diagnostics=diagnostics,
    )
    assert rows
    assert diagnostics["schema"]["status"] == AMBIGUOUS
    assert "ambiguous_table_schema" in rows[0].metadata["review_reasons"]
    assert "critical_value_missing" not in rows[0].metadata["review_reasons"]
    assembled = SpecificationRowAssembler().build_page(1, rows)
    assert "ambiguous_table_schema" in critical_blockers_for_row(assembled[0])


def test_grid_detector_rejects_missing_outer_rule_and_reports_integrity():
    image = _ruled_image(rows=6, columns=4)
    cv2.rectangle(image, (77, 70), (84, 630), 255, -1)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is False
    assert "missing_outer_left_boundary" in detection.reasons
    assert detection.grid is not None
    assert detection.grid.metrics["layout_trusted"] is False


def test_grid_candidate_diagnostics_include_scores_for_all_viable_candidates():
    image = np.full((900, 1400), 255, dtype=np.uint8)
    _draw_table(image, (60, 60, 600, 660), rows=8, columns=5)
    _draw_table(image, (800, 80, 1340, 680), rows=8, columns=5)
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert len(detection.candidate_diagnostics) == 2
    assert all("candidate_score" in item for item in detection.candidate_diagnostics)
    assert all("spacing_score" in item for item in detection.candidate_diagnostics)
    assert all("area_score" in item for item in detection.candidate_diagnostics)
    assert all(item["rejected_reason"] == "ambiguous_table_candidates" for item in detection.candidate_diagnostics)


def test_weak_critical_word_assignment_is_empty_reviewable_not_trusted():
    payload = _geometry_payload()
    quantity_word = next(
        word
        for word in payload["textAnnotation"]["blocks"][0]["lines"][0]["words"]
        if word["text"] == "1"
    )
    quantity_word["boundingBox"] = {
        "vertices": [
            {"x": 630, "y": 512}, {"x": 730, "y": 512},
            {"x": 730, "y": 540}, {"x": 630, "y": 540},
        ]
    }
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    row = next(row for row in rows if row.values.get("name") == "Установка")
    assert row.values.get("quantity", "") == ""
    assert row.metadata["weak_critical_assignment"] is True
    assert "word_assignment_ambiguity" in row.metadata["review_reasons"]
    assert diagnostics["weak_critical_assignment_count"] == 1


def test_all_words_in_an_ambiguous_body_row_force_legacy_or_empty_fallback():
    payload = _geometry_payload()
    for word in payload["textAnnotation"]["blocks"][0]["lines"][0]["words"]:
        vertices = word["boundingBox"]["vertices"]
        center_y = sum(point["y"] for point in vertices) / len(vertices)
        if 500 < center_y < 750:
            word["boundingBox"] = {
                "vertices": [
                    {"x": 166, "y": center_y - 14},
                    {"x": 277, "y": center_y - 14},
                    {"x": 277, "y": center_y + 14},
                    {"x": 166, "y": center_y + 14},
                ]
            }
    diagnostics: dict = {}
    rows = rows_from_physical_grid(
        payload,
        "yandex_vision",
        _normalized_grid(4, 9),
        diagnostics=diagnostics,
    )
    assert rows is None
    assert diagnostics["assignment_safety"] == "fallback_required"
    assert diagnostics["assignment_fallback_reason"] == "ambiguous_or_unassigned_row"


def test_structural_column_boundary_disagreement_is_diagnostic_and_blocks_export():
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        _geometry_payload(),
        "yandex_vision",
        physical_grid=_normalized_grid(4, 9),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert rows
    evidence = diagnostics["structural_evidence"]
    assert evidence["material_disagreement"] is True
    assert evidence["column_boundary_conflicts"]
    assert all("structural_disagreement" in row.metadata["review_reasons"] for row in rows)
    assembled = SpecificationRowAssembler().build_page(1, rows)
    assert any(
        "structural_disagreement" in critical_blockers_for_row(row)
        for row in assembled
    )


def test_exact_cell_verification_skips_structurally_ambiguous_rows():
    raster, rows = _benchmark_raster(["1"])
    rows[0].metadata["structural_ambiguity"] = True
    http = _ExactCellHttp(["1"])
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    provider = YandexVisionProvider(
        _Settings(), secrets, cache_dir=None, http=http,
        sleep_fn=lambda _seconds: None, reconstruction_mode="geometry",
    )
    provider._exact_cell_verify_page(
        18, raster, rows, provider._yandex_settings(), "test-key", None, [],
        {"exact_cell_checked": 0, "exact_cell_requests": 0, "exact_cell_candidates": 0},
    )
    assert http.calls == []


@pytest.mark.parametrize(
    "schema_assessment",
    [None, {}, {"status": "unknown"}, {"status": AMBIGUOUS}, {"status": UNSUPPORTED}],
)
def test_exact_cell_verification_is_fail_closed_without_supported_schema(schema_assessment):
    raster, rows = _benchmark_raster(["1"])
    rows[0].metadata.pop("schema_assessment", None)
    if schema_assessment is not None:
        rows[0].metadata["schema_assessment"] = schema_assessment
    http = _ExactCellHttp(["1"])
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    provider = YandexVisionProvider(
        _Settings(), secrets, cache_dir=None, http=http,
        sleep_fn=lambda _seconds: None, reconstruction_mode="geometry",
    )
    stats = {"exact_cell_checked": 0, "exact_cell_requests": 0, "exact_cell_candidates": 0}
    provider._exact_cell_verify_page(
        18, raster, rows, provider._yandex_settings(), "test-key", None, [], stats,
    )
    assert http.calls == []
    assert stats == {"exact_cell_checked": 0, "exact_cell_requests": 0, "exact_cell_candidates": 0}


def test_product_evidence_wins_over_generic_section_name():
    assembler = SpecificationRowAssembler()
    section = assembler.build_page(18, [OcrRow(
        1, {"name": "Оборудование"}, {}, {}, {}, {"provider": "yandex_vision"},
    )])[0]
    product = assembler.build_page(18, [OcrRow(
        1, {"name": "Оборудование вентиляции", "code": "42"}, {}, {}, {},
        {"provider": "yandex_vision"},
    )])[0]
    assert section["row_type"] == "section"
    assert product["row_type"] == "item"


@pytest.mark.parametrize(
    "mutate, expected",
    [
        ("duplicate_boundary", "non_monotonic_x_boundaries"),
        ("confidence", "invalid_grid_confidence"),
        ("cell_bbox", "cell_bbox_not_aligned_to_boundaries"),
        ("high_reason", "high_confidence_with_fatal_reasons"),
    ],
)
def test_physical_grid_validator_rejects_malformed_or_untrusted_contract(mutate, expected):
    valid = _normalized_grid(2, 2)
    kwargs = {
        "source": valid.source,
        "x_boundaries": valid.x_boundaries,
        "y_boundaries": valid.y_boundaries,
        "cells": valid.cells,
        "confidence": valid.confidence,
        "high_confidence": valid.high_confidence,
        "reasons": valid.reasons,
    }
    if mutate == "duplicate_boundary":
        kwargs["x_boundaries"] = (0.0, 0.5, 0.5)
    elif mutate == "confidence":
        kwargs["confidence"] = 1.1
    elif mutate == "cell_bbox":
        cells = list(valid.cells)
        cells[0] = PhysicalGridCell(0, 0, (0.0, 0.0, 0.6, 0.5))
        kwargs["cells"] = tuple(cells)
    else:
        kwargs["reasons"] = ("weak_intersections",)
    malformed = PhysicalGrid(**kwargs)
    assert expected in validate_physical_grid(malformed)
