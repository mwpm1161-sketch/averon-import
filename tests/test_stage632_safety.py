from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.base import OcrResult, OcrRow, PageOcrResult
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.physical_grid import PhysicalGrid, PhysicalGridCell
from averon_import.services.ocr.raster_grid import (
    RasterGridPage,
    RasterRuledTableGridDetector,
    physical_row_raster_witness,
)
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.recognition import RecognitionService
from averon_import.services.pdf_service import PdfService
from averon_import.services.row_assembler import SpecificationRowAssembler


def _status(page: int, output: str, *, blockers=None, reason: str | None = None) -> dict:
    diagnostics = {"fallback_reason": reason} if reason else {}
    return {
        "page": page,
        "layout_status": "TRUSTED" if output == "USABLE" else "AMBIGUOUS",
        "schema_status": "SUPPORTED",
        "output_status": output,
        "blockers": list(blockers or []),
        "diagnostics": diagnostics,
    }


def _item(name: str = "Насос") -> dict:
    return {
        "name": name,
        "unit": "шт.",
        "quantity": "1",
        "row_type": "item",
        "status": "verified",
        "selected": True,
    }


@pytest.mark.parametrize(
    "output, rows, match",
    [
        ("REVIEW_REQUIRED", [_item()], "страница 30.*REVIEW_REQUIRED"),
        ("NO_SPEC_OUTPUT", [], "страница 30.*NO_SPEC_OUTPUT"),
    ],
)
def test_page_output_status_always_blocks_normal_export(tmp_path, output, rows, match):
    with pytest.raises(ValueError, match=match):
        ExcelExportService().export(
            rows,
            ["name", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses={"30": _status(30, output)},
        )


def test_usable_page_allows_clean_export(tmp_path):
    target = tmp_path / "usable.xlsx"
    ExcelExportService().export(
        [_item()],
        ["name", "unit", "quantity"],
        target,
        page_statuses={"30": _status(30, "USABLE")},
    )
    assert target.exists()


def test_one_unsafe_page_blocks_multipage_export(tmp_path):
    with pytest.raises(ValueError, match="страница 2.*NO_SPEC_OUTPUT"):
        ExcelExportService().export(
            [_item("one"), _item("three")],
            ["name", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses={
                "1": _status(1, "USABLE"),
                "2": _status(2, "NO_SPEC_OUTPUT"),
                "3": _status(3, "USABLE"),
            },
        )


def test_only_exportable_filters_rows_but_never_disables_safety(tmp_path):
    rows = [_item(), {"name": "Служебная строка", "row_type": "note", "selected": True}]
    unsafe = {"30": _status(30, "REVIEW_REQUIRED", blockers=["physical_row_loss_suspected"])}
    with pytest.raises(ValueError, match="страница 30"):
        ExcelExportService().export(
            rows,
            ["name", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            only_exportable=False,
            page_statuses=unsafe,
        )

    target = tmp_path / "clean.xlsx"
    ExcelExportService().export(
        rows,
        ["name", "unit", "quantity"],
        target,
        only_exportable=False,
        page_statuses={"30": _status(30, "USABLE")},
    )
    assert target.exists()


def test_export_with_supplied_statuses_rejects_missing_processed_page(tmp_path):
    with pytest.raises(ValueError, match="страница 2.*missing_page_status"):
        ExcelExportService().export(
            [_item("one"), {**_item("two"), "page": 2}],
            ["name", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses={"1": _status(1, "USABLE")},
        )


def _normalized_grid(rows: int, columns: int) -> PhysicalGrid:
    xs = tuple(index / columns for index in range(columns + 1))
    ys = tuple(index / rows for index in range(rows + 1))
    return PhysicalGrid(
        source="test_grid",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=tuple(
            PhysicalGridCell(
                row,
                column,
                (xs[column], ys[row], xs[column + 1], ys[row + 1]),
            )
            for row in range(rows)
            for column in range(columns)
        ),
        confidence=1.0,
        high_confidence=True,
    )


def _word(text: str, row: int, column: int, *, width: int = 1000) -> dict:
    left = column * width / 4 + 8
    top = row * 100 + 12
    right = min((column + 1) * width / 4 - 8, left + max(18, len(text) * 8))
    return {
        "text": text,
        "boundingBox": {"vertices": [
            {"x": left, "y": top}, {"x": right, "y": top},
            {"x": right, "y": top + 28}, {"x": left, "y": top + 28},
        ]},
    }


def _table_cell(text: str, row: int, column: int, *, width: int = 1000) -> dict:
    left = column * width / 4
    top = row * 100
    right = (column + 1) * width / 4
    return {
        "text": text,
        "rowIndex": row,
        "columnIndex": column,
        "rowSpan": 1,
        "columnSpan": 1,
        "boundingBox": {"vertices": [
            {"x": left, "y": top}, {"x": right, "y": top},
            {"x": right, "y": top + 90}, {"x": left, "y": top + 90},
        ]},
    }


def _partial_assignment_payload(field: str | None = None) -> dict:
    headers = ["Позиция", "Наименование", "Ед. изм.", "Количество"]
    values = ["1", "Насос", "шт.", "1"]
    words = [
        _word(text, row, column)
        for row, row_values in enumerate((headers, values))
        for column, text in enumerate(row_values)
    ]
    if field is not None:
        column = {"position": 0, "name": 1, "unit": 2, "quantity": 3}[field]
        damaged = next(word for word in words if word["text"] == values[column])
        damaged["boundingBox"] = {"vertices": [
            {"x": 100, "y": 112}, {"x": 1400, "y": 112},
            {"x": 1400, "y": 140}, {"x": 100, "y": 140},
        ]}
    cells = [
        _table_cell(text, row, column)
        for row, row_values in enumerate((headers, values))
        for column, text in enumerate(row_values)
    ]
    return {
        "page": {"width": 1000, "height": 200},
        "textAnnotation": {
            "width": 1000,
            "height": 200,
            "fullText": "Спецификация оборудования",
            "blocks": [{"lines": [{"words": words}]}],
            "tables": [{"rowCount": 2, "columnCount": 4, "cells": cells}],
        },
    }


@pytest.mark.parametrize("field", ["quantity", "name", "unit"])
def test_unassigned_word_inside_body_row_never_becomes_clean_geometry(field):
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        _partial_assignment_payload(field),
        "yandex_vision",
        physical_grid=_normalized_grid(2, 4),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert diagnostics["assignment_safety"] == "fallback_required"
    assert diagnostics["unassigned_body_rows"] == [1]
    assert diagnostics["selected_mode"] == "table_fallback"
    assert page_status_from_diagnostics(1, diagnostics, row_count=len(rows)).output_status == "REVIEW_REQUIRED"


def test_outside_grid_stamp_does_not_poison_body_assignment():
    payload = _partial_assignment_payload()
    payload["textAnnotation"]["blocks"][0]["lines"][0]["words"].append(
        _word("STAMP", 3, 5)
    )
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        payload,
        "yandex_vision",
        physical_grid=_normalized_grid(2, 4),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert diagnostics["selected_mode"] == "geometry_first"
    assert diagnostics["assignment_safety"] == "geometry_usable"
    assert diagnostics["outside_grid_word_count"] >= 1
    assert diagnostics.get("unassigned_body_rows", []) == []
    assert rows


def _tail_partial_ocr_payload(field: str) -> dict:
    headers = ["Позиция", "Наименование", "Тип, марка", "Ед. изм.", "Количество"]
    first = ["1", "Насос-1", "N1", "шт.", "1"]
    second = ["", "", "", "", ""]
    second[{"quantity": 4, "unit": 3, "type_mark": 2}[field]] = {
        "quantity": "2",
        "unit": "шт.",
        "type_mark": "N2",
    }[field]
    cells = [
        {
            "text": text,
            "rowIndex": row,
            "columnIndex": column,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": {"vertices": [
                {"x": column * 200, "y": row * 100},
                {"x": (column + 1) * 200, "y": row * 100},
                {"x": (column + 1) * 200, "y": row * 100 + 90},
                {"x": column * 200, "y": row * 100 + 90},
            ]},
        }
        for row, row_values in enumerate((headers, first, second))
        for column, text in enumerate(row_values)
    ]
    words = []
    for row, row_values in enumerate((headers, first, second)):
        for column, text in enumerate(row_values):
            if not text:
                continue
            left = column * 200 + 8
            top = row * 100 + 12
            words.append({
                "text": text,
                "boundingBox": {"vertices": [
                    {"x": left, "y": top}, {"x": left + 80, "y": top},
                    {"x": left + 80, "y": top + 28}, {"x": left, "y": top + 28},
                ]},
            })
    return {
        "page": {"width": 1000, "height": 300},
        "textAnnotation": {
            "fullText": "Спецификация оборудования",
            "blocks": [{"lines": [{"words": words}]}],
            "tables": [{"rowCount": 3, "columnCount": 5, "cells": cells}],
        },
    }


@pytest.mark.parametrize("field", ["quantity", "unit", "type_mark"])
def test_partially_ocrd_final_item_row_is_not_dropped_as_service_tail(tmp_path, field):
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        _tail_partial_ocr_payload(field),
        "yandex_vision",
        physical_grid=_normalized_grid(3, 5),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    partial = [row for row in rows if row.metadata.get("source_row_index") == 2]
    assert len(partial) == 1
    assert partial[0].values[field]
    assert diagnostics["selected_mode"] == "geometry_first"
    assert not any(
        event.get("source_row_index") == 2
        and event.get("drop_reason") == "trailing_service_block"
    for event in diagnostics.get("events", [])
    )
    status = page_status_from_diagnostics(1, diagnostics, row_count=len(rows))
    assert status.output_status == "USABLE"
    assembled = SpecificationRowAssembler().build_page(1, rows)
    with pytest.raises(ValueError, match="Не проверено"):
        ExcelExportService().export(
            assembled,
            ["name", "type_mark", "unit", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses={"1": status.as_dict()},
        )


def _structured_ocr_row(values: dict[str, str]) -> OcrRow:
    return OcrRow(
        source_row=1,
        values=values,
        confidences={},
        sources={},
        bbox={},
        metadata={
            "provider": "yandex_vision",
            "structured_table": True,
            "provider_has_explicit_rows": True,
            "reconstruction_mode": "geometry_first",
            "schema_assessment": {"status": "supported"},
        },
    )


@pytest.mark.parametrize(
    "values",
    [
        {"name": "Насос НЦ-50"},
        {"position": "2"},
    ],
)
def test_structured_identity_only_body_row_is_export_blocked(tmp_path, values):
    row = SpecificationRowAssembler().build_page(1, [_structured_ocr_row(values)])[0]
    assert row["row_type"] == "item_candidate"
    assert "physical_row_unresolved" in row["review_reasons"]
    with pytest.raises(ValueError, match="Не проверено"):
        ExcelExportService().export(
            [row],
            ["position", "name", "unit", "quantity", "mass"],
            tmp_path / "blocked.xlsx",
        )


def test_structured_section_remains_section():
    row = SpecificationRowAssembler().build_page(
        1, [_structured_ocr_row({"name": "Водоснабжение"})]
    )[0]
    assert row["row_type"] == "section"


def test_structured_system_remains_system():
    row = SpecificationRowAssembler().build_page(
        1, [_structured_ocr_row({"name": "К1"})]
    )[0]
    assert row["row_type"] == "system"


@pytest.mark.parametrize("values", [{"position": "2"}, {"name": "Насос"}])
def test_structured_short_identity_row_is_not_loose_system(tmp_path, values):
    rows = SpecificationRowAssembler().build_page(
        1,
        [
            _structured_ocr_row({"name": "Водоснабжение"}),
            _structured_ocr_row(values),
        ],
    )
    row = rows[1]
    assert row["row_type"] == "item_candidate"
    assert "physical_row_unresolved" in row["review_reasons"]
    with pytest.raises(ValueError, match="Не проверено"):
        ExcelExportService().export(
            rows,
            ["position", "name", "unit", "quantity", "mass"],
            tmp_path / "blocked.xlsx",
        )


def test_structured_confirmed_short_system_remains_system():
    rows = SpecificationRowAssembler().build_page(
        1,
        [
            _structured_ocr_row({"name": "Водоснабжение"}),
            _structured_ocr_row({"name": "К1"}),
        ],
    )
    assert rows[1]["row_type"] == "system"


def test_structured_bullet_component_remains_component():
    row = SpecificationRowAssembler().build_page(
        1, [_structured_ocr_row({"name": "- датчик температуры"})]
    )[0]
    assert row["row_type"] == "component"


def test_unstructured_ordinary_note_remains_note():
    row = SpecificationRowAssembler().build_page(
        1,
        [OcrRow(
            source_row=1,
            values={"name": "Примечание к чертежу"},
            confidences={},
            sources={},
            bbox={},
            metadata={"provider": "yandex_vision"},
        )],
    )[0]
    assert row["row_type"] == "note"


def _raster_with_rows(values: list[str]) -> tuple[RasterGridPage, PhysicalGrid]:
    grid = _normalized_grid(len(values), 3)
    image = np.full((len(values) * 100, 300), 255, dtype=np.uint8)
    for row, value in enumerate(values):
        if value:
            cv2.putText(image, value, (35, row * 100 + 65), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
    raster = RasterGridPage(
        detection=type("Detection", (), {"grid": grid})(),
        grayscale=image,
        line_mask=np.zeros_like(image),
    )
    return raster, grid


def test_physical_row_loss_witness_flags_removed_nonempty_row_but_not_empty_row():
    raster, grid = _raster_with_rows(["1", "2", ""])
    witness = physical_row_raster_witness(raster, grid, {0, 1, 2}, {0, 2})
    assert witness["raster_nonempty_rows"] == [0, 1]
    assert witness["suspected_loss_rows"] == [1]

    empty_witness = physical_row_raster_witness(raster, grid, {0, 2}, {0, 2})
    assert empty_witness["suspected_loss_rows"] == []


def test_horizontal_partial_span_is_not_high_confidence():
    image = np.full((700, 1000), 255, dtype=np.uint8)
    left, top, right, bottom = 80, 70, 920, 630
    columns, rows = 4, 6
    for column in range(columns + 1):
        x = round(left + (right - left) * column / columns)
        cv2.line(image, (x, top), (x, bottom), 0, 3)
    missing_row = round(top + (bottom - top) * 3 / rows)
    for row in range(rows + 1):
        y = round(top + (bottom - top) * row / rows)
        cv2.line(image, (left, y), (right, y), 0, 3)
    first_cell_left = left + (right - left) / columns
    cv2.rectangle(
        image,
        (round(first_cell_left + 25), missing_row - 8),
        (round(first_cell_left + 170), missing_row + 8),
        255,
        -1,
    )
    detection, _mask = RasterRuledTableGridDetector().detect_image(image)
    assert detection.high_confidence is False
    assert "merged_cell_span_ambiguity" in detection.reasons


def test_recognition_page_assembly_is_atomic_and_marks_page_blocked(monkeypatch, tmp_path):
    original = __import__(
        "averon_import.services.row_assembler",
        fromlist=["SpecificationRowAssembler"],
    ).SpecificationRowAssembler.build_row
    calls = {"count": 0}

    def fail_on_second(self, page, raw):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("synthetic row assembly failure")
        return original(self, page, raw)

    monkeypatch.setattr(
        "averon_import.services.row_assembler.SpecificationRowAssembler.build_row",
        fail_on_second,
    )

    class Provider:
        key = "synthetic"

        def recognize(self, pdf_path, pages, **kwargs):
            rows = [
                OcrRow(index, {"name": name, "quantity": "1"}, {}, {}, {})
                for index, name in enumerate(("first", "second", "third"))
            ]
            return OcrResult(provider=self.key, pages=[
                PageOcrResult(page=1, rows=rows),
                PageOcrResult(page=2, rows=[
                    OcrRow(0, {"name": "later", "quantity": "1"}, {}, {}, {})
                ]),
            ])

    result = RecognitionService(PdfService(), ocr=Provider()).recognize(
        Path("doc.pdf"), tmp_path, [1, 2], None, 300, lambda *_: None,
    )
    assert [row["page"] for row in result["rows"]] == [2]
    status = result["page_statuses"]["1"]
    assert status["output_status"] == "NO_SPEC_OUTPUT"
    assert "assembly_error" in status["blockers"]
    assert "synthetic row assembly failure" in status["diagnostics"]["assembly_error"]
    with pytest.raises(ValueError, match="страница 1"):
        ExcelExportService().export(
            result["rows"],
            ["name", "quantity"],
            tmp_path / "blocked.xlsx",
            page_statuses=result["page_statuses"],
        )
