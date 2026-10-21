from __future__ import annotations

from averon_import.services.ocr.qwen_vision_contract import VisionPage
from averon_import.services.ocr.qwen_vision_diagnostics import analyze_transcriptions


HEADERS = ["Позиция", "Наименование", "Тип", "Количество", "Масса единицы, кг"]


def _cell(index: int, text: str = "", **changes):
    cell = {
        "column_index": index, "column_span": 1, "row_span": 1,
        "raw_text": text, "alternatives": [], "handwritten_replacements": [],
        "unreadable": False, "struck_through": False, "handwritten": False,
        "bbox_candidate": None,
    }
    cell.update(changes)
    return cell


def _row(position: str = "", name: str = "", quantity: str = "", mass: str = "",
         *, kind: str = "header", row_changes=None, cell_changes=None):
    cell_changes = cell_changes or {}
    cells = [
        _cell(0, position, **cell_changes.get(0, {})),
        _cell(1, name, **cell_changes.get(1, {})),
        _cell(2, ""),
        _cell(3, quantity, **cell_changes.get(3, {})),
        _cell(4, mass, **cell_changes.get(4, {})),
    ]
    result = {
        "kind": kind, "logical_row_index": None, "continuation_of": None,
        "continues_previous_page": False, "struck_through": False,
        "handwritten": False, "handwritten_replacements": [],
        "bbox_candidate": None, "cells": cells,
    }
    result.update(row_changes or {})
    return result


def _page(rows, headers=None):
    return VisionPage.model_validate({
        "page": 1,
        "tables": [{
            "headers": headers or HEADERS, "rows": rows,
            "continues_previous_page": False, "bbox_candidate": None,
        }],
        "unreadable_fragments": [], "handwritten_replacements": [],
    })


def _analysis(rows, headers=None):
    return analyze_transcriptions({1: _page(rows, headers)})


def _position_rows(numbers, *, kind="header"):
    return [_row(str(number), f"Test item {number}", "2", kind=kind) for number in numbers]


def _table_summary(result):
    return result["summary"]["position_sequences"][0]


def test_all_actual_positions_can_be_found_when_model_calls_them_headers():
    result = _analysis(_position_rows(range(1, 16)))
    assert result["summary"]["source_row_count"] == 15
    assert result["summary"]["model_kind_counts"] == {"header": 15}
    assert result["summary"]["potential_numbered_position_count"] == 15
    assert result["summary"]["model_kind_candidate_row_count"] == 0
    assert result["summary"]["role_conflict_count"] == 15
    assert result["summary"]["confirmed_position_count"] == 0
    assert all(row["role_verified"] is False for row in result["rows"])


def test_column_numbering_1_through_9_is_not_a_specification_position():
    headers = ["Позиция", "Наименование", "Тип", "Код", "Завод", "Ед.", "Количество", "Масса", "Примечание"]
    row = {
        "kind": "header", "logical_row_index": None, "continuation_of": None,
        "continues_previous_page": False, "struck_through": False, "handwritten": False,
        "handwritten_replacements": [], "bbox_candidate": None,
        "cells": [_cell(index, str(index + 1)) for index in range(9)],
    }
    result = _analysis([row], headers)
    assert result["rows"][0]["server_role_candidate"] == "column_numbering"
    assert result["rows"][0]["position_number"] is None
    assert result["summary"]["potential_numbered_position_count"] == 0


def test_section_title_with_nearby_handwritten_number_is_not_an_item():
    section = _row(
        "", "Section A", "150", "",
        cell_changes={3: {"handwritten": True}},
    )
    result = _analysis([section, _row("1", "Test item", "2")])
    assert result["rows"][0]["server_role_candidate"] == "section_header"
    assert result["rows"][0]["position_number"] is None
    assert result["summary"]["potential_numbered_position_count"] == 1
    assert result["rows"][0]["role_verified"] is False


def test_positions_1_to_15_have_no_gap_or_duplicate():
    result = _analysis(_position_rows(range(1, 16)))
    sequence = _table_summary(result)
    assert sequence["observed_position_numbers"] == list(range(1, 16))
    assert sequence["missing_between_observed_positions"] == []
    assert sequence["duplicate_position_numbers"] == []


def test_missing_position_is_reported_without_resequencing_rows():
    result = _analysis(_position_rows([1, 2, 4]))
    sequence = _table_summary(result)
    assert sequence["observed_position_numbers"] == [1, 2, 4]
    assert sequence["missing_between_observed_positions"] == [3]
    assert [row["position_number"] for row in result["rows"]] == [1, 2, 4]


def test_duplicate_position_is_reported_and_rows_remain_distinct():
    result = _analysis(_position_rows([1, 2, 2, 3]))
    sequence = _table_summary(result)
    assert sequence["duplicate_position_numbers"] == [2]
    assert [row["row_index"] for row in result["rows"]] == [0, 1, 2, 3]


def test_struck_quantity_and_possible_replacement_remain_separate():
    row = _row(
        "1", "Test item", "10",
        cell_changes={3: {
            "struck_through": True,
            "handwritten_replacements": [{"text": "12", "bbox_candidate": None}],
        }},
    )
    record = _analysis([row])["rows"][0]
    quantity = next(cell for cell in record["cells"] if cell["column_index"] == 3)
    assert quantity["raw_text"] == "10"
    assert quantity["struck_through"] is True
    assert quantity["handwritten_replacements"][0]["text"] == "12"
    assert quantity["projection"]["status"] == "suppressed_uncertain"
    assert record["values"]["quantity"] == "10"
    assert "effective_quantity" not in record


def test_handwritten_mass_is_not_attributed_to_struck_quantity():
    row = _row(
        "1", "Test item", "80", "370",
        cell_changes={
            3: {"struck_through": True},
            4: {"handwritten": True},
        },
    )
    record = _analysis([row])["rows"][0]
    quantity, mass = (next(c for c in record["cells"] if c["column_index"] == i) for i in (3, 4))
    assert (quantity["raw_text"], quantity["struck_through"]) == ("80", True)
    assert (mass["raw_text"], mass["handwritten"]) == ("370", True)
    assert quantity["projection"]["canonical_field"] == "quantity"
    assert mass["projection"]["canonical_field"] == "mass"
    assert quantity["projection"]["status"] == mass["projection"]["status"] == "suppressed_uncertain"


def test_fully_struck_position_is_retained_as_a_candidate():
    row = _row("1", "Test item", "2", row_changes={"struck_through": True})
    result = _analysis([row])
    record = result["rows"][0]
    assert record["server_role_candidate"] == "numbered_position"
    assert record["role_verified"] is False
    assert result["summary"]["potential_numbered_position_count"] == 1
    assert record["projection"]["suppressed_fields"] == ["mass", "name", "position", "quantity", "type_mark"]


def test_unassignable_row_level_correction_does_not_pick_a_cell():
    row = _row(
        "1", "Test item", "10",
        row_changes={"handwritten_replacements": [{"text": "red mark", "bbox_candidate": None}]},
    )
    record = _analysis([row])["rows"][0]
    assert record["row_flags"]["handwritten_replacements"][0]["text"] == "red mark"
    assert all(not cell["handwritten_replacements"] for cell in record["cells"])
    assert record["projection"]["suppressed_fields"] == ["mass", "name", "position", "quantity", "type_mark"]


def test_model_kind_and_server_role_conflict_is_explicit():
    result = _analysis([_row("1", "Test item", "2", kind="header")])
    record = result["rows"][0]
    assert record["model_kind"] == "header"
    assert record["server_role_candidate"] == "numbered_position"
    assert record["role_conflict"] is True
    assert record["role_verified"] is False


def test_role_heuristics_never_grant_usable_or_sourcing_admission():
    result = _analysis(_position_rows([1]))
    record = result["rows"][0]
    assert record["role_verified"] is False
    assert result["summary"]["confirmed_position_count"] == 0
    assert not {"usable", "sourcing_admitted", "admitted_to_sourcing"} & set(record)
    assert not {"usable", "sourcing_admitted", "admitted_to_sourcing"} & set(result["summary"])
