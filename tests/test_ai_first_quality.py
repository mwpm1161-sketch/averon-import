from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from averon_import.services.ai_first.assembly import assemble_document_draft, page_from_payload
from averon_import.services.ai_first.quality import evaluate_draft_quality, reference_from_mapping


FIXTURE = Path(__file__).with_name("ai_first_quality_fixture.json")


def _cases():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["format_version"] == 1
    return payload["cases"]


def _reference(case):
    reference_payload = {key: value for key, value in case.items() if key != "synthetic_model_output"}
    return reference_from_mapping(reference_payload)


def _draft(case, model_output=None):
    source_id = f"synthetic-quality:{case['case_id']}"
    outputs = case["synthetic_model_output"] if model_output is None else model_output
    pages = [page_from_payload(item, source_document_id=source_id, page_number=item["page"]) for item in outputs]
    return assemble_document_draft(
        source_id, pages, requested_pages=case["expected_page_numbers"],
    )


def test_synthetic_ordinary_specifications_pass_exact_reference_metrics():
    results = []
    for case in _cases():
        result = evaluate_draft_quality(_reference(case), _draft(case))
        results.append(result)
        assert result["missing_pages"] == result["incomplete_pages"] == 0
        assert result["pages_with_all_positions"] == result["expected_pages"]
        assert result["missing_positions"] == result["extra_rows"] == 0
        assert result["position_coverage_rate"] == 1.0
        assert result["position_order_preserved"] is True
        assert result["position_alignment_errors"] == 0
        assert result["duplicate_positions"] == result["unexpected_positions"] == 0
        assert result["article_errors"] == result["name_errors"] == 0
        assert result["quantity_errors"] == result["unit_errors"] == 0
        assert result["mass_errors"] == result["note_errors"] == 0
        assert result["column_assignment_errors"] == result["classification_errors"] == 0

    assert sum(result["expected_positions"] for result in results) == 5
    assert all("SYN-" not in json.dumps(result) for result in results)


def test_quality_metrics_detect_missing_duplicate_reordered_and_wrong_values():
    case = deepcopy(_cases()[0])
    page_one_rows = case["synthetic_model_output"][0]["tables"][0]["rows"]
    page_one_rows[0]["cells"], page_one_rows[2]["cells"] = page_one_rows[2]["cells"], page_one_rows[0]["cells"]
    page_one_rows[2]["kind"] = "section"
    page_one_rows.extend([
        {"kind": "item", "cells": [
            {"column_index": 0, "raw_text": "99"}, {"column_index": 1, "raw_text": "SYN-X-999"},
            {"column_index": 2, "raw_text": "Synthetic false item"}, {"column_index": 3, "raw_text": "шт"},
            {"column_index": 4, "raw_text": "7"}, {"column_index": 5, "raw_text": ""},
        ]},
        {"kind": "item", "cells": [
            {"column_index": 0, "raw_text": "1"}, {"column_index": 1, "raw_text": "SYN-A-001"},
            {"column_index": 2, "raw_text": "Synthetic duplicate"}, {"column_index": 3, "raw_text": "шт"},
            {"column_index": 4, "raw_text": "2"}, {"column_index": 5, "raw_text": ""},
        ]},
        {"kind": "item", "cells": [
            {"column_index": 0, "raw_text": "1"}, {"column_index": 1, "raw_text": "SYN-A-001"},
            {"column_index": 2, "raw_text": "Synthetic repeated extra"}, {"column_index": 3, "raw_text": "шт"},
            {"column_index": 4, "raw_text": "2"}, {"column_index": 5, "raw_text": ""},
        ]},
    ])
    # Omitting page 2 lets assembly create a failed page from the source-page expectation.
    draft = _draft(case, model_output=case["synthetic_model_output"][:1])
    result = evaluate_draft_quality(_reference(case), draft)

    assert result["expected_positions"] == 3
    assert result["recognized_positions"] == 0
    assert result["missing_positions"] == 3
    assert result["position_coverage_rate"] == 0
    assert result["position_order_preserved"] is False
    assert result["duplicate_positions"] == 1
    assert result["unexpected_positions"] == 1
    assert result["extra_rows"] == 3
    assert result["extra_item_rows"] == 4
    assert result["article_errors"] == 2
    assert result["quantity_errors"] == 2
    assert result["classification_errors"] == 1
    assert result["incomplete_pages"] == 1
    assert result["pages_with_all_positions"] == 0
    assert result["page_completeness_rate"] == 0


def test_section_number_cannot_cover_missing_position_even_when_mislabeled_as_item():
    case = deepcopy(_cases()[0])
    page_one_rows = case["synthetic_model_output"][0]["tables"][0]["rows"]
    # Position 1 is absent from its real row. The section row contains a 1
    # and is incorrectly labeled as an item by the model.
    page_one_rows[0]["cells"][0]["raw_text"] = ""
    page_one_rows[1]["kind"] = "item"
    page_one_rows[1]["cells"][0]["raw_text"] = "1"

    result = evaluate_draft_quality(_reference(case), _draft(case))

    assert result["recognized_positions"] == 2
    assert result["missing_positions"] == 1
    assert result["position_coverage_rate"] == 2 / 3
    assert result["pages_with_all_positions"] == 1
    assert result["page_completeness_rate"] == 1 / 2
    assert result["classification_errors"] == 1
    assert result["position_order_preserved"] is False


def test_column_numbering_row_cannot_cover_missing_position_one():
    case = deepcopy(_cases()[0])
    case["expected_rows"][1]["role"] = "column_numbers"
    case["expected_rows"][1]["fields"] = {}
    page_one_rows = case["synthetic_model_output"][0]["tables"][0]["rows"]
    page_one_rows[0]["cells"][0]["raw_text"] = ""
    page_one_rows[1]["kind"] = "column_numbers"
    page_one_rows[1]["cells"][0]["raw_text"] = "1"

    result = evaluate_draft_quality(_reference(case), _draft(case))

    assert result["recognized_positions"] == 2
    assert result["missing_positions"] == 1
    assert result["position_coverage_rate"] == 2 / 3
    assert result["pages_with_all_positions"] == 1
    assert result["page_completeness_rate"] == 1 / 2
    assert result["classification_errors"] == 0
    assert result["position_order_preserved"] is False


def test_omitted_item_row_cannot_be_recovered_by_rows_shifted_into_its_slot():
    case = deepcopy(_cases()[0])
    case["synthetic_model_output"][0]["tables"][0]["rows"].pop(0)

    result = evaluate_draft_quality(_reference(case), _draft(case))

    assert result["missing_rows"] == 1
    assert result["recognized_positions"] == 1
    assert result["missing_positions"] == 2
    assert result["pages_with_all_positions"] == 1
    assert result["position_order_preserved"] is False
    assert result["classification_errors"] == 2


def test_reordered_physical_item_rows_keep_order_and_coverage_errors_independent():
    case = deepcopy(_cases()[0])
    rows = case["synthetic_model_output"][0]["tables"][0]["rows"]
    rows[0], rows[2] = rows[2], rows[0]

    result = evaluate_draft_quality(_reference(case), _draft(case))

    assert result["position_order_preserved"] is False
    assert result["position_order_errors"] == 1
    assert result["recognized_positions"] == 1
    assert result["missing_positions"] == 2
    assert result["classification_errors"] == 0


def test_quality_metrics_detect_column_assignment_error_without_rewriting_raw_cells():
    case = deepcopy(_cases()[1])
    case["synthetic_model_output"][0]["tables"][0]["headers"][0] = "Unknown synthetic column"
    draft = _draft(case)
    result = evaluate_draft_quality(_reference(case), draft)

    assert result["column_assignment_errors"] == 1
    assert result["article_errors"] == 2
    assert draft.pages[0].tables[0].columns[0].raw_header == "Unknown synthetic column"
    assert draft.pages[0].tables[0].rows[0].raw_cells[0].raw_value == "SYN-B-101"

    case = deepcopy(_cases()[1])
    cells = case["synthetic_model_output"][0]["tables"][0]["rows"][0]["cells"]
    cells[0]["column_index"], cells[4]["column_index"] = cells[4]["column_index"], cells[0]["column_index"]
    draft = _draft(case)
    result = evaluate_draft_quality(_reference(case), draft)
    assert result["column_assignment_errors"] == 2
    assert result["header_assignment_errors"] == 0
    assert result["cell_column_assignment_errors"] == 2
    assert result["article_errors"] == result["name_errors"] == 1


def test_reference_rejects_duplicate_expected_positions_and_unknown_fields():
    case = deepcopy(_cases()[0])
    case["expected_rows"].append(deepcopy(case["expected_rows"][0]))
    try:
        reference_from_mapping(case)
    except ValueError as error:
        assert str(error) == "invalid_quality_reference"
    else:
        raise AssertionError("duplicate gold row must be rejected")

    case = deepcopy(_cases()[0])
    case["expected_rows"][0]["fields"]["not_a_field"] = "Synthetic"
    try:
        reference_from_mapping(case)
    except ValueError as error:
        assert str(error) == "invalid_quality_reference"
    else:
        raise AssertionError("unknown reference field must be rejected")
