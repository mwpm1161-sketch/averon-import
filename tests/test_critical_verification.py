from __future__ import annotations

from pathlib import Path

import pytest

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.critical_verification import (
    attach_secondary_candidates,
)
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    refresh_review_state,
)
from averon_import.services.row_assembler import SpecificationRowAssembler


def box(x: float, y: float, width: float, height: float) -> dict:
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


def vertices(value: dict) -> dict:
    return {
        "vertices": [
            {"x": value["x"], "y": value["y"]},
            {"x": value["x"] + value["width"], "y": value["y"]},
            {"x": value["x"] + value["width"], "y": value["y"] + value["height"]},
            {"x": value["x"], "y": value["y"] + value["height"]},
        ]
    }


def secondary_payload(cells: list[tuple[str, dict]]) -> dict:
    return {
        "textAnnotation": {
            "width": 1000,
            "height": 1000,
            "tables": [{
                "cells": [
                    {
                        "text": text,
                        "boundingBox": vertices({
                            "x": bbox["x"] * 1000,
                            "y": bbox["y"] * 1000,
                            "width": bbox["width"] * 1000,
                            "height": bbox["height"] * 1000,
                        }),
                    }
                    for text, bbox in cells
                ]
            }],
        }
    }


def primary_row(
    *,
    quantity: str = "",
    unit: str = "шт.",
    mass: str = "0.2",
    cell_bboxes: dict | None = None,
    normalization: dict | None = None,
) -> OcrRow:
    return OcrRow(
        source_row=1,
        values={
            "position": "1",
            "name": "Клапан обратный",
            "type_mark": "V-1",
            "unit": unit,
            "quantity": quantity,
            "mass": mass,
        },
        confidences={},
        sources={},
        bbox=box(0.1, 0.1, 0.8, 0.1),
        metadata={
            "provider": "yandex_vision",
            "structured_table": True,
            "cell_bboxes": cell_bboxes or {},
            "normalization": normalization or {},
            "review_reasons": [],
        },
    )


def assembled(row: OcrRow) -> dict:
    return SpecificationRowAssembler().build_row(30, row.as_dict())


def test_primary_missing_secondary_unique_becomes_manual_candidate_without_fill():
    quantity_box = box(0.70, 0.20, 0.10, 0.05)
    row = primary_row(quantity="", cell_bboxes={"quantity": quantity_box})
    result = attach_secondary_candidates(
        [row], secondary_payload([("1", quantity_box)])
    )

    assert row.values["quantity"] == ""
    assert row.metadata["value_candidates"]["quantity"] == {
        "value_candidate": "1",
        "raw_value": "1",
        "bbox": quantity_box,
        "candidate_source": "yandex_secondary",
        "review_reason": "recovered_by_secondary_ocr",
    }
    assert result == {"secondary_candidates": 1, "secondary_recovered": 1}
    final = assembled(row)
    assert final["quantity"] == ""
    assert "critical_value_missing" in critical_blockers_for_row(final)
    assert final["value_candidates"]["quantity"]["candidate_source"] == "yandex_secondary"


def test_secondary_empty_keeps_missing_critical_blocker():
    quantity_box = box(0.70, 0.20, 0.10, 0.05)
    row = primary_row(quantity="", cell_bboxes={"quantity": quantity_box})
    attach_secondary_candidates([row], secondary_payload([]))

    final = assembled(row)
    assert final["quantity"] == ""
    assert final["review_reasons"].count("critical_value_missing") == 1
    assert critical_blockers_for_row(final) == ["critical_value_missing"]


def test_ambiguous_secondary_matches_are_not_filled():
    quantity_box = box(0.70, 0.20, 0.10, 0.05)
    row = primary_row(quantity="", cell_bboxes={"quantity": quantity_box})
    attach_secondary_candidates(
        [row],
        secondary_payload([
            ("1", quantity_box),
            ("1", quantity_box),
        ]),
    )

    assert row.metadata.get("value_candidates") in (None, {})
    assert "critical_value_missing" in critical_blockers_for_row(assembled(row))


def test_primary_secondary_conflict_is_preserved_for_review():
    quantity_box = box(0.70, 0.20, 0.10, 0.05)
    row = primary_row(quantity="2", cell_bboxes={"quantity": quantity_box})
    result = attach_secondary_candidates(
        [row], secondary_payload([("6", quantity_box)])
    )

    assert result == {"secondary_candidates": 1, "secondary_recovered": 0}
    assert row.values["quantity"] == "2"
    assert row.metadata["secondary_conflict_fields"] == ["quantity"]
    final = assembled(row)
    assert final["quantity"] == "2"
    assert "secondary_conflict" in critical_blockers_for_row(final)


def test_manual_edit_of_conflicting_field_clears_secondary_blocker():
    quantity_box = box(0.70, 0.20, 0.10, 0.05)
    row = primary_row(quantity="2", cell_bboxes={"quantity": quantity_box})
    attach_secondary_candidates([row], secondary_payload([("6", quantity_box)]))

    final = assembled(row)
    assert final["secondary_conflict_fields"] == ["quantity"]
    assert "secondary_conflict" in final["review_reasons"]

    final["quantity"] = "6"
    final["edited_fields"] = ["quantity"]
    final["status"] = "edited"
    refresh_review_state(final)

    assert "secondary_conflict" not in critical_blockers_for_row(final)
    assert "secondary_conflict" not in final["review_reasons"]


def test_manual_correction_clears_missing_blocker():
    row = assembled(primary_row(quantity="", unit="", mass=""))
    assert "critical_value_missing" in critical_blockers_for_row(row)

    row.update({"quantity": "1", "unit": "шт.", "mass": "0.2"})
    row["edited_fields"] = ["quantity", "unit", "mass"]
    row["status"] = "edited"
    refresh_review_state(row)

    assert critical_blockers_for_row(row) == []
    assert row["critical_fields"] == []


def test_explicit_verified_confirmation_clears_nonempty_numeric_blocker():
    row = assembled(
        primary_row(
            quantity="1",
            normalization={"quantity": {
                "raw_value": "1О",
                "normalized_candidate": "1",
                "numeric_suspect": True,
            }},
        )
    )
    assert "numeric_suspect" in critical_blockers_for_row(row)
    row["status"] = "verified"
    refresh_review_state(row)
    assert critical_blockers_for_row(row) == []


def test_unresolved_critical_blocks_normal_export(tmp_path: Path):
    row = assembled(primary_row(quantity="", unit="", mass=""))
    with pytest.raises(ValueError, match="Не проверено 3 критичных значений"):
        ExcelExportService().export(
            [row], ["name", "unit", "quantity", "mass"], tmp_path / "blocked.xlsx"
        )


def test_no_confidence_alone_does_not_block_export(tmp_path: Path):
    row = assembled(primary_row(quantity="1"))
    assert "no_confidence" in row["review_reasons"]
    assert critical_blockers_for_row(row) == []
    target = tmp_path / "allowed.xlsx"
    ExcelExportService().export([row], ["name", "quantity"], target)
    assert target.exists()


def test_numeric_suspect_blocks_export_until_verified(tmp_path: Path):
    row = assembled(
        primary_row(
            quantity="1",
            normalization={"quantity": {
                "raw_value": "1О",
                "normalized_candidate": "1",
                "numeric_suspect": True,
            }},
        )
    )
    with pytest.raises(ValueError, match="Не проверено 1 критичных значений"):
        ExcelExportService().export([row], ["name", "quantity"], tmp_path / "blocked.xlsx")

    row["status"] = "verified"
    refresh_review_state(row)
    ExcelExportService().export([row], ["name", "quantity"], tmp_path / "verified.xlsx")
