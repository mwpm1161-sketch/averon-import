from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from averon_import.services.ai_first.assembly import assemble_document_draft, page_from_payload
from averon_import.services.ai_first.comparison import (
    build_comparison_report,
    comparison_run_from_ai_draft,
    comparison_run_from_ocr_result,
)


FIXTURE = Path(__file__).with_name("ai_first_comparison_fixture.json")


def _fixture():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["format_version"] == 1
    return payload


def _report(*, reference=None, runs=None):
    payload = _fixture()
    return build_comparison_report(
        reference or payload["reference"],
        runs or [deepcopy(payload["standard_run"]), deepcopy(payload["qwen_run"])],
    )


def _run(report, engine):
    return next(item for item in report["runs"] if item["engine"] == engine)


def _row(table, code):
    return next(row for row in table["rows"] if row["values"].get("code") == code)


def test_shared_report_compares_both_engines_and_keeps_metrics_separate():
    report = _report()
    assert report["format_version"] == 1
    assert len(report["runs"]) == 2
    for engine in ("standard_ocr", "qwen_vision"):
        run = _run(report, engine)
        assert run["position_detection"]["expected_items"] == 4
        assert run["position_detection"]["detected_item_rows"] == 4
        assert run["position_detection"]["missing_item_rows"] == 0
        assert run["pages"]["processing_complete"] == 2
        assert run["pages"]["complete"] == 2
        assert run["classification"]["errors"] == 0
        assert run["order"]["inversion_pairs"] == 0
        assert all(metrics["errors"] == 0 for metrics in run["field_accuracy"].values())
        assert run["column_assignment"]["errors"] == 0
    assert _run(report, "standard_ocr")["cost"] == {"amount_rub": None, "basis": "unknown"}
    assert _run(report, "qwen_vision")["cost"] == {"amount_rub": 0.42, "basis": "estimated"}
    assert _run(report, "qwen_vision")["runtime"]["basis"] == "observed"
    # Reports do not repeat document values.
    serialized = json.dumps(report, ensure_ascii=False)
    assert "SYN-A01" not in serialized
    assert "Synthetic valve" not in serialized


def test_extra_and_missing_rows_do_not_shift_later_matches_or_page_metrics():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    first_table = standard["pages"][0]["tables"][0]
    first_table["rows"].insert(2, {
        "kind": "item", "values": {
            "position": "99", "code": "SYN-X99", "name": "Synthetic extra row",
            "quantity": "9", "unit": "шт",
        },
    })
    first_table["rows"] = [row for row in first_table["rows"] if row["values"].get("code") != "SYN-A02"]
    report = _report(runs=[standard])
    run = _run(report, "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 3
    assert run["position_detection"]["missing_item_rows"] == 1
    assert run["position_detection"]["unmatched_candidate_rows"] == 1
    assert run["position_detection"]["unmatched_item_candidates"] == 1
    # All later items still align; no position-index cascade is counted.
    assert run["field_accuracy"]["code"]["errors"] == 1
    assert run["field_accuracy"]["name"]["errors"] == 1
    assert run["pages"]["processing_complete"] == 2
    assert run["pages"]["complete"] == 1


def test_reordered_rows_are_matched_by_content_and_order_is_reported_separately():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    rows = standard["pages"][0]["tables"][0]["rows"]
    rows[1], rows[2] = rows[2], rows[1]

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 4
    assert run["field_accuracy"]["code"]["errors"] == 0
    assert run["order"]["compared_pairs"] == 3
    assert run["order"]["inversion_pairs"] == 1
    assert run["order"]["preserved"] is False


def test_order_uses_page_global_sequence_across_tables():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    table_a, table_b = standard["pages"][0]["tables"]
    table_a["rows"][0]["page_order"] = 0
    table_a["rows"][1]["page_order"] = 2
    table_a["rows"][2]["page_order"] = 3
    table_b["rows"][0]["page_order"] = 4
    table_b["rows"][1]["page_order"] = 1

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 4
    assert run["order"]["compared_pairs"] == 3
    assert run["order"]["inversion_pairs"] == 2


def test_same_position_across_sections_and_pages_requires_other_identity_evidence():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    table = standard["pages"][0]["tables"][1]
    table["rows"][1] = {"kind": "item", "section": "Beta", "values": {"position": "1"}}

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 3
    assert run["position_detection"]["missing_item_rows"] == 1
    assert run["position_detection"]["unmatched_item_candidates"] == 1
    assert run["field_accuracy"]["code"]["errors"] == 1
    assert run["field_accuracy"]["name"]["errors"] == 1
    assert run["pages"]["complete"] == 1


def test_unique_position_and_matching_section_still_do_not_match_without_identity_content():
    reference = {
        "format_version": 1,
        "case_id": "synthetic_position_is_not_identity",
        "source_sha256": "0000000000000000000000000000000000000000000000000000000000000002",
        "expected_pages": [1],
        "expected_columns": [{"page": 1, "table_index": 0, "fields": ["position", "code", "name"]}],
        "expected_rows": [{
            "page": 1, "table_index": 0, "row_index": 0, "order_index": 0, "role": "item",
            "section": "Alpha",
            "values": {"position": "8", "code": "SYN-P08", "name": "Synthetic pump"},
        }],
    }
    run = {
        "engine": "standard_ocr",
        "source_sha256": "0000000000000000000000000000000000000000000000000000000000000002",
        "pages": [{"page": 1, "status": "completed", "tables": [{
            "table_index": 0, "columns": ["position", "code", "name"],
            "rows": [{"kind": "item", "section": "Alpha", "values": {"position": "8"}}],
        }]}],
    }
    report = build_comparison_report(reference, [run])
    result = _run(report, "standard_ocr")
    assert result["position_detection"]["detected_item_rows"] == 0
    assert result["position_detection"]["missing_item_rows"] == 1
    assert result["position_detection"]["unmatched_item_candidates"] == 1
    assert result["pages"]["complete"] == 0
    assert result["column_assignment"]["status"] == "unavailable"


def test_classification_errors_do_not_change_position_detection_or_field_scores():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    standard["pages"][0]["tables"][0]["rows"][0]["kind"] = "item"

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 4
    assert run["classification"]["errors"] == 1
    assert run["field_accuracy"]["code"]["errors"] == 0
    assert run["position_detection"]["candidate_item_rows"] == 5


def test_position_detection_is_independent_of_candidate_classification():
    source_sha256 = "2" * 64
    reference = {
        "format_version": 1, "case_id": "synthetic_misclassified_position",
        "source_sha256": source_sha256, "expected_pages": [1],
        "expected_columns": [{"page": 1, "table_index": 0, "fields": ["position", "name"]}],
        "expected_rows": [{
            "page": 1, "table_index": 0, "row_index": 0, "order_index": 0,
            "role": "item", "values": {"position": "1", "name": "abcd"},
        }],
    }
    run = {
        "engine": "standard_ocr", "source_sha256": source_sha256,
        "pages": [{"page": 1, "status": "completed", "tables": [{
            "table_index": 0, "columns": ["position", "name"], "rows": [{
                "kind": "section", "page_order": 0,
                "values": {"position": "1", "name": "abxy"},
            }],
        }]}],
    }

    result = _run(build_comparison_report(reference, [run]), "standard_ocr")
    assert result["position_detection"]["detected_item_rows"] == 1
    assert result["classification"]["errors"] == 1
    assert result["field_accuracy"]["name"]["errors"] == 1


def test_equal_weight_duplicate_rows_are_not_reported_as_confirmed_matches():
    source_sha256 = "1" * 64
    reference = {
        "format_version": 1,
        "case_id": "synthetic_ambiguous_duplicate",
        "source_sha256": source_sha256,
        "expected_pages": [1],
        "expected_columns": [
            {"page": 1, "table_index": index, "fields": ["position", "code", "name"]}
            for index in (0, 1)
        ],
        "expected_rows": [
            {
                "page": 1, "table_index": index, "row_index": 0, "order_index": index,
                "role": "item", "values": {
                    "position": "1", "code": "SYN-DUP", "name": "Synthetic duplicate",
                },
            }
            for index in (0, 1)
        ],
    }
    run = {
        "engine": "standard_ocr", "source_sha256": source_sha256,
        "pages": [{
            "page": 1, "status": "completed", "tables": [
                {"table_index": index, "columns": ["position", "code", "name"], "rows": [{
                    "kind": "item", "page_order": index, "values": {
                        "position": "1", "code": "SYN-DUP", "name": "Synthetic duplicate",
                    },
                }]}
                for index in (0, 1)
            ],
        }],
    }

    result = _run(build_comparison_report(reference, [run]), "standard_ocr")
    assert result["matching"] == {"ambiguous_expected_rows": 2, "ambiguous_candidate_rows": 2}
    assert result["position_detection"]["detected_item_rows"] == 0
    assert result["position_detection"]["missing_item_rows"] == 2
    assert result["order"]["preserved"] is None
    assert result["physical_location"]["status"] == "unavailable"
    assert result["column_assignment"]["status"] == "unavailable"
    assert result["field_accuracy"]["name"] == {
        "compared": 0, "correct": 0, "errors": 0,
        "unresolved_ambiguous_rows": 2, "accuracy_rate": None,
    }


def test_reference_rejects_non_independent_geometry():
    payload = _fixture()
    for basis in ("provider_geometry", "model_candidate_unverified"):
        reference = deepcopy(payload["reference"])
        reference["expected_rows"][0]["location"] = {
            "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
            "basis": basis,
        }
        try:
            build_comparison_report(reference, [deepcopy(payload["standard_run"])])
        except ValueError as error:
            assert str(error) == "invalid_comparison_reference"
        else:
            raise AssertionError("reference geometry must be independently established")


def test_article_name_quantity_and_unit_errors_have_independent_counts():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    row = _row(standard["pages"][0]["tables"][0], "SYN-A01")
    row["values"].update({
        "code": "SYN-A00", "name": "Synthetic valve DN21", "quantity": "9", "unit": "м",
    })

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["position_detection"]["detected_item_rows"] == 4
    assert run["field_accuracy"]["code"]["errors"] == 1
    assert run["field_accuracy"]["name"]["errors"] == 1
    assert run["field_accuracy"]["quantity"]["errors"] == 1
    assert run["field_accuracy"]["unit"]["errors"] == 1
    assert run["field_accuracy"]["position"]["errors"] == 0


def test_columns_runtime_cost_and_page_failures_are_reported_independently():
    payload = _fixture()
    standard = deepcopy(payload["standard_run"])
    standard["pages"][0]["tables"][0]["columns"][1] = "name"
    standard["pages"][1]["status"] = "failed"

    run = _run(_report(runs=[standard]), "standard_ocr")
    assert run["column_assignment"]["errors"] == 1
    assert run["pages"]["processing_complete"] == 1
    assert run["pages"]["failed_or_incomplete"] == 1
    assert run["pages"]["complete"] == 1
    assert run["runtime"]["duration_ms"] == 850.0
    assert run["cost"]["basis"] == "unknown"


def test_ai_draft_adapter_preserves_candidate_observations_for_shared_report():
    source_id = "synthetic-adapter-case"
    payload = {
        "page": 1,
        "tables": [{
            "headers": ["Поз.", "Артикул", "Наименование", "Количество", "Ед. изм."],
            "rows": [{
                "kind": "item",
                "cells": [
                    {"column_index": 0, "raw_text": "1"},
                    {"column_index": 1, "raw_text": "SYN-Z01"},
                    {"column_index": 2, "raw_text": "Synthetic bracket"},
                    {"column_index": 3, "raw_text": "2"},
                    {"column_index": 4, "raw_text": "шт"},
                ],
            }],
        }],
    }
    page = page_from_payload(payload, source_document_id=source_id)
    draft = assemble_document_draft(source_id, [page], requested_pages=[1])
    run = comparison_run_from_ai_draft(
        draft, source_sha256="0" * 64, duration_ms=120.0,
        cost={"amount_rub": 0.1, "basis": "estimated"},
        usage={"prompt_tokens": 50, "completion_tokens": 12},
    )
    assert run["engine"] == "qwen_vision"
    assert run["pages"][0]["status"] == "completed"
    assert run["pages"][0]["tables"][0]["columns"] == ["position", "code", "name", "quantity", "unit"]
    assert run["pages"][0]["tables"][0]["rows"][0]["values"] == {
        "position": "1", "code": "SYN-Z01", "name": "Synthetic bracket", "quantity": "2", "unit": "шт",
    }


def test_standard_provider_adapter_uses_metadata_mapping_and_trusted_geometry():
    result = SimpleNamespace(
        provider="yandex_vision",
        pages=[SimpleNamespace(
            page=1,
            geometry={"width": 1000, "height": 500},
            errors=[],
            rows=[SimpleNamespace(
                source_row=0,
                values={
                    "position": "1", "code": "SYN-Z01", "name": "Synthetic bracket",
                    "quantity": "2", "unit": "шт",
                },
                confidences={}, sources={},
                bbox={"vertices": [
                    {"x": 100, "y": 100}, {"x": 900, "y": 100},
                    {"x": 900, "y": 200}, {"x": 100, "y": 200},
                ]},
                metadata={
                    "source_table_index": 0,
                    "semantic_role": "item",
                    "column_mapping": {
                        "0": ["position"], "1": ["code"], "2": ["name"],
                        "3": ["quantity"], "4": ["unit"],
                    },
                },
            )],
        )],
        stats={"elapsed_ms": 45.0},
    )
    run = comparison_run_from_ocr_result(result, source_sha256="0" * 64)
    assert run["engine"] == "yandex_vision"
    assert run["duration_ms"] == 45.0
    assert run["pages"][0]["tables"][0]["rows"][0]["location"] == {
        "bbox": {"x": 0.1, "y": 0.2, "width": 0.8, "height": 0.2},
        "basis": "provider_geometry",
    }


def test_unverified_model_box_is_not_reported_as_physical_provider_geometry():
    payload = _fixture()
    reference = deepcopy(payload["reference"])
    reference_row = next(
        row for row in reference["expected_rows"]
        if row["values"].get("code") == "SYN-A01"
    )
    reference_row["location"] = {
        "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
        "basis": "independent_reference",
    }
    standard = deepcopy(payload["standard_run"])
    first = standard["pages"][0]["tables"][0]["rows"][1]
    first["location"] = {
        "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
        "basis": "model_candidate_unverified",
    }
    run = _run(_report(reference=reference, runs=[standard]), "standard_ocr")
    assert run["physical_location"]["status"] == "candidate_only"
    assert run["physical_location"]["unverified_model_candidate_comparisons"] == 1
    assert run["physical_location"]["provider_geometry_comparisons"] == 0


def test_report_rejects_unknown_cost_basis_and_duplicate_engine_names():
    payload = _fixture()
    invalid = deepcopy(payload["standard_run"])
    invalid["cost"] = {"amount_rub": 1.0, "basis": "unknown"}
    try:
        build_comparison_report(payload["reference"], [invalid])
    except ValueError as error:
        assert str(error) == "invalid_comparison_run"
    else:
        raise AssertionError("unknown billing must not carry an amount")

    run = deepcopy(payload["standard_run"])
    try:
        build_comparison_report(payload["reference"], [run, deepcopy(run)])
    except ValueError as error:
        assert str(error) == "duplicate_comparison_engine"
    else:
        raise AssertionError("engine names must identify runs uniquely")


def test_report_rejects_runs_from_different_source_documents():
    payload = _fixture()
    run = deepcopy(payload["standard_run"])
    run["source_sha256"] = "f" * 64
    try:
        build_comparison_report(payload["reference"], [run])
    except ValueError as error:
        assert str(error) == "comparison_source_document_mismatch"
    else:
        raise AssertionError("runs must be tied to the reference source")
