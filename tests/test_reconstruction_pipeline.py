from __future__ import annotations

import json
import os
import copy
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
        cell("M. _\nM.\n_", 26, 5, height=190),
        cell("6\n6", 26, 6, height=190),
    ]
    return {
        "page": {"width": 1000, "height": 3200},
        "textAnnotation": {
            "tables": [{"rowCount": 27, "columnCount": 8, "cells": cells}],
            "blocks": [{"lines": [{"words": [
                geometry_word("Гибкий", 145, 2660, 175, 2690),
                geometry_word("воздуховод", 178, 2660, 220, 2690),
                geometry_word("#125", 205, 2660, 228, 2690),
                geometry_word("M.", 560, 2660, 575, 2690),
                geometry_word("_", 576, 2660, 590, 2690),
                geometry_word("6", 680, 2660, 695, 2690),
                geometry_word("Гибкий", 145, 2755, 175, 2785),
                geometry_word("воздуховод", 178, 2755, 220, 2785),
                geometry_word("Ø100", 205, 2755, 228, 2785),
                geometry_word("M.", 560, 2755, 575, 2785),
                geometry_word("_", 576, 2755, 590, 2785),
                geometry_word("6", 680, 2755, 695, 2785),
            ]}]}],
        },
    }


def geometry_word(text: str, left: int, top: int, right: int, bottom: int) -> dict:
    return {
        "text": text,
        "boundingBox": {"vertices": [
            {"x": left, "y": top}, {"x": right, "y": top},
            {"x": right, "y": bottom}, {"x": left, "y": bottom},
        ]},
    }


def secondary_rows_for_source_row(payload: dict, source_row: int, split_at: float) -> tuple[dict, dict]:
    """Build generic secondary row geometry for one primary source row.

    The helper derives the crop from the source table bounds; it does not use
    page-specific coordinates.  Secondary cells carry row boundaries only in
    these tests; primary words remain the value source.
    """
    table = payload["textAnnotation"]["tables"][0]
    boxes = [
        _bounds(cell_item["boundingBox"])
        for cell_item in table["cells"]
        if "boundingBox" in cell_item
    ]
    left = min(item[0] for item in boxes)
    top = min(item[1] for item in boxes)
    right = max(item[2] for item in boxes)
    bottom = max(item[3] for item in boxes)
    page_width = float(payload["page"]["width"])
    page_height = float(payload["page"]["height"])
    crop = {
        "x": left / page_width,
        "y": top / page_height,
        "width": (right - left) / page_width,
        "height": (bottom - top) / page_height,
    }
    source_cell_boxes = [
        _bounds(cell_item["boundingBox"])
        for cell_item in table["cells"]
        if int(cell_item.get("rowIndex", -1)) == source_row
    ]
    source_top = min(item[1] for item in source_cell_boxes)
    source_bottom = max(item[3] for item in source_cell_boxes)
    annotation_width = 1000.0
    annotation_height = 1000.0

    def local_box(box: tuple[float, float, float, float]) -> dict:
        return {
            "vertices": [
                {"x": (box[0] - left) / (right - left) * annotation_width, "y": (box[1] - top) / (bottom - top) * annotation_height},
                {"x": (box[2] - left) / (right - left) * annotation_width, "y": (box[1] - top) / (bottom - top) * annotation_height},
                {"x": (box[2] - left) / (right - left) * annotation_width, "y": (box[3] - top) / (bottom - top) * annotation_height},
                {"x": (box[0] - left) / (right - left) * annotation_width, "y": (box[3] - top) / (bottom - top) * annotation_height},
            ]
        }

    split = split_at
    cells = []
    for row_index, row_top, row_bottom in (
        (0, source_top, split),
        (1, split, source_bottom),
    ):
        cells.append({
            "text": "",
            "rowIndex": row_index,
            "columnIndex": 0,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": local_box((left, row_top, right, row_bottom)),
        })
    return (
        {
            "page": {"width": annotation_width, "height": annotation_height},
            "textAnnotation": {
                "width": annotation_width,
                "height": annotation_height,
                "tables": [{"rowCount": 2, "columnCount": 1, "cells": cells}],
            },
        },
        crop,
    )


def _bounds(bbox: dict) -> tuple[float, float, float, float]:
    vertices = bbox["vertices"]
    xs = [float(point["x"]) for point in vertices]
    ys = [float(point["y"]) for point in vertices]
    return min(xs), min(ys), max(xs), max(ys)


def _word_bounds(word: dict) -> tuple[float, float, float, float]:
    return _bounds(word["boundingBox"])


def _all_fixture_words(payload: dict) -> list[dict]:
    return [
        word_item
        for block in payload["textAnnotation"].get("blocks", [])
        for line in block.get("lines", [])
        for word_item in line.get("words", [])
    ]


def page18_structure_fixture() -> dict:
    def wide_cell(text: str, row: int, column: int, *, height: int = 90) -> dict:
        lefts = [40, 140, 340, 500, 600, 700, 760, 820]
        rights = [140, 340, 500, 600, 700, 760, 820, 980]
        top = 40 + row * 100
        return {
            "text": text,
            "rowIndex": row,
            "columnIndex": column,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": {"vertices": [
                {"x": lefts[column], "y": top},
                {"x": rights[column], "y": top},
                {"x": rights[column], "y": top + height},
                {"x": lefts[column], "y": top + height},
            ]},
        }

    cells = [cell(text, 0, column) for column, text in enumerate([
        *PAGE30_HEADER[:-1], "Масса единицы, кг. Примечания",
    ])]
    cells += [cell(text, 1, column) for column, text in {
        2: "3", 4: "5", 5: "6", 6: "7",
    }.items()]
    for row_index, name in enumerate([
        "Вентиляция*:",
        "П1",
        "клапан воздушный",
        "шумоглушитель",
        "гибкие вставки",
        "щит управления",
        "Дроссель-клапан",
        "Теплоизоляция",
    ], start=2):
        cells.append(cell(name, row_index, 1))
        cells.append(cell("шт.", row_index, 5))
        cells.append(cell("2", row_index, 6))
    cells.extend([
        wide_cell(
            "Наружная решетка\nЩелевая решетка с КСД приточная",
            10, 1, height=180,
        ),
        wide_cell("APH 1100x800\n6APC2000", 10, 2, height=180),
        wide_cell("Арктика\nАрктика", 10, 4, height=180),
        wide_cell("шт.\nшт.", 10, 5, height=180),
        wide_cell("6", 10, 6, height=180),
        wide_cell(
            "Подобрать решетку под\nцвет облицовки\nПодобрать под цвет\nотделки",
            10, 7, height=180,
        ),
    ])
    cells.extend([
        cell("", 11, 0),
        cell("", 11, 1),
        cell("", 12, 0),
        cell("", 12, 1),
        wide_cell("Примечание:\nслужебный нижний блок", 13, 1, height=600),
        wide_cell("Лист", 13, 5, height=600),
    ])
    words = [
        geometry_word("Наружная", 145, 1050, 195, 1080),
        geometry_word("решетка", 200, 1050, 245, 1080),
        geometry_word("Щелевая", 145, 1140, 195, 1170),
        geometry_word("решетка", 200, 1140, 245, 1170),
        geometry_word("с", 250, 1140, 260, 1170),
        geometry_word("КСД", 265, 1140, 290, 1170),
        geometry_word("приточная", 295, 1140, 335, 1170),
        geometry_word("APH", 345, 1050, 375, 1080),
        geometry_word("1100x800", 380, 1050, 440, 1080),
        geometry_word("6APC2000", 355, 1140, 420, 1170),
        geometry_word("Арктика", 605, 1050, 650, 1080),
        geometry_word("Арктика", 605, 1140, 650, 1170),
        geometry_word("шт.", 705, 1050, 735, 1080),
        geometry_word("шт.", 705, 1140, 735, 1170),
        geometry_word("6", 780, 1140, 795, 1170),
    ]
    return {
        "page": {"width": 1000, "height": 1600},
        "textAnnotation": {
            "tables": [{"rowCount": 14, "columnCount": 8, "cells": cells}],
            "blocks": [{"lines": [{"words": words}]}],
        },
    }


def page18_parent_component_fixture() -> dict:
    """Compact geometry fixture for parent/component physical splitting."""
    headers = [cell(text, 0, column) for column, text in enumerate(PAGE30_HEADER)]

    def wide(text: str, row: int, column: int, height: int = 180) -> dict:
        lefts = [40, 140, 340, 500, 600, 700, 760, 820]
        rights = [140, 340, 500, 600, 700, 760, 820, 980]
        left = lefts[column]
        top = 40 + (row - 2) * 200 + 200
        return {
            "text": text,
            "rowIndex": row,
            "columnIndex": column,
            "rowSpan": 1,
            "columnSpan": 1,
            "boundingBox": {"vertices": [
                {"x": left, "y": top}, {"x": rights[column], "y": top},
                {"x": rights[column], "y": top + height}, {"x": left, "y": top + height},
            ]},
        }

    cells = headers + [
        wide("Приточная установка в компл.:\nмоноблочная приточная установка", 2, 1),
        wide("Scirocco", 2, 2),
        wide('ООО "Глобал Климат"', 2, 4),
        wide("компл.", 2, 5),
        wide("1", 2, 6),
        wide("- диф реле давления\n- датчик канальной температуры", 3, 1),
    ]
    words = [
        geometry_word("Приточная", 145, 260, 190, 290),
        geometry_word("установка", 195, 260, 245, 290),
        geometry_word("в", 250, 260, 260, 290),
        geometry_word("компл.", 265, 260, 305, 290),
        geometry_word("ООО", 605, 260, 630, 290),
        geometry_word("Глобал", 635, 260, 675, 290),
        geometry_word("Климат", 678, 260, 698, 290),
        geometry_word("компл.", 705, 260, 745, 290),
        geometry_word("1", 765, 260, 780, 290),
        geometry_word("моноблочная", 145, 390, 215, 420),
        geometry_word("приточная", 220, 390, 280, 420),
        geometry_word("установка", 285, 390, 335, 420),
        geometry_word("Scirocco", 345, 390, 400, 420),
        geometry_word("-", 145, 460, 155, 490),
        geometry_word("диф", 160, 460, 185, 490),
        geometry_word("реле", 190, 460, 220, 490),
        geometry_word("давления", 225, 460, 280, 490),
        geometry_word("-", 145, 590, 155, 620),
        geometry_word("датчик", 160, 590, 200, 620),
        geometry_word("канальной", 205, 590, 265, 620),
        geometry_word("температуры", 270, 590, 340, 620),
    ]
    return {
        "page": {"width": 1000, "height": 1000},
        "textAnnotation": {
            "tables": [{"rowCount": 4, "columnCount": 8, "cells": cells}],
            "blocks": [{"lines": [{"words": words}]}],
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
    assert [row["unit"] for row in ducts] == ["м", "м", "м"]
    assert all(row["structured_table"] for row in assembled if row["row_type"] != "skip")
    assert all(row["confidence"] == 0.0 for row in assembled)
    assert all("no_confidence" in row["review_reasons"] for row in assembled)


def test_wrapped_multiline_source_row_with_one_band_stays_one_ambiguous_subrow():
    payload = copy.deepcopy(page30_fixture())
    payload["textAnnotation"]["blocks"][0]["lines"][0]["words"] = [
        word_item for word_item in _all_fixture_words(payload)
        if _word_bounds(word_item)[1] < 2720
    ]

    rows = reconstruct_page_rows(payload, "yandex_vision")
    split_rows = [row for row in rows if row.metadata.get("source_row_index") == 26]

    assert len(split_rows) == 1
    assert split_rows[0].metadata["source_subrow_index"] == 0
    assert split_rows[0].metadata["structural_ambiguity"] is True
    assert "structural_ambiguity" in split_rows[0].metadata["review_reasons"]


def test_secondary_row_geometry_splits_source_row_without_raw_line_matching():
    payload = page30_fixture()
    secondary, crop = secondary_rows_for_source_row(payload, 26, 2735)
    rows = reconstruct_page_rows(
        payload,
        "yandex_vision",
        secondary_payload=secondary,
        secondary_crop=crop,
    )
    split_rows = [row for row in rows if row.metadata.get("source_row_index") == 26]

    assert [row.metadata["source_subrow_index"] for row in split_rows] == [0, 1]
    assert all("secondary_table_rows" in row.metadata["split_evidence"] for row in split_rows)
    assert [(row.values.get("unit"), row.values.get("quantity")) for row in split_rows] == [
        ("м", "6"), ("м", "6")
    ]


def test_secondary_geometry_splits_one_multiline_cell_into_two_subrows():
    payload = copy.deepcopy(page30_fixture())
    for cell_item in payload["textAnnotation"]["tables"][0]["cells"]:
        if int(cell_item.get("rowIndex", -1)) == 26 and int(cell_item.get("columnIndex", -1)) != 1:
            cell_item["text"] = ""
    payload["textAnnotation"]["blocks"][0]["lines"][0]["words"] = [
        word_item for word_item in _all_fixture_words(payload)
        if _word_bounds(word_item)[0] < 400
    ]
    secondary, crop = secondary_rows_for_source_row(payload, 26, 2735)

    rows = reconstruct_page_rows(
        payload,
        "yandex_vision",
        secondary_payload=secondary,
        secondary_crop=crop,
    )
    split_rows = [row for row in rows if row.metadata.get("source_row_index") == 26]

    assert len(split_rows) == 2
    assert [row.values["name"] for row in split_rows] == [
        "Гибкий воздуховод #125", "Гибкий воздуховод Ø100"
    ]
    assert all(row.metadata["split_evidence"] == ["secondary_table_rows"] for row in split_rows)


def test_two_word_geometry_bands_create_two_subrows_even_with_three_raw_fragments():
    rows = reconstruct_page_rows(page30_fixture(), "yandex_vision")
    split_rows = [row for row in rows if row.metadata.get("source_row_index") == 26]

    assert len(split_rows) == 2
    assert [row.metadata["source_subrow_index"] for row in split_rows] == [0, 1]
    assert [row.values["name"] for row in split_rows] == [
        "Гибкий воздуховод #125", "Гибкий воздуховод Ø100"
    ]
    assert [row.values["unit"] for row in split_rows] == ["м", "м"]
    assert [row.values["quantity"] for row in split_rows] == ["6", "6"]
    assert all("primary_word_bands" in row.metadata["split_evidence"] for row in split_rows)


def test_subrow_bbox_contains_the_words_projected_into_that_band():
    payload = page30_fixture()
    rows = reconstruct_page_rows(payload, "yandex_vision")
    split_rows = [row for row in rows if row.metadata.get("source_row_index") == 26]
    words = _all_fixture_words(payload)

    for row in split_rows:
        bounds = _bounds({
            "vertices": [
                {"x": row.bbox["x"] * 1000, "y": row.bbox["y"] * 3200},
                {"x": (row.bbox["x"] + row.bbox["width"]) * 1000, "y": row.bbox["y"] * 3200},
                {"x": (row.bbox["x"] + row.bbox["width"]) * 1000, "y": (row.bbox["y"] + row.bbox["height"]) * 3200},
                {"x": row.bbox["x"] * 1000, "y": (row.bbox["y"] + row.bbox["height"]) * 3200},
            ]
        })
        projected_words = [
            word_item for word_item in words
            if bounds[1] <= sum(_word_bounds(word_item)[1::2]) / 2 < bounds[3]
        ]
        assert projected_words
        assert all(
            bounds[0] <= (word_bounds[0] + word_bounds[2]) / 2 <= bounds[2]
            and bounds[1] <= (word_bounds[1] + word_bounds[3]) / 2 <= bounds[3]
            for word_item in projected_words
            for word_bounds in [_word_bounds(word_item)]
        )


def test_page18_parent_and_component_rows_use_geometry_not_newline_count():
    rows = reconstruct_page_rows(page18_parent_component_fixture(), "yandex_vision")

    parent_rows = [row for row in rows if row.metadata.get("source_row_index") == 2]
    component_rows = [row for row in rows if row.metadata.get("source_row_index") == 3]
    assert [row.values.get("name", "").rstrip(":") for row in parent_rows] == [
        "Приточная установка в компл.", "моноблочная приточная установка"
    ]
    assert parent_rows[0].values["manufacturer"] == "ООО Глобал Климат"
    assert parent_rows[0].values["unit"] == "компл."
    assert parent_rows[0].values["quantity"] == "1"
    assert parent_rows[1].values["type_mark"] == "Scirocco"
    assert [row.values["name"] for row in component_rows] == [
        "- диф реле давления", "- датчик канальной температуры"
    ]


def test_span_cell_is_projected_once_and_source_ref_is_retained():
    headers = [cell(text, 0, column) for column, text in enumerate(PAGE30_HEADER)]
    payload = {
        "page": {"width": 1000, "height": 500},
        "textAnnotation": {"tables": [{
            "rowCount": 2,
            "columnCount": 8,
            "cells": headers + [
                cell("P-1", 1, 0),
                cell("Клапан обратный", 1, 1, column_span=3),
                cell("Арктика", 1, 4),
                cell("шт.", 1, 5),
                cell("2", 1, 6),
            ],
        }]},
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")

    assert rows[0].values["name"] == "Клапан обратный"
    assert "type_mark" not in rows[0].values and "code" not in rows[0].values
    assert rows[0].metadata["source_cell_refs"]


def test_ambiguous_unit_anchor_keeps_table_path_and_marks_local_review():
    headers = [
        "Позиция", "Наименование", "Тип", "Код", "Изготовитель",
        "Единица измерения", "Количество", "Единица измерения", "Примечание",
    ]
    values = ["P-1", "Клапан", "V-1", "C-1", "Арктика", "шт.", "2", "1.5", "монтаж"]
    payload = {
        "page": {"width": 1200, "height": 500},
        "textAnnotation": {"tables": [{
            "rowCount": 2,
            "columnCount": 9,
            "cells": [cell(text, row, column) for row, row_values in enumerate((headers, values)) for column, text in enumerate(row_values)],
        }]},
    }
    rows = reconstruct_page_rows(payload, "yandex_vision")

    assert len(rows) == 1
    assert rows[0].metadata["structured_table"] is True
    assert rows[0].values["unit"] == "шт."
    assert rows[0].values.get("mass", "") == ""
    assert "ambiguous_columns" in rows[0].metadata["review_reasons"]


def test_dropped_footer_row_has_traceable_diagnostic_reason():
    diagnostics: dict = {}
    reconstruct_page_rows(
        page18_structure_fixture(), "yandex_vision", diagnostics=diagnostics
    )

    assert any(
        event.get("kind") == "row_dropped"
        and event.get("source_row_index") == 13
        and event.get("drop_reason") == "trailing_service_block"
        for event in diagnostics.get("events", [])
    )


def test_fragmented_header_uses_table_grid_and_geometry_for_merged_rows():
    rows = reconstruct_page_rows(page18_structure_fixture(), "yandex_vision")

    assert rows
    assert all(row.metadata["structured_table"] for row in rows)
    assert not any("служебный нижний блок" in str(row.values) for row in rows)

    outer = next(row for row in rows if row.values.get("name") == "Наружная решетка")
    slot = next(row for row in rows if row.values.get("type_mark") == "6APC2000")
    assert outer.values["type_mark"] == "APH 1100×800"
    assert outer.values["unit"] == "шт."
    assert "quantity" not in outer.values
    assert slot.values["name"] == "Щелевая решетка с КСД приточная"
    assert slot.values["unit"] == "шт."
    assert slot.values["quantity"] == "6"


def test_mass_header_does_not_create_duplicate_unit_mapping():
    rows = reconstruct_page_rows(page30_fixture(), "yandex_vision")
    fan = next(row for row in rows if "Канальный вентилятор" in row.values.get("name", ""))

    assert fan.metadata["structured_table"] is True
    assert fan.values["unit"] == "компл."
    assert "quantity" not in fan.values


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
    assert rows == []


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
