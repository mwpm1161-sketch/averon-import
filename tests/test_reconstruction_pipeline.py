from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from averon_import.core.normalizers import normalize_cell, numeric_cell_metadata
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.ocr.base import OcrRow
from averon_import.services.row_assembler import SpecificationRowAssembler


def cell(text: str, row: int, column: int, *, height: int = 90, column_span: int = 1) -> dict:
    left = 40 + column * 100
    top = 40 + row * 100
    right = left + 90 * column_span
    return {
        "text": text,
        "rowIndex": row,
        "columnIndex": column,
        "rowSpan": 1,
        "columnSpan": column_span,
        "boundingBox": {"vertices": [
            {"x": left, "y": top}, {"x": right, "y": top},
            {"x": right, "y": top + height}, {"x": left, "y": top + height},
        ]},
    }


PAGE30_HEADER = [
    "Позиция",
    "Наименование и техническая характеристика",
    "Тип, марка, обозначение документа",
    "Код оборудования, изделия, материала",
    "Завод-изготовитель (поставщик)",
    "Единица измерения",
    "Количество",
    "Масса Примечания кг.",
]


def page30_fixture() -> dict:
    cells = [cell(text, 0, column) for column, text in enumerate(PAGE30_HEADER)]
    cells += [cell(text, 1, column) for column, text in {
        2: "3", 4: "5", 5: "6", 6: "7",
    }.items()]
    cells += [
        cell("B3.1", 2, 1),
        cell("Канальный вентилятор, в компл.:", 3, 1),
        cell("ST 160 /FBP.E22.2E", 3, 2),
        cell("ООО “Глобал Климат”", 3, 4),
        cell("компл.", 3, 5),
        cell("Наружная решетка", 10, 1),
        cell("APH 250x250", 10, 2),
        cell("Арктика", 10, 4),
        cell("шт.", 10, 5),
        cell("Подобрать решетку под\nцвет облицовки", 10, 7),
        cell("Клапан огнезадерживающий круглый", 11, 1),
        cell("V-klapan", 11, 4),
        cell("шт.", 11, 5),
        cell("Вытяжной диффузор Ø160", 17, 1),
        cell("VE160", 17, 2),
        cell("Арктика", 17, 4),
        cell("шт.", 17, 5),
        cell("1", 17, 6),
        cell("Воздуховод Ø200 из оцинкованной стали", 21, 1),
        cell("ГОСТ 14918-2020", 21, 2),
        cell("м", 21, 5),
        cell("1", 21, 6),
        cell("Гибкий воздуховод #160", 25, 1),
        cell("м", 25, 5),
        cell("3", 25, 6),
        cell("Гибкий воздуховод #125\nГибкий воздуховод Ø100", 26, 1, height=190),
        cell("м\nм", 26, 5, height=190),
        cell("6\n6", 26, 6, height=190),
    ]
    return {
        "page": {"width": 1000, "height": 3200},
        "textAnnotation": {
            "tables": [{"rowCount": 27, "columnCount": 8, "cells": cells}],
        },
    }


def test_page30_fixture_maps_semantic_fields_and_splits_flexible_ducts():
    rows = reconstruct_page_rows(page30_fixture(), "yandex_vision")
    assembled = SpecificationRowAssembler().build_page(30, rows)
    exported = [row for row in assembled if row["row_type"] in {"item", "component"}]

    assert not any(row["name"] in {"3", "5", "6", "7"} for row in assembled)
    fan = next(row for row in exported if "Канальный вентилятор" in row["name"])
    assert fan["manufacturer"] == "ООО “Глобал Климат”"
    assert fan["unit"] == "компл."
    grille = next(row for row in exported if row["name"] == "Наружная решетка")
    assert grille["manufacturer"] == "Арктика"
    assert "Подобрать решетку" in grille["note"]
    valve = next(row for row in exported if row["name"].startswith("Клапан"))
    assert valve["manufacturer"] == "V-klapan"
    ducts = [row for row in exported if row["name"].startswith("Гибкий воздуховод")]
    assert [(row["name"], row["quantity"]) for row in ducts] == [
        ("Гибкий воздуховод #160", "3"),
        ("Гибкий воздуховод #125", "6"),
        ("Гибкий воздуховод Ø100", "6"),
    ]
    assert all(row["structured_table"] for row in assembled if row["row_type"] != "skip")
    assert all(row["confidence"] == 0.0 for row in assembled)
    assert all("no_confidence" in row["review_reasons"] for row in assembled)


def test_multiline_note_stays_in_note_column():
    rows = reconstruct_page_rows(page30_fixture(), "yandex_vision")
    grille = next(row for row in rows if row.values.get("name") == "Наружная решетка")
    assert grille.values["note"] == "Подобрать решетку под\nцвет облицовки"
    assert grille.metadata["source_table_index"] == 0
    assert grille.metadata["source_row_index"] == 10


def test_all_nine_base_columns_survive_semantic_mapping():
    headers = [
        "Позиция", "Наименование", "Тип, марка", "Код",
        "Изготовитель", "Ед. изм.", "Количество", "Масса", "Примечание",
    ]
    values = ["P-1", "Клапан", "K-100", "C-42", "Арктика", "шт.", "2", "1,5", "Для монтажа"]
    payload = {
        "page": {"width": 1200, "height": 500},
        "textAnnotation": {
            "tables": [{
                "rowCount": 2,
                "columnCount": 9,
                "cells": [cell(text, row, column) for row, row_values in enumerate((headers, values)) for column, text in enumerate(row_values)],
            }],
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert len(rows) == 1
    assert rows[0].values == {
        "position": "P-1", "name": "Клапан", "type_mark": "K-100", "code": "C-42",
        "manufacturer": "Арктика", "unit": "шт.", "quantity": "2", "mass": "1.5",
        "note": "Для монтажа",
    }


def test_suspect_numeric_candidate_is_marked_for_review_without_changing_clean_values():
    examples = ["1О", "О,5", "10шт", "3З", "1l", "10O", "1,О5"]
    for raw in examples:
        details = numeric_cell_metadata(raw)
        assert details["normalized_candidate"] == normalize_cell("quantity", raw)
        assert details["numeric_suspect"] is True

    for raw in ["10", "10.5", "10,5", "-2", "0.25"]:
        assert numeric_cell_metadata(raw)["numeric_suspect"] is False

    row = OcrRow(
        source_row=1,
        values={"name": "Воздуховод", "quantity": "1"},
        confidences={},
        sources={"name": "yandex_vision", "quantity": "yandex_vision"},
        bbox={},
        metadata={
            "provider": "yandex_vision",
            "normalization": {"quantity": numeric_cell_metadata("1О")},
            "review_reasons": ["numeric_suspect"],
        },
    )
    assembled = SpecificationRowAssembler().build_page(1, [row])[0]
    assert assembled["quantity"] == "1"
    assert assembled["status"] == "review"
    assert "numeric_suspect" in assembled["review_reasons"]


def test_ambiguous_table_fallback_has_review_reason_instead_of_a_guess():
    payload = {
        "page": {"width": 600, "height": 800},
        "textAnnotation": {
            "tables": [{
                "rowCount": 2,
                "columnCount": 8,
                "cells": [cell(f"C{column}", 0, column) for column in range(8)]
                + [cell("Воздуховод", 1, 0)],
            }],
            "text": "Воздуховод",
        },
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")
    assert rows
    assert "ambiguous_columns" in rows[0].metadata["review_reasons"]


def test_structured_rows_with_same_name_are_not_continuation_merged():
    rows = [
        OcrRow(1, {"name": "Тройник", "quantity": "1"}, {}, {}, {}, {
            "provider": "yandex_vision", "structured_table": True,
            "provider_has_explicit_rows": True,
        }),
        OcrRow(2, {"name": "Тройник", "quantity": "2"}, {}, {}, {}, {
            "provider": "yandex_vision", "structured_table": True,
            "provider_has_explicit_rows": True,
        }),
    ]
    assembled = SpecificationRowAssembler().build_page(1, rows)
    assert [(row["name"], row["quantity"]) for row in assembled] == [
        ("Тройник", "1"), ("Тройник", "2")
    ]


def _quantity_payload(quantities: list[str]) -> dict:
    cells = [cell(text, 0, column) for column, text in enumerate(PAGE30_HEADER)]
    for row_index, quantity in enumerate(quantities, start=2):
        cells.extend([
            cell(f"Позиция {row_index}", row_index, 1),
            cell("шт.", row_index, 5),
            cell(quantity, row_index, 6),
        ])
    return {
        "page": {"width": 1000, "height": 3200},
        "textAnnotation": {
            "tables": [{"rowCount": len(quantities) + 1, "columnCount": 8, "cells": cells}],
        },
    }


def test_structured_quantities_keep_zero_one_and_adjacent_rows():
    expected = ["0", "1", "1", "2", "6", "1"]
    rows = reconstruct_page_rows(_quantity_payload(expected), "yandex_vision")
    assembled = SpecificationRowAssembler().build_page(1, rows)

    assert [row["quantity"] for row in assembled] == expected
    assert all(row.get("position", "") == "" for row in assembled)


def test_structured_text_only_row_is_preserved_without_continuation_merge():
    rows = [
        OcrRow(
            1,
            {"name": "Клапан", "unit": "шт.", "quantity": "1"},
            {},
            {},
            {},
            {"provider": "yandex_vision", "structured_table": True, "provider_has_explicit_rows": True, "source_row_index": 11},
        ),
        OcrRow(
            2,
            {"name": "с отдельным приводом 220В"},
            {},
            {},
            {},
            {"provider": "yandex_vision", "structured_table": True, "provider_has_explicit_rows": True, "source_row_index": 12},
        ),
    ]

    prepared = SpecificationRowAssembler().prepare(rows)
    assembled = SpecificationRowAssembler().build_page(30, rows)

    assert len(prepared) == 2
    assert assembled[1]["name"] == "с отдельным приводом 220В"
    assert assembled[1]["row_type"] == "note"
    assert assembled[1]["source_row_index"] == 12


def test_real_page30_cache_preserves_raw_quantities_and_source_rows():
    cache_name = os.environ.get("AVERON_PAGE30_CACHE")
    if not cache_name:
        pytest.skip("AVERON_PAGE30_CACHE is not configured")
    cache_path = Path(cache_name)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))["pages"][0]
    table = payload["textAnnotation"]["tables"][0]

    raw_source_rows = {
        int(raw["rowIndex"])
        for raw in table["cells"]
        if int(raw.get("rowIndex", -1)) >= 2 and str(raw.get("text", "")).strip()
    }
    raw_quantity_ones = {
        int(raw["rowIndex"])
        for raw in table["cells"]
        if int(raw.get("columnIndex", -1)) == 6
        and any(line.strip() == "1" for line in str(raw.get("text", "")).splitlines())
    }

    ocr_rows = reconstruct_page_rows(payload, "yandex_vision")
    assembled = SpecificationRowAssembler().build_page(30, ocr_rows)
    final_source_rows = {row.get("source_row_index") for row in assembled}

    assert raw_source_rows <= final_source_rows
    assert all(
        any(row.get("source_row_index") == source_row and row.get("quantity") == "1" for row in assembled)
        for source_row in raw_quantity_ones
    )
    assert any("привод" in (row.get("name") or "").lower() for row in assembled)
    assert not any(row.get("name") in {"1", "2", "3", "4", "5", "6", "7", "8", "9"} for row in assembled)
