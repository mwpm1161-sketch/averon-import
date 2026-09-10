from __future__ import annotations

from copy import deepcopy

import pytest

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.page_disposition import (
    POSSIBLE_SPEC_UNRESOLVED,
    SPEC_OUTPUT,
    page_disposition_from_scene,
)
from averon_import.services.review_decisions import (
    FIELD_DECISION,
    RELATION_DECISION,
    HumanReviewService,
    ReviewDecisionStore,
)


def _refs(index: int) -> list[dict]:
    return [{"table": {"page_number": 58, "table_index": 0}, "row_index": index}]


def _row_refs(row: dict) -> list[dict]:
    return row.get("physical_row_refs") or row.get("ocr_metadata", {}).get("physical_row_refs") or []


def _row(index: int, *, row_type: str = "item", quantity: str = "") -> dict:
    refs = _refs(index)
    return {
        "id": f"row-{index}",
        "page": 58,
        "row_type": row_type,
        "status": "recognized" if quantity else "review",
        "name": f"Насос {index}" if row_type != "semantic_review" else "",
        "unit": "шт." if row_type != "semantic_review" else "",
        "quantity": quantity,
        "mass": "",
        "selected": True,
        "physical_row_refs": refs,
        "ocr_metadata": {
            "physical_row_refs": refs,
            "raw_physical_cells": [{"row_index": index, "raw_text": f"raw-{index}"}],
        },
    }


def _result() -> dict:
    rows = [_row(10), _row(20, quantity="2")]
    rows[0]["value_candidates"] = {
        "quantity": {
            "value_candidate": "4",
            "raw_value": "4",
            "auto_trusted": False,
            "candidate_source": "yandex_exact_cell_2x",
            "review_reason": "recovered_by_exact_cell_ocr",
        }
    }
    rows[0]["ocr_metadata"]["value_candidates"] = rows[0]["value_candidates"]
    rows.extend([_row(11, row_type="semantic_review"), _row(12, row_type="semantic_review")])
    for row in rows[2:]:
        row["semantic_review"] = True
        row["semantic_state"] = "REVIEW"
        row["semantic_review_preview"] = f"Фрагмент {row['physical_row_refs'][0]['row_index']}"
        row["continuation_evidence"] = {
            "candidate_parent_physical_refs": _refs(10),
        }
        row["value_candidates"] = {
            "name": {
                "value_candidate": row["semantic_review_preview"],
                "auto_trusted": False,
            }
        }
        row["ocr_metadata"]["semantic_review_preview"] = row["semantic_review_preview"]
        row["ocr_metadata"]["value_candidates"] = row["value_candidates"]
    status = {
        "page": 58,
        "layout_status": "TRUSTED",
        "schema_status": "SUPPORTED",
        "page_disposition": SPEC_OUTPUT,
        "output_status": "REVIEW_REQUIRED",
        "blockers": ["critical_value_missing", "physical_row_semantics_unresolved"],
        "diagnostics": {
            "selected_mode": "geometry_first",
            "page_disposition": {"disposition": SPEC_OUTPUT},
            "semantic_critical_value_missing_count": 1,
            "semantic_output_critical_unresolved_count": 2,
            "semantic_review_item_count": 2,
            "semantic_review_evidence_row_count": 2,
            "unresolved_physical_row_count": 2,
        },
    }
    return {
        "document_fingerprint": "a" * 64,
        "rows": rows,
        "page_statuses": {"58": status},
        "errors": [],
    }


class _Scene:
    def __init__(self, candidate):
        self.region_candidates = (candidate,)


class _Candidate:
    schema_status = "supported"
    physical_status = "TRUSTED"
    rejection_reasons = ()
    ref = "region:spec"
    schema_evidence = {
        "family": {"family": "SUPPORTED_SPECIFICATION", "status": "MATCHED"},
        "profile_match": {
            "selected_profile": {"production_authoritative": True}
        },
    }


def test_d1_authoritative_route_wins_over_shadow_disagreement():
    decision = page_disposition_from_scene(
        _Scene(_Candidate()),
        arbitration={"can_activate": False, "reasons": ["raster_vector_conflict"]},
        authoritative={
            "selected_mode": "geometry_first",
            "schema": {"status": "supported"},
            "geometry_grid": {"high_confidence": True},
            "profile_match": {
                "selected_profile": {"production_authoritative": True}
            },
            "structural_evidence": {"material_disagreement": False},
        },
    )
    assert decision.disposition == SPEC_OUTPUT
    assert "authoritative_production_route" in decision.reasons


def test_d2_shadow_cannot_upgrade_unsafe_authoritative_route():
    decision = page_disposition_from_scene(
        _Scene(_Candidate()),
        arbitration={"can_activate": False},
        authoritative={
            "selected_mode": "geometry_first",
            "schema": {"status": "supported"},
            "geometry_grid": {"high_confidence": True},
            "physical_row_loss_suspected": True,
        },
    )
    assert decision.disposition == POSSIBLE_SPEC_UNRESOLVED


def test_d2a_authoritative_route_owns_disposition_without_supported_shadow_candidate():
    decision = page_disposition_from_scene(
        _Scene(None),
        arbitration={"can_activate": False, "reasons": ["shadow_has_no_supported_candidate"]},
        authoritative={
            "selected_mode": "geometry_first",
            "schema": {"status": "supported"},
            "geometry_grid": {"high_confidence": True},
            "profile_match": {
                "selected_profile": {"production_authoritative": True}
            },
            "structural_evidence": {"material_disagreement": False},
        },
    )
    assert decision.disposition == SPEC_OUTPUT


def test_d2b_shadow_supported_candidate_cannot_upgrade_profile_shadow_only_route():
    shadow_only = _Candidate()
    shadow_only.schema_evidence = {
        "family": {"family": "SUPPORTED_SPECIFICATION", "status": "MATCHED"},
        "profile_match": {
            "selected_profile": {"production_authoritative": False}
        },
    }
    decision = page_disposition_from_scene(
        _Scene(shadow_only),
        arbitration={"can_activate": True},
        authoritative={
            "selected_mode": "geometry_first",
            "schema": {"status": "supported"},
            "geometry_grid": {"high_confidence": True},
            "profile_match": {
                "selected_profile": {"production_authoritative": False}
            },
        },
    )
    assert decision.disposition != SPEC_OUTPUT


def test_d3_field_decision_is_typed_and_raw_evidence_stays_immutable():
    result = _result()
    before = deepcopy(result["rows"][0]["ocr_metadata"])
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(10),
        decision=FIELD_DECISION,
        field="quantity",
        candidate_value="4",
    )
    assert decision.provenance == "human"
    updated = service.apply_decision(result, decision)
    row = updated["rows"][0]
    assert row["quantity"] == "4"
    assert row["verification_state"] == "HUMAN_VERIFIED"
    assert row["value_candidates"]["quantity"]["auto_trusted"] is False
    assert row["ocr_metadata"] == before


def test_d4_field_acceptance_recomputes_page_safety_and_strict_export(tmp_path):
    result = _result()
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(10),
        decision=FIELD_DECISION,
        field="quantity",
        candidate_value="4",
    )
    updated = service.apply_decision(result, decision)
    assert "critical_value_missing" not in updated["page_statuses"]["58"]["blockers"]
    assert "physical_row_semantics_unresolved" in updated["page_statuses"]["58"]["blockers"]


def test_d5_rejected_candidate_leaves_review_and_does_not_change_canonical():
    result = _result()
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(10),
        decision="REJECT_CANDIDATE",
        field="quantity",
        candidate_value="4",
    )
    updated = service.apply_decision(result, decision)
    assert updated["rows"][0]["quantity"] == ""
    assert updated["rows"][0]["human_rejected_candidates"][0]["provenance"] == "human"
    assert updated["page_statuses"]["58"]["output_status"] == "REVIEW_REQUIRED"


def test_d6_continuation_relation_requires_parent_and_never_composes_numeric_fields():
    result = _result()
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(11),
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value="Фрагмент 11",
        target={"parent_physical_refs": _refs(10)},
    )
    updated = service.apply_decision(result, decision)
    parent = updated["rows"][0]
    child = updated["rows"][2]
    assert "Фрагмент 11" in parent["name"]
    assert child["row_type"] == "skip"
    assert parent["quantity"] == ""


def test_d6a_arbitrary_continuation_text_is_rejected():
    result = _result()
    with pytest.raises(ValueError, match="Текст продолжения"):
        HumanReviewService().create_decision(
            result,
            document_fingerprint=result["document_fingerprint"],
            page=58,
            physical_refs=_refs(11),
            decision=RELATION_DECISION,
            relation="human_confirmed_continuation",
            candidate_value="Не было в OCR",
            target={"parent_physical_refs": _refs(10)},
        )


def test_d6b_arbitrary_parent_is_rejected_against_bounded_evidence():
    result = _result()
    with pytest.raises(ValueError, match="Родитель продолжения"):
        HumanReviewService().create_decision(
            result,
            document_fingerprint=result["document_fingerprint"],
            page=58,
            physical_refs=_refs(11),
            decision=RELATION_DECISION,
            relation="human_confirmed_continuation",
            candidate_value="Фрагмент 11",
            target={"parent_physical_refs": _refs(20)},
        )


def test_d6c_relation_without_candidate_parent_evidence_fails_closed():
    result = _result()
    result["rows"][2].pop("continuation_evidence")
    with pytest.raises(ValueError, match="bounded candidate-parent evidence"):
        HumanReviewService().create_decision(
            result,
            document_fingerprint=result["document_fingerprint"],
            page=58,
            physical_refs=_refs(11),
            decision=RELATION_DECISION,
            relation="human_confirmed_continuation",
            candidate_value="Фрагмент 11",
            target={"parent_physical_refs": _refs(10)},
        )


def test_d6d_valid_relation_keeps_raw_evidence_and_does_not_concatenate_numeric_fields():
    result = _result()
    before = deepcopy(result["rows"][2]["ocr_metadata"])
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(12),
        decision=RELATION_DECISION,
        relation="human_confirmed_continuation",
        candidate_value="Фрагмент 12",
        target={"parent_physical_refs": _refs(10)},
    )
    updated = service.apply_decision(result, decision)
    assert updated["rows"][0]["quantity"] == ""
    assert updated["rows"][0]["unit"] == "шт."
    assert updated["rows"][2]["ocr_metadata"] == before


def test_d7_decisions_persist_by_document_and_evidence_fingerprint(tmp_path):
    result = _result()
    service = HumanReviewService()
    decision = service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=58,
        physical_refs=_refs(10),
        decision=FIELD_DECISION,
        field="quantity",
        candidate_value="4",
    )
    store = ReviewDecisionStore(tmp_path / "review_decisions.json")
    store.upsert(decision)
    restored = service.apply_saved_decisions(result, store.load(), result["document_fingerprint"])
    assert restored["rows"][0]["quantity"] == "4"
    changed = deepcopy(result)
    changed["rows"][0]["value_candidates"]["quantity"]["value_candidate"] = "9"
    changed["rows"][0]["ocr_metadata"]["value_candidates"]["quantity"]["value_candidate"] = "9"
    stale = service.apply_saved_decisions(changed, store.load(), result["document_fingerprint"])
    assert stale["rows"][0]["quantity"] == ""


def test_d8_strict_export_rejects_unresolved_human_review(tmp_path):
    result = _result()
    try:
        ExcelExportService().export(
            result["rows"], ["name", "unit", "quantity"], tmp_path / "review.xlsx",
            page_statuses=result["page_statuses"],
        )
    except ValueError as error:
        assert "Экспорт заблокирован" in str(error)
    else:
        (tmp_path / "review.xlsx").unlink(missing_ok=True)
        raise AssertionError("unresolved review must block strict export")


def test_d9_p58_style_all_decisions_restore_19_items_and_allow_strict_export(tmp_path):
    result = _result()
    result["rows"].append(_row(13, row_type="semantic_review"))
    child = result["rows"][-1]
    child["semantic_review"] = True
    child["semantic_state"] = "REVIEW"
    child["semantic_review_preview"] = "Фрагмент 13"
    child["continuation_evidence"] = {
        "candidate_parent_physical_refs": _refs(10),
    }
    child["value_candidates"] = {"name": {"value_candidate": "Фрагмент 13", "auto_trusted": False}}
    child["ocr_metadata"]["semantic_review_preview"] = "Фрагмент 13"
    child["ocr_metadata"]["value_candidates"] = child["value_candidates"]
    extra = [_row(100 + index, quantity="1") for index in range(17)]
    for row in extra:
        row["mass"] = "1"
    result["rows"].extend(extra)
    second = result["rows"][1]
    second["quantity"] = ""
    second["status"] = "review"
    second["value_candidates"] = {
        "quantity": {
            "value_candidate": "3",
            "raw_value": "3",
            "auto_trusted": False,
            "candidate_source": "yandex_exact_cell_2x",
            "review_reason": "recovered_by_exact_cell_ocr",
        }
    }
    second["ocr_metadata"]["value_candidates"] = second["value_candidates"]
    service = HumanReviewService()
    decisions = []
    for row in result["rows"]:
        candidate = (row.get("value_candidates") or {}).get("quantity")
        if candidate:
            decisions.append(service.create_decision(
                result,
                document_fingerprint=result["document_fingerprint"],
                page=58,
                physical_refs=_row_refs(row),
                decision=FIELD_DECISION,
                field="quantity",
                candidate_value=candidate["value_candidate"],
            ))
    children = [row for row in result["rows"] if row.get("row_type") == "semantic_review"]
    for row in children:
        decisions.append(service.create_decision(
            result,
            document_fingerprint=result["document_fingerprint"],
            page=58,
            physical_refs=_row_refs(row),
            decision=RELATION_DECISION,
            relation="human_confirmed_continuation",
            candidate_value=row["semantic_review_preview"],
            target={"parent_physical_refs": _row_refs(result["rows"][0])},
        ))
    updated = result
    for decision in decisions:
        updated = service.apply_decision(updated, decision)
    page_rows = [row for row in updated["rows"] if row.get("page") == 58]
    assert sum(row.get("row_type") in {"item", "component", "item_candidate"} for row in page_rows) == 19
    assert not [row for row in page_rows if row.get("row_type") == "semantic_review"]
    assert updated["page_statuses"]["58"]["output_status"] == "USABLE"
    ExcelExportService().export(
        page_rows,
        ["name", "unit", "quantity", "mass"],
        tmp_path / "p58.xlsx",
        page_statuses={"58": updated["page_statuses"]["58"]},
    )
