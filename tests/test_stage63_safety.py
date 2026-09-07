from __future__ import annotations

from dataclasses import replace

import pytest

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.page_contract import (
    LAYOUT_AMBIGUOUS,
    OUTPUT_NO_SPEC,
    PageExtractionStatus,
    page_status_from_diagnostics,
)
from averon_import.services.ocr.physical_grid import (
    PhysicalGridCell,
    validate_physical_grid,
)
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.ocr.schema_recognizer import (
    SUPPORTED,
    UNSUPPORTED,
    SupportedSpecificationSchemaRecognizer,
)
from averon_import.services.ocr.base import OcrRow
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.review_policy import critical_blockers_for_row


def test_malformed_grid_types_are_controlled_validation_failures():
    grid = _valid_grid()
    malformed = replace(
        grid,
        cells=(PhysicalGridCell("0", 0, grid.cells[0].bounds),) + grid.cells[1:],
    )
    errors = validate_physical_grid(malformed)
    assert "invalid_grid_cell_index" in errors

    malformed_bbox = replace(
        grid,
        cells=(PhysicalGridCell(0, 0, ("bad", 0.0, 0.2, 0.2)),) + grid.cells[1:],
    )
    assert "invalid_cell_bbox" in validate_physical_grid(malformed_bbox)


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("x_boundaries", None, "invalid_x_boundaries_container"),
        ("y_boundaries", None, "invalid_y_boundaries_container"),
        ("cells", None, "invalid_cells_container"),
        ("x_boundaries", {0: 0.0}, "invalid_x_boundaries_container"),
        ("cells", "not-a-cell-list", "invalid_cells_container"),
    ],
)
def test_physical_grid_validator_handles_none_and_wrong_containers(field, value, expected):
    errors = validate_physical_grid(replace(_valid_grid(), **{field: value}))
    assert expected in errors


def _valid_grid():
    from averon_import.services.ocr.physical_grid import PhysicalGrid

    xs = (0.0, 0.5, 1.0)
    ys = (0.0, 0.5, 1.0)
    return PhysicalGrid(
        source="test",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=tuple(
            PhysicalGridCell(row, column, (xs[column], ys[row], xs[column + 1], ys[row + 1]))
            for row in range(2)
            for column in range(2)
        ),
        confidence=1.0,
        high_confidence=True,
    )


def _table_cell(text: str, row: int, column: int, columns: int = 4) -> dict:
    width = 1200 / columns
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


def test_schema_negative_family_is_not_supported():
    assessment = SupportedSpecificationSchemaRecognizer().assess(
        column_count=5,
        mapping={0: ("position",), 1: ("name",), 2: ("type_mark",), 3: ("unit",), 4: ("quantity",)},
        header_rows={0},
        header_text="Позиция Наименование Марка Ед. изм. Количество",
        context_text="Кабельный журнал",
    )
    assert assessment.status == UNSUPPORTED
    assert any(reason.startswith("negative_document_family:") for reason in assessment.reasons)


def test_generic_four_column_equipment_list_is_not_supported_by_one_word():
    recognizer = SupportedSpecificationSchemaRecognizer()
    assessment = recognizer.assess(
        column_count=4,
        mapping={0: ("name",), 1: ("type_mark",), 2: ("unit",), 3: ("quantity",)},
        header_rows={0},
        header_text="Наименование Тип, марка Ед. изм. Количество",
        context_text="Перечень оборудования",
    )
    assert assessment.status != SUPPORTED
    assert "family_evidence_missing" in assessment.reasons


def test_four_column_specification_variant_remains_supported():
    assessment = SupportedSpecificationSchemaRecognizer().assess(
        column_count=4,
        mapping={0: ("name",), 1: ("type_mark",), 2: ("unit",), 3: ("quantity",)},
        header_rows={0},
        header_text="Наименование Тип, марка Ед. изм. Количество",
        context_text="Спецификация оборудования",
    )
    assert assessment.status == SUPPORTED


def test_generic_four_column_equipment_list_blocks_safe_export(tmp_path):
    headers = ["Наименование", "Тип, марка", "Ед. изм.", "Количество"]
    values = ["Насос", "Н-1", "шт.", "1"]
    cells = [
        _table_cell(text, row, column)
        for row, row_values in enumerate((headers, values))
        for column, text in enumerate(row_values)
    ]
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        {
            "page": {"width": 1200, "height": 200},
            "textAnnotation": {
                "fullText": "Перечень оборудования",
                "tables": [{
                    "rowCount": 2,
                    "columnCount": 4,
                    "cells": cells,
                }],
            },
        },
        "yandex_vision",
        diagnostics=diagnostics,
    )
    assert diagnostics["schema"]["status"] != SUPPORTED
    status = page_status_from_diagnostics(1, diagnostics, row_count=len(rows))
    assert status.output_status == "REVIEW_REQUIRED"
    assert "structural_schema_ambiguous" in status.blockers
    assembled = SpecificationRowAssembler().build_page(1, rows)
    with pytest.raises(ValueError, match="Экспорт заблокирован"):
        ExcelExportService().export(
            assembled,
            ["name", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses={"1": status.as_dict()},
        )


def test_partial_product_row_is_reviewable_not_skip():
    row = SpecificationRowAssembler().build_page(1, [OcrRow(
        1,
        {"unit": "шт.", "quantity": "2"},
        {},
        {},
        {},
        {"provider": "yandex_vision"},
    )])[0]
    assert row["row_type"] == "item_candidate"
    assert row["status"] == "unrecognized"
    assert "physical_row_unresolved" in critical_blockers_for_row(row)


def test_page_contract_survives_empty_rows_and_blocks_export(tmp_path):
    status = page_status_from_diagnostics(
        30,
        {
            "selected_mode": "no_spec_output",
            "fallback_reason": "physical_grid_low_confidence",
            "geometry_source": "raster_cv",
            "schema_status": "unknown",
        },
        row_count=0,
    )
    assert status.layout_status == LAYOUT_AMBIGUOUS
    assert status.output_status == OUTPUT_NO_SPEC
    assert "schema_unknown" in status.blockers
    with pytest.raises(ValueError, match="страница 30"):
        ExcelExportService().export(
            [], ["name"], tmp_path / "blocked.xlsx", page_statuses={"30": status.as_dict()}
        )


def test_supported_page_contract_is_usable():
    status = page_status_from_diagnostics(
        18,
        {
            "selected_mode": "geometry_first",
            "geometry_grid": {"high_confidence": True},
            "schema": {"status": SUPPORTED},
        },
        row_count=2,
    )
    assert status.output_status == "USABLE"
    assert status.blockers == []
