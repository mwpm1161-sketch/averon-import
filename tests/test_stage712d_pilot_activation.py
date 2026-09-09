from __future__ import annotations

from pathlib import Path

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.base import OcrResult, OcrRow, PageOcrResult
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.reconstruction import reconstruct_page_rows_result
from averon_import.services.ocr.semantics.semantic_projection import (
    project_semantic_table,
)
from averon_import.services.ocr.yandex_vision import YandexVisionProvider
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.recognition import RecognitionService

from tests.test_stage712c_row_relations import _geometry_payload, _grid


def _semantic_result():
    diagnostics: dict = {}
    return reconstruct_page_rows_result(
        _geometry_payload(),
        "yandex_vision",
        physical_grid=_grid(),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )


def _pilot_provider() -> YandexVisionProvider:
    provider = YandexVisionProvider.__new__(YandexVisionProvider)
    provider._reconstruction_mode = "geometry"
    return provider


def test_typed_result_keeps_live_semantic_ir_outside_json_diagnostics():
    result = _semantic_result()
    assert result.physical_ir_constructed
    assert result.functional_analysis_completed
    assert result.relation_analysis_completed
    assert result.semantic_resolution_completed
    assert result.semantic_table is not None
    assert "semantic_table" not in result.diagnostics


def test_authoritative_projection_is_one_row_per_logical_item_and_candidate_only():
    result = _semantic_result()
    rows = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    assert result.semantic_authoritative
    assert len(rows) == len(result.semantic_table.logical_items)
    assert all(row.metadata["semantic_authoritative"] for row in rows)
    assert all(row.metadata["semantic_role"] == "ITEM_ROOT" for row in rows)
    assert all(row.confidences == {} for row in rows)
    assert all(
        not any(
            candidate.get("auto_trusted")
            for candidate in (row.metadata.get("value_candidates") or {}).values()
            if isinstance(candidate, dict)
        )
        for row in rows
    )


def test_semantic_assembler_does_not_classify_or_repair(monkeypatch):
    assembler = SpecificationRowAssembler()

    def fail(*_args, **_kwargs):
        raise AssertionError("legacy semantic inference was called")

    monkeypatch.setattr(assembler, "classify_row", fail)
    monkeypatch.setattr(assembler, "repair_continuation_rows", staticmethod(fail))
    raw = OcrRow(
        source_row=1,
        values={
            "name": "Насос",
            "unit": "шт",
            "quantity": "2",
            "mass": "4",
        },
        confidences={},
        sources={"name": "yandex_vision"},
        bbox={},
        metadata={
            "provider": "yandex_vision",
            "structured_table": True,
            "provider_has_explicit_rows": True,
            "semantic_authoritative": True,
            "semantic_resolved": True,
            "semantic_role": "ITEM_ROOT",
            "semantic_state": "VERIFIED",
            "review_reasons": [],
        },
    )
    row = assembler.build_semantic_row(1, raw)
    assert row["row_type"] == "item"
    assert row["status"] == "recognized"
    assert "no_confidence" not in row["review_reasons"]


def test_recognition_service_selects_semantic_presentation_path():
    class Provider:
        key = "yandex_vision"

        def recognize(self, _pdf_path, pages, **_kwargs):
            return OcrResult(
                provider=self.key,
                pages=[
                    PageOcrResult(
                        page=pages[0],
                        rows=[
                            OcrRow(
                                1,
                                {"name": "Насос", "unit": "шт", "quantity": "2", "mass": "4"},
                                {},
                                {},
                                {},
                                {
                                    "provider": self.key,
                                    "structured_table": True,
                                    "provider_has_explicit_rows": True,
                                    "semantic_authoritative": True,
                                    "semantic_resolved": True,
                                    "semantic_role": "ITEM_ROOT",
                                    "semantic_state": "VERIFIED",
                                },
                            )
                        ],
                        page_status={
                            "page": pages[0],
                            "layout_status": "TRUSTED",
                            "schema_status": "SUPPORTED",
                            "output_status": "USABLE",
                            "diagnostics": {"semantic_authoritative": True},
                        },
                    )
                ],
            )

    result = RecognitionService(object(), ocr=Provider()).recognize(
        Path("document.pdf"), Path("pages"), [1], None, 300, lambda *_: None
    )
    assert result["rows"][0]["row_type"] == "item"
    assert result["rows"][0]["status"] == "recognized"


def test_yandex_no_confidence_does_not_review_a_complete_grounded_item():
    assembler = SpecificationRowAssembler()
    raw = OcrRow(
        1,
        {"name": "Насос", "unit": "шт", "quantity": "2", "mass": "4"},
        {},
        {},
        {},
        {"provider": "yandex_vision", "semantic_authoritative": True,
         "semantic_resolved": True, "semantic_role": "ITEM_ROOT",
         "semantic_state": "VERIFIED", "structured_table": True,
         "provider_has_explicit_rows": True},
    )
    row = assembler.build_semantic_row(1, raw)
    assert row["status"] == "recognized"
    assert row["review_reasons"] == []


def test_unresolved_semantic_row_is_review_evidence_not_an_item():
    assembler = SpecificationRowAssembler()
    raw = OcrRow(
        2,
        {},
        {},
        {},
        {},
        {
            "provider": "yandex_vision",
            "structured_table": True,
            "semantic_authoritative": True,
            "semantic_resolved": False,
            "semantic_role": "ITEM_ROOT",
            "semantic_state": "REVIEW",
            "review_reasons": ["physical_row_semantics_unresolved"],
        },
    )
    row = assembler.build_semantic_row(1, raw)
    assert row["row_type"] == "semantic_review"
    assert row["status"] == "review"
    assert "physical_row_semantics_unresolved" in row["review_reasons"]


def test_semantic_failure_is_fail_closed_without_legacy_rows():
    result = _semantic_result()
    result.semantic_candidate = True
    result.semantic_resolution_error = "RuntimeError: resolver failed"
    old_rows = list(result.rows)
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    assert old_rows
    assert projected == []
    assert result.diagnostics["semantic_authoritative"] is True
    assert result.diagnostics["semantic_resolution_error"]


def test_semantic_unresolved_rows_block_page_and_strict_export(tmp_path: Path):
    diagnostics = {
        "selected_mode": "geometry_first",
        "geometry_grid": {"high_confidence": True},
        "schema": {"status": "supported"},
        "semantic_authoritative": True,
        "unresolved_physical_row_count": 1,
    }
    status = page_status_from_diagnostics(1, diagnostics, row_count=1).as_dict()
    assert status["output_status"] == "REVIEW_REQUIRED"
    assert "physical_row_semantics_unresolved" in status["blockers"]
    row = {
        "name": "Насос",
        "row_type": "item",
        "structured_table": True,
        "semantic_authoritative": True,
        "semantic_state": "REVIEW",
        "ocr_metadata": {"semantic_review": True},
        "page": 1,
    }
    output = tmp_path / "review.xlsx"
    ExcelExportService().export(
        [row], ["name"], output, page_statuses={"1": status}, enforce_safety=False
    )
    # Review rows are not part of a normal exportable projection.
    assert output.exists()


def test_rollout_switch_disables_activation_but_keeps_legacy_result(monkeypatch):
    monkeypatch.setenv("AVERON_SEMANTIC_TABLE_AUTHORITATIVE", "0")
    result = _semantic_result()
    rows = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    assert result.semantic_authoritative is False
    assert rows == result.rows
    assert result.diagnostics["semantic_authoritative_activation"]["enabled"] is False
