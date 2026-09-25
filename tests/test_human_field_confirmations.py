from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from averon_import.services.export_service import ExcelExportService
from averon_import.services.review_decisions import (
    CONFIRM_FIELD_ABSENT_DECISION,
    CONFIRM_FIELD_VALUE_DECISION,
    HumanReviewService,
    ReviewDecisionStore,
)
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    missing_critical_fields,
    refresh_review_state,
    row_requires_review,
)
from averon_import.services.sourcing.service import _trusted_quantity


def _refs(index: int = 4) -> list[dict]:
    return [{"table": {"page_number": 1, "table_index": 0}, "row_index": index}]


def _row(*, quantity: str = "87", unit: str = "м2", mass: str = "2") -> dict:
    refs = _refs()
    return {
        "id": "pilot-quantity-row",
        "page": 1,
        "row_type": "item",
        "status": "review",
        "name": "Воздуховод",
        "unit": unit,
        "quantity": quantity,
        "mass": mass,
        "selected": True,
        "physical_row_refs": refs,
        "review_reasons": ["numeric_suspect"],
        "critical_blockers": ["numeric_suspect"],
        "ocr_metadata": {
            "provider": "yandex_vision",
            "physical_row_refs": refs,
            "raw_physical_cells": [{"row_index": 4, "raw_text": quantity}],
            "normalization": {"quantity": {"numeric_suspect": True}} if quantity else {},
        },
    }


def _result(row: dict) -> dict:
    return {
        "document_fingerprint": "d" * 64,
        "rows": [row],
        "page_statuses": {
            "1": {
                "page": 1,
                "layout_status": "TRUSTED",
                "schema_status": "SUPPORTED",
                "page_disposition": "SPEC_OUTPUT",
                "output_status": "REVIEW_REQUIRED",
                "blockers": ["numeric_suspect", "structural_layout_ambiguous"],
                "diagnostics": {
                    "selected_mode": "geometry_first",
                    "page_disposition": {"disposition": "SPEC_OUTPUT"},
                },
            }
        },
        "summary": {},
        "errors": [],
    }


def _decision(service, result, decision, **kwargs):
    return service.create_decision(
        result,
        document_fingerprint=result["document_fingerprint"],
        page=1,
        physical_refs=_refs(),
        decision=decision,
        **kwargs,
    )


def test_confirm_current_value_resolves_only_its_numeric_blockers_and_is_incremental():
    result = _result(_row())
    raw_evidence = deepcopy(result["rows"][0]["ocr_metadata"])
    result["rows"][0]["review_reasons"].append("ambiguous_table_schema")
    result["rows"][0]["critical_blockers"].append("ambiguous_table_schema")
    service = HumanReviewService()
    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )

    updated, changed_rows, pages, applied = service.apply_decision_incremental(result, decision)
    row = updated["rows"][0]

    assert applied is True
    assert row["quantity"] == "87"
    assert row["human_verified_fields"] == ["quantity"]
    value_record = row["human_verified_field_values"]["quantity"]
    assert value_record["value"] == "87"
    assert value_record["decision"] == CONFIRM_FIELD_VALUE_DECISION
    assert value_record["decision_key"] == decision.decision_key
    assert value_record["decision_id"] == decision.decision_id
    assert value_record["evidence_fingerprint"] == decision.evidence_fingerprint
    assert value_record["provenance"] == "human"
    assert "numeric_suspect" not in critical_blockers_for_row(row)
    assert "ambiguous_table_schema" in critical_blockers_for_row(row)
    assert "structural_layout_ambiguous" in updated["page_statuses"]["1"]["blockers"]
    assert row["ocr_metadata"] == raw_evidence
    assert changed_rows == [row]
    assert pages == {1}
    assert service.metrics["review_rows_detached"] == 1
    assert service.metrics["page_safety_pages_recalculated"] == 1
    assert service.metrics["full_result_copies"] == 0


@pytest.mark.parametrize(
    ("normalization", "reason"),
    [
        ({"numeric_suspect": True}, "numeric_suspect"),
        ({"non_scalar": True, "numeric_shape": "NON_SCALAR_SLASH"}, "numeric_non_scalar"),
        ({"integer_like_decimal": True}, "numeric_shape_suspect"),
    ],
)
def test_value_confirmation_resolves_each_supported_field_numeric_blocker(
    normalization, reason
):
    result = _result(_row())
    row = result["rows"][0]
    row["review_reasons"] = [reason]
    row["critical_blockers"] = [reason]
    row["ocr_metadata"]["normalization"]["quantity"] = normalization
    service = HumanReviewService()
    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )

    updated, _, _, applied = service.apply_decision_incremental(result, decision)

    assert applied
    assert reason not in critical_blockers_for_row(updated["rows"][0])


def test_value_confirmation_keeps_other_field_conflicts_and_identity_blockers():
    result = _result(_row())
    row = result["rows"][0]
    row["review_reasons"].extend([
        "secondary_conflict",
        "identity_cell_missing",
        "physical_row_unresolved",
    ])
    row["critical_blockers"].extend([
        "secondary_conflict",
        "identity_cell_missing",
        "physical_row_unresolved",
    ])
    row["secondary_conflict_fields"] = ["mass"]
    row["value_candidates"] = {
        "mass": {"value_candidate": "3", "review_reason": "secondary_conflict"}
    }
    row["ocr_metadata"]["identity_cell_missing"] = True
    service = HumanReviewService()
    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )

    updated, _, _, applied = service.apply_decision_incremental(result, decision)

    assert applied
    blockers = critical_blockers_for_row(updated["rows"][0])
    assert "secondary_conflict" in blockers
    assert "identity_cell_missing" in blockers
    assert "physical_row_unresolved" in blockers
    assert "numeric_suspect" not in blockers


def test_confirm_value_rejects_arbitrary_or_stale_values_without_mutation():
    result = _result(_row())
    service = HumanReviewService()
    before = deepcopy(result)
    with pytest.raises(ValueError, match="не совпадает"):
        _decision(
            service,
            result,
            CONFIRM_FIELD_VALUE_DECISION,
            field="quantity",
            confirmed_value="88",
        )
    assert result == before

    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )
    changed = deepcopy(result)
    changed["rows"][0]["quantity"] = "88"
    refresh_review_state(changed["rows"][0])
    updated, changed_rows, _, applied = service.apply_decision_incremental(changed, decision)
    assert applied is False
    assert changed_rows == []
    assert updated["rows"][0]["quantity"] == "88"
    assert "numeric_suspect" in critical_blockers_for_row(updated["rows"][0])


def test_stale_ui_value_confirmation_returns_400_without_writing_result_or_ledger(
    monkeypatch, tmp_path
):
    from averon_import import main
    from averon_import.services.workspace import WorkspaceService

    workspace_service = WorkspaceService(tmp_path)
    document_id = "b" * 32
    (workspace_service.documents_dir / document_id).mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    fingerprint = "e" * 64
    workspace_service.write_json(workspace.metadata_path, {
        "document_id": document_id,
        "source_sha256": fingerprint,
    })
    canonical = _result(_row())
    canonical["document_fingerprint"] = fingerprint
    canonical["revision"] = 0
    canonical["review_ledger_revision"] = 0
    workspace_service.write_json(workspace.result_path, canonical)
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    request = main.ReviewDecisionRequest(
        page=1,
        physical_refs=_refs(),
        decision=CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="88",
    )

    with pytest.raises(HTTPException) as error:
        main.save_review_decision(document_id, request)

    assert error.value.status_code == 400
    assert workspace_service.read_json(workspace.result_path) == canonical
    assert not workspace.review_decisions_path.exists()


def test_value_confirmation_is_idempotent_and_invalidated_by_new_ocr_evidence(tmp_path):
    result = _result(_row())
    service = HumanReviewService()
    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )
    once, _, _, applied_once = service.apply_decision_incremental(result, decision)
    twice, changed_rows, _, applied_twice = service.apply_decision_incremental(once, decision)
    assert applied_once and applied_twice
    assert changed_rows == []
    assert len(twice["rows"][0]["human_verified_field_values"]) == 1
    assert twice["rows"][0]["quantity"] == "87"

    ledger = ReviewDecisionStore(tmp_path / "review_decisions.json")
    ledger.upsert(decision)
    ledger.upsert(decision)
    assert ledger.load_snapshot()[1] == 1
    assert len(ledger.load()) == 1
    restored = ledger.load()[0]
    replayed_same_evidence = service.apply_saved_decisions(
        result, [restored], result["document_fingerprint"]
    )
    assert replayed_same_evidence["rows"][0]["human_verified_field_values"]["quantity"]["value"] == "87"

    changed_evidence = deepcopy(result)
    changed_evidence["rows"][0]["ocr_metadata"]["raw_physical_cells"][0]["raw_text"] = "87,0"
    replayed = service.apply_saved_decisions(
        changed_evidence, [decision], result["document_fingerprint"]
    )
    assert "human_verified_field_values" not in replayed["rows"][0]
    assert "numeric_suspect" in critical_blockers_for_row(replayed["rows"][0])


def test_changed_canonical_value_permanently_invalidates_prior_confirmation():
    result = _result(_row())
    service = HumanReviewService()
    decision = _decision(
        service,
        result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )
    confirmed, _, _, applied = service.apply_decision_incremental(result, decision)
    assert applied

    changed = deepcopy(confirmed["rows"][0])
    changed["quantity"] = "88"
    changed["edited_fields"] = ["quantity"]
    refresh_review_state(changed)
    assert changed["human_verified_field_values"]["quantity"]["invalidated"] is True
    assert "numeric_suspect" in critical_blockers_for_row(changed)
    assert changed["status"] == "review"

    changed["quantity"] = "87"
    refresh_review_state(changed)
    assert changed["human_verified_field_values"]["quantity"]["invalidated"] is True
    assert "numeric_suspect" in critical_blockers_for_row(changed)

    reconfirm_result = _result(changed)
    reconfirm = _decision(
        service,
        reconfirm_result,
        CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )
    reverified, _, _, reapplied = service.apply_decision_incremental(
        reconfirm_result, reconfirm
    )
    assert reapplied
    assert reconfirm.decision_key != decision.decision_key
    assert not reverified["rows"][0]["human_verified_field_values"]["quantity"].get("invalidated")
    assert "numeric_suspect" not in critical_blockers_for_row(reverified["rows"][0])


def test_confirm_absence_keeps_field_blank_and_is_idempotent():
    result = _result(_row(quantity=""))
    service = HumanReviewService()
    decision = _decision(service, result, CONFIRM_FIELD_ABSENT_DECISION, field="quantity")

    once, _, _, applied_once = service.apply_decision_incremental(result, decision)
    twice, changed_rows, _, applied_twice = service.apply_decision_incremental(once, decision)
    row = twice["rows"][0]

    assert applied_once and applied_twice
    assert changed_rows == []
    assert row["quantity"] == ""
    absence_record = row["human_confirmed_absent_fields"]["quantity"]
    assert absence_record["decision"] == CONFIRM_FIELD_ABSENT_DECISION
    assert absence_record["decision_key"] == decision.decision_key
    assert absence_record["decision_id"] == decision.decision_id
    assert absence_record["evidence_fingerprint"] == decision.evidence_fingerprint
    assert absence_record["provenance"] == "human"
    assert "quantity" not in missing_critical_fields(row)
    assert "critical_value_missing" not in critical_blockers_for_row(row)

    changed_evidence = deepcopy(result)
    changed_evidence["rows"][0]["ocr_metadata"]["raw_physical_cells"][0]["raw_text"] = "количество не указано"
    replayed = service.apply_saved_decisions(
        changed_evidence, [decision], result["document_fingerprint"]
    )
    assert "quantity" not in replayed["rows"][0].get("human_confirmed_absent_fields", {})
    assert "quantity" in missing_critical_fields(replayed["rows"][0])


def test_absence_does_not_clear_other_missing_fields_and_is_invalidated_by_entry():
    result = _result(_row(quantity="", unit=""))
    service = HumanReviewService()
    decision = _decision(service, result, CONFIRM_FIELD_ABSENT_DECISION, field="quantity")
    updated, _, _, applied = service.apply_decision_incremental(result, decision)
    row = updated["rows"][0]
    assert applied
    assert row["quantity"] == ""
    assert "quantity" not in missing_critical_fields(row)
    assert "unit" in missing_critical_fields(row)
    assert "critical_value_missing" in critical_blockers_for_row(row)
    assert row_requires_review(row)

    row["quantity"] = "2"
    refresh_review_state(row)
    assert row["quantity"] == "2"
    assert row["human_confirmed_absent_fields"]["quantity"]["invalidated"] is True
    assert "quantity" not in missing_critical_fields(row)
    assert "critical_value_missing" in critical_blockers_for_row(row)  # unit remains missing

    row["quantity"] = ""
    refresh_review_state(row)
    assert "quantity" in missing_critical_fields(row)


def test_confirmed_blank_quantity_exports_blank_without_inventing_sourcing_total(tmp_path):
    result = _result(_row(quantity=""))
    service = HumanReviewService()
    decision = _decision(service, result, CONFIRM_FIELD_ABSENT_DECISION, field="quantity")
    updated, _, _, applied = service.apply_decision_incremental(result, decision)
    assert applied
    row = updated["rows"][0]
    assert not critical_blockers_for_row(row)

    target = tmp_path / "confirmed-absence.xlsx"
    ExcelExportService().export(
        [row],
        ["name", "unit", "quantity", "mass"],
        target,
        page_statuses={
            "1": {
                "page": 1,
                "output_status": "USABLE",
                "page_disposition": "SPEC_OUTPUT",
                "blockers": [],
            }
        },
    )
    sheet = load_workbook(target)["Спецификация"]
    assert sheet["C2"].value is None
    assert row["quantity"] == ""
    assert _trusted_quantity(row) is None


def test_confirmation_audit_and_ocr_evidence_cannot_be_forged_by_row_save():
    from averon_import.main import (
        ReviewDecisionRequest,
        _restore_server_owned_review_state,
    )

    canonical = _row()
    submitted = deepcopy(canonical)
    submitted["status"] = "verified"
    submitted["row_type"] = "skip"
    submitted["human_verified_fields"] = ["quantity"]
    submitted["human_verified_field_values"] = {
        "quantity": {
            "value": "87",
            "decision_key": "forged",
            "evidence_fingerprint": "forged",
            "provenance": "human",
        }
    }
    submitted["ocr_metadata"] = {}
    submitted["critical_blockers"] = []
    submitted["review_reasons"] = []

    restored = _restore_server_owned_review_state([submitted], [canonical])[0]

    assert restored["status"] == "review"
    assert restored["row_type"] == "item"
    assert "human_verified_field_values" not in restored
    assert restored["ocr_metadata"] == canonical["ocr_metadata"]
    assert "numeric_suspect" in restored["critical_blockers"]
    request = ReviewDecisionRequest(
        page=1,
        physical_refs=_refs(),
        decision=CONFIRM_FIELD_VALUE_DECISION,
        field="quantity",
        confirmed_value="87",
    )
    assert request.confirmed_value == "87"


def test_human_field_confirmation_controls_are_field_scoped_in_ui():
    from pathlib import Path

    app_js = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    cell_html = app_js.split("function cellHtml(row, key)", 1)[1].split("\nfunction options(", 1)[0]
    table_events = app_js.split("function ensureResultTableEvents()", 1)[1].split("\nfunction renderPatchedReviewRows(", 1)[0]
    pilot_row = _row(quantity="87", unit="м2")
    pilot_row["value_candidates"] = {}
    assert pilot_row["quantity"] == "87"
    assert pilot_row["unit"] == "м2"
    assert pilot_row["ocr_metadata"]["normalization"]["quantity"]["numeric_suspect"] is True
    assert pilot_row["value_candidates"] == {}
    assert 'decision: "CONFIRM_FIELD_VALUE"' in app_js
    assert 'decision: "CONFIRM_FIELD_ABSENT"' in app_js
    assert "Подтвердить ${escapeHtml(value)}" in app_js
    assert "В исходнике отсутствует" in app_js
    assert "Проверено пользователем ✓" in app_js
    assert "Отсутствие подтверждено ✓" in app_js
    assert 'class="field-confirm-value"' in app_js
    assert 'class="field-confirm-absent"' in app_js
    assert 'else if (missing)' in cell_html
    assert 'В исходнике отсутствует' in cell_html and '>Ввести</button>' in cell_html
    assert 'Подтвердить ${escapeHtml(value)}' in cell_html
    assert 'button.matches(".field-confirm-value")' in table_events
    assert 'button.matches(".field-confirm-absent")' in table_events
    assert "Подтвердить всю строку" not in app_js
