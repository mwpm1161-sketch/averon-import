from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from collections.abc import Mapping
import json
from pathlib import Path

import pytest

from averon_import.core.normalizers import normalize_cell
from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.base import OcrResult, OcrRow, PageOcrResult
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.reconstruction import reconstruct_page_rows_result
from averon_import.services.ocr.semantics.semantic_projection import (
    project_semantic_table,
)
from averon_import.services.ocr.semantics.row_evidence import (
    RowRelationAssessment,
    RowRelationState,
    RowRelationType,
)
from averon_import.services.ocr.semantics.semantic_table import (
    LogicalFieldValue,
    ValueCandidate,
)
from averon_import.services.ocr.yandex_vision import YandexVisionProvider
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.recognition import RecognitionService
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    missing_critical_fields,
    refresh_review_state,
)
from averon_import.services.review_decisions import (
    RELATION_DECISION,
    HumanReviewService,
)

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


def _with_semantic_item(result, item_index=0, **field_updates):
    semantic = result.semantic_table
    item = semantic.logical_items[item_index]
    fields = dict(item.fields)
    for field, value in field_updates.items():
        if value is None:
            fields.pop(field, None)
        else:
            fields[field] = value
    result.semantic_table = replace(
        semantic,
        logical_items=(
            replace(item, fields=fields),
            *semantic.logical_items[item_index + 1:],
        ),
    )
    return result


def _assembled_semantic_row(result):
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    assert projected
    return SpecificationRowAssembler().build_semantic_row(1, projected[0])


def _unresolve_first_item(result):
    semantic = result.semantic_table
    item = semantic.logical_items[0]
    target_key = item.physical_row_refs[0].key
    target = next(
        disposition
        for disposition in semantic.dispositions
        if disposition.physical_row_ref.key == target_key
    )
    unresolved = replace(
        target,
        validation_state="UNVALIDATED",
        role_state="UNRESOLVED",
        reasons=("physical_row_semantics_unresolved",),
    )
    result.semantic_table = replace(
        semantic,
        logical_items=semantic.logical_items[1:],
        dispositions=tuple(
            unresolved if disposition.physical_row_ref.key == target_key else disposition
            for disposition in semantic.dispositions
        ),
    )
    return result


def _relation_result(*, state=RowRelationState.AMBIGUOUS, contradictions=()):
    result = _unresolve_first_item(_semantic_result())
    semantic = result.semantic_table
    table = semantic.analysis_context.physical_table
    source_row = table.rows[1]
    source_row = replace(
        source_row,
        cells=tuple(cell for cell in source_row.cells if cell.ref.column_index == 1),
    )
    updated_table = replace(
        table,
        rows=(table.rows[0], source_row, *table.rows[2:]),
        cells=tuple(
            cell
            for physical_row in (table.rows[0], source_row, *table.rows[2:])
            for cell in physical_row.cells
        ),
    )
    updated_context = replace(semantic.analysis_context, physical_table=updated_table)
    semantic = replace(semantic, analysis_context=updated_context)
    result.physical_table = updated_table
    result.semantic_table = semantic
    relation = RowRelationAssessment(
        source_row_ref=source_row.ref,
        candidate_target_refs=(updated_table.rows[2].ref,),
        relation_type=RowRelationType.CONTINUATION_OF,
        state=state,
        evidence_strength="SUPPORTING",
        evidence=("source_fields_subset_of_parent",),
        contradictions=tuple(contradictions),
        provenance={"source": "sanitized-relation-fixture"},
    )
    result.semantic_table = replace(semantic, relations=(relation,))
    return result


def _projected_review_result(result):
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    presented = [
        SpecificationRowAssembler().build_semantic_row(58, row)
        for row in projected
    ]
    presented = json.loads(
        json.dumps(
            presented,
            ensure_ascii=False,
            default=lambda value: dict(value) if isinstance(value, Mapping) else value,
        )
    )
    review = next(row for row in presented if row.get("source_row_index") == 1)
    parent = next(row for row in presented if row.get("source_row_index") == 2)
    return {
        "document_fingerprint": "b" * 64,
        "rows": presented,
        "page_statuses": {},
        "errors": [],
    }, review, parent


def test_e1_existing_semantic_relation_is_projected_to_review_dto():
    result = _relation_result()
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("source_row_index") == 1)
    evidence = review.metadata.get("continuation_evidence")
    assert evidence["relation_type"] == "CONTINUATION_OF"
    assert evidence["state"] == "AMBIGUOUS"
    assert evidence["evidence_strength"] == "SUPPORTING"


def test_e2_projected_parent_refs_equal_relation_candidate_target_refs():
    result = _relation_result()
    relation = result.semantic_table.relations[0]
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("source_row_index") == 1)
    assert review.metadata["continuation_evidence"]["candidate_parent_physical_refs"] == [
        relation.candidate_target_refs[0].as_dict()
    ]


def test_e3_no_semantic_relation_means_no_continuation_candidate():
    result = _unresolve_first_item(_semantic_result())
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("source_row_index") == 1)
    assert "continuation_evidence" not in review.metadata


def test_e4_contradicted_relation_is_not_human_actionable():
    result = _relation_result(contradictions=("parent_not_confirmed_item_root",))
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("source_row_index") == 1)
    assert "continuation_evidence" not in review.metadata


def test_e5_projection_preserves_existing_relation_state():
    result = _relation_result(state=RowRelationState.UNRESOLVED)
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("source_row_index") == 1)
    assert review.metadata["continuation_evidence"]["state"] == "UNRESOLVED"


def test_e6_review_parent_selection_has_no_nearest_row_fallback():
    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    assert "sourceRowIndex(candidate) < index" not in source
    assert "candidate_parent_physical_refs" in source


def test_e7_human_service_accepts_a_projected_bounded_parent():
    result, review, parent = _projected_review_result(_relation_result())
    service = HumanReviewService()
    fragment = review["value_candidates"]["name"]["value_candidate"]
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=review["physical_row_refs"],
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value=fragment,
        target={"parent_physical_refs": parent["physical_row_refs"]},
    )
    updated = service.apply_decision(result, decision)
    updated_parent = next(row for row in updated["rows"] if row.get("source_row_index") == 2)
    updated_child = next(row for row in updated["rows"] if row.get("source_row_index") == 1)
    assert fragment in updated_parent["name"]
    assert updated_child["row_type"] == "skip"


def test_e8_non_projected_parent_remains_rejected():
    result, review, _parent = _projected_review_result(_relation_result())
    service = HumanReviewService()
    fragment = review["value_candidates"]["name"]["value_candidate"]
    with pytest.raises(ValueError, match="Родитель продолжения"):
        service.create_decision(
            result,
            document_fingerprint=result["document_fingerprint"],
            page=58,
            physical_refs=review["physical_row_refs"],
            decision=RELATION_DECISION,
            relation="human_confirmed_continuation",
            candidate_value=fragment,
            target={"parent_physical_refs": [{"table": {"page_number": 58}, "row_index": 999}]},
        )


def test_e10_projection_and_review_leave_raw_physical_evidence_unchanged():
    result, review, parent = _projected_review_result(_relation_result())
    before = deepcopy(review["ocr_metadata"])
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=review["physical_row_refs"],
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value=review["value_candidates"]["name"]["value_candidate"],
        target={"parent_physical_refs": parent["physical_row_refs"]},
    )
    updated = service.apply_decision(result, decision)
    updated_review = next(row for row in updated["rows"] if row.get("source_row_index") == 1)
    assert updated_review["ocr_metadata"] == before


def test_e11_projected_relation_cannot_compose_numeric_fields():
    result, review, parent = _projected_review_result(_relation_result())
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=review["physical_row_refs"],
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value=review["value_candidates"]["name"]["value_candidate"],
        target={"parent_physical_refs": parent["physical_row_refs"]},
    )
    updated = service.apply_decision(result, decision)
    updated_parent = next(row for row in updated["rows"] if row.get("source_row_index") == 2)
    assert updated_parent["quantity"] == "2"
    assert updated_parent["unit"] == parent["unit"]


def test_e12_changed_projected_relation_evidence_invalidates_saved_decision():
    result, review, parent = _projected_review_result(_relation_result())
    service = HumanReviewService()
    fragment = review["value_candidates"]["name"]["value_candidate"]
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=review["physical_row_refs"],
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value=fragment,
        target={"parent_physical_refs": parent["physical_row_refs"]},
    )
    changed = deepcopy(result)
    changed_review = next(row for row in changed["rows"] if row.get("source_row_index") == 1)
    changed_review["ocr_metadata"]["continuation_evidence"]["state"] = "REJECTED"
    updated = service.apply_decision(changed, decision)
    updated_parent = next(row for row in updated["rows"] if row.get("source_row_index") == 2)
    assert fragment not in updated_parent.get("name", "")


def test_d17_optional_blank_mass_does_not_review_semantic_item():
    row = _assembled_semantic_row(_semantic_result())
    assert row.get("mass", "") == ""
    assert row["ocr_metadata"]["semantic_required_critical_fields"] == [
        "quantity", "unit"
    ]
    assert "mass" not in row["critical_fields"]
    assert "critical_value_missing" not in row["review_reasons"]
    assert row["status"] == "recognized"


def test_d18_missing_required_quantity_reviews_and_blocks():
    result = _semantic_result()
    item = result.semantic_table.logical_items[0]
    _with_semantic_item(
        result,
        quantity=replace(item.fields["quantity"], canonical_text=None),
    )
    row = _assembled_semantic_row(result)
    assert "quantity" in row["critical_fields"]
    assert "critical_value_missing" in row["review_reasons"]
    assert "critical_value_missing" in critical_blockers_for_row(row)


def test_d19_missing_required_unit_reviews_and_blocks():
    result = _semantic_result()
    item = result.semantic_table.logical_items[0]
    _with_semantic_item(
        result,
        unit=replace(item.fields["unit"], canonical_text=None),
    )
    row = _assembled_semantic_row(result)
    assert "unit" in row["critical_fields"]
    assert "critical_value_missing" in row["review_reasons"]
    assert "critical_value_missing" in critical_blockers_for_row(row)


def test_s4_mass_candidate_evidence_makes_missing_mass_reviewable():
    result = _semantic_result()
    semantic = result.semantic_table
    item = semantic.logical_items[0]
    fields = dict(item.fields)
    fields["mass"] = LogicalFieldValue(
        field="mass",
        canonical_text=None,
        candidates=(ValueCandidate(value="4"),),
    )
    result.semantic_table = replace(
        semantic,
        logical_items=(replace(item, fields=fields), *semantic.logical_items[1:]),
    )
    row = _assembled_semantic_row(result)
    assert "mass" in row["ocr_metadata"]["semantic_required_critical_fields"]
    assert "mass" in row["critical_fields"]
    assert "critical_value_missing" in row["review_reasons"]


def test_d20_source_structural_review_reason_survives_projection():
    result = _semantic_result()
    result.rows[0].metadata.update({
        "review_reasons": ["word_assignment_ambiguity"],
        "word_assignment_ambiguity": True,
    })
    row = _assembled_semantic_row(result)
    assert "word_assignment_ambiguity" in row["review_reasons"]
    assert "word_assignment_ambiguity" in critical_blockers_for_row(row)
    assert row["status"] == "review"


def test_d21_no_confidence_does_not_survive_as_semantic_blocker():
    result = _semantic_result()
    result.rows[0].metadata.update({
        "review_reasons": ["no_confidence"],
        "no_confidence": True,
    })
    row = _assembled_semantic_row(result)
    assert "no_confidence" not in row["review_reasons"]
    assert row["status"] == "recognized"


def test_d22_unresolved_raw_values_are_candidates_and_preview_only():
    result = _unresolve_first_item(_semantic_result())
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("semantic_review"))
    assert review.values == {}
    assert review.metadata["semantic_review_preview"]
    assert review.metadata["value_candidates"]
    assert all(
        candidate["auto_trusted"] is False
        for candidate in review.metadata["value_candidates"].values()
    )
    presented = SpecificationRowAssembler().build_semantic_row(1, review)
    assert presented["row_type"] == "semantic_review"
    assert presented.get("name", "") == ""
    assert presented["semantic_review_preview"]


def test_d23_p12_raw_17_is_review_evidence_not_canonical_quantity():
    result = _semantic_result()
    table = result.physical_table
    first_row = table.rows[1]
    quantity_cell = next(
        cell for cell in first_row.cells if cell.ref.column_index == 6
    )
    updated_row = replace(
        first_row,
        cells=tuple(
            replace(cell, raw_text="17") if cell is quantity_cell else cell
            for cell in first_row.cells
        ),
    )
    result.physical_table = replace(
        table,
        rows=tuple(
            updated_row if row.ref.row_index == first_row.ref.row_index else row
            for row in table.rows
        ),
    )
    _unresolve_first_item(result)
    projected = _pilot_provider()._apply_semantic_projection(result, result.diagnostics)
    review = next(row for row in projected if row.metadata.get("semantic_review"))
    assert review.values == {}
    candidate = review.metadata["value_candidates"]["quantity"]
    assert candidate["raw_value"] == "17"
    assert candidate["value_candidate"] == normalize_cell("quantity", "17")
    presented = SpecificationRowAssembler().build_semantic_row(1, review)
    assert presented.get("quantity", "") == ""
    assert presented["semantic_review_preview"]
    assert presented["status"] == "review"


def test_d24_blank_optional_mass_does_not_create_false_review():
    row = _assembled_semantic_row(_semantic_result())
    refresh_review_state(row)
    assert row.get("mass", "") == ""
    assert row["critical_fields"] == []
    assert row["status"] == "recognized"


def test_d25_legacy_review_policy_remains_unchanged():
    legacy = {
        "name": "Насос",
        "row_type": "item",
        "unit": "шт.",
        "quantity": "1",
        "mass": "",
        "ocr_metadata": {"provider": "yandex_vision"},
    }
    assert missing_critical_fields(legacy) == ["mass"]
    tesseract = {
        **legacy,
        "ocr_metadata": {"provider": "tesseract"},
    }
    assert missing_critical_fields(tesseract) == []
