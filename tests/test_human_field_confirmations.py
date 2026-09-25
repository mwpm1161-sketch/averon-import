from __future__ import annotations

from copy import deepcopy
import json
import subprocess

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


def _pilot_refs(index: int) -> list[dict]:
    return [{"table": {"page_number": 25, "table_index": 0}, "row_index": index}]


def _pilot_shape_row(index: int = 17) -> dict:
    return {
        "id": "pilot-row",
        "page": 25,
        "row_type": "item",
        "status": "review",
        "name": "Воздуховод",
        "unit": "",
        "quantity": "",
        "mass": "",
        "physical_row_refs": _pilot_refs(index),
        "ocr_metadata": {"provider": "yandex_vision", "physical_row_refs": []},
        "value_candidates": {"unit": {"value_candidate": "шт."}},
        "review_reasons": ["critical_value_missing"],
        "critical_blockers": ["critical_value_missing"],
    }


def _pilot_shape_result(*rows: dict) -> dict:
    return {
        "document_fingerprint": "e" * 64,
        "revision": 10,
        "review_ledger_revision": 0,
        "rows": list(rows),
        "page_statuses": {
            "25": {
                "page": 25,
                "page_disposition": "SPEC_OUTPUT",
                "output_status": "REVIEW_REQUIRED",
                "blockers": [],
            }
        },
        "errors": [],
        "summary": {},
    }


def _install_pilot_shape_document(monkeypatch, tmp_path, result):
    from averon_import import main
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path)
    document_id = "c" * 32
    (service.documents_dir / document_id).mkdir()
    workspace = service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pilot pdf")
    service.write_json(workspace.metadata_path, {
        "document_id": document_id,
        "source_sha256": "e" * 64,
    })
    service.write_json(workspace.result_path, result)
    monkeypatch.setattr(main, "workspace_service", service)
    return main, service, document_id, workspace


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


@pytest.mark.parametrize(
    "decision",
    [
        "ACCEPT_FIELD_CANDIDATE",
        "REJECT_CANDIDATE",
        "CONFIRM_FIELD_VALUE",
        "CONFIRM_FIELD_ABSENT",
        "ACCEPT_CONTINUATION_RELATION",
    ],
)
def test_review_decisions_use_top_level_refs_when_metadata_refs_are_empty(
    monkeypatch, tmp_path, decision
):
    row = _pilot_shape_row()
    request_values = {"page": 25, "physical_refs": _pilot_refs(17), "decision": decision}
    if decision in {"ACCEPT_FIELD_CANDIDATE", "REJECT_CANDIDATE"}:
        request_values.update(field="unit", candidate_value="шт.")
    elif decision == "CONFIRM_FIELD_VALUE":
        row.update(quantity="87", value_candidates={})
        row["review_reasons"] = ["numeric_suspect"]
        row["critical_blockers"] = ["numeric_suspect"]
        row["ocr_metadata"]["normalization"] = {
            "quantity": {"numeric_suspect": True}
        }
        request_values.update(field="quantity", confirmed_value="87")
    elif decision == "CONFIRM_FIELD_ABSENT":
        row.update(unit="шт.", value_candidates={})
        request_values.update(field="quantity")
    else:
        parent = _pilot_shape_row(index=16)
        parent.update(id="pilot-parent", name="Родитель", quantity="1", value_candidates={})
        row.update(
            id="pilot-child",
            row_type="semantic_review",
            name="",
            value_candidates={"name": {"value_candidate": "Продолжение"}},
            semantic_review_preview="Продолжение",
            continuation_evidence={
                "candidate_parent_physical_refs": _pilot_refs(16),
                "candidate_value": "Продолжение",
            },
        )
        result = _pilot_shape_result(parent, row)
        request_values.update(
            physical_refs=_pilot_refs(17),
            relation="human_confirmed_continuation",
            candidate_value="Продолжение",
            target={"parent_physical_refs": _pilot_refs(16)},
        )

    if decision != "ACCEPT_CONTINUATION_RELATION":
        result = _pilot_shape_result(row)
    main, service, document_id, _workspace = _install_pilot_shape_document(
        monkeypatch, tmp_path, result
    )
    response = main.save_review_decision(
        document_id, main.ReviewDecisionRequest(**request_values)
    )

    assert response["saved"] is True
    patched = {item["id"]: item for item in response["result_patch"]["rows"]}
    if decision == "ACCEPT_FIELD_CANDIDATE":
        assert patched["pilot-row"]["unit"] == "шт."
        assert service.read_json(service.get(document_id).result_path)["rows"][0]["unit"] == "шт."
    elif decision == "REJECT_CANDIDATE":
        assert patched["pilot-row"]["human_rejected_candidates"][0]["candidate_value"] == "шт."
    elif decision == "CONFIRM_FIELD_VALUE":
        assert patched["pilot-row"]["human_verified_field_values"]["quantity"]["value"] == "87"
    elif decision == "CONFIRM_FIELD_ABSENT":
        assert patched["pilot-row"]["human_confirmed_absent_fields"]["quantity"]["provenance"] == "human"
    else:
        assert set(patched) == {"pilot-parent", "pilot-child"}
        assert "Продолжение" in patched["pilot-parent"]["name"]


def test_empty_review_refs_return_400_without_mutating_result_or_ledger(
    monkeypatch, tmp_path
):
    from averon_import.services.review_decisions import ReviewDecisionStore

    row = _pilot_shape_row()
    row["physical_row_refs"] = []
    result = _pilot_shape_result(row)
    result["review_ledger_revision"] = 2
    main, service, document_id, workspace = _install_pilot_shape_document(
        monkeypatch, tmp_path, result
    )
    store = ReviewDecisionStore(workspace.review_decisions_path)
    store.save([], revision=2)
    result_before = workspace.result_path.read_bytes()
    ledger_before = workspace.review_decisions_path.read_bytes()
    marker_before = store.revision_path.read_bytes()

    with pytest.raises(HTTPException) as error:
        main.save_review_decision(
            document_id,
            main.ReviewDecisionRequest(
                page=25,
                physical_refs=[],
                decision="CONFIRM_FIELD_ABSENT",
                field="quantity",
            ),
        )

    assert error.value.status_code == 400
    assert "OCR-доказательство" in str(error.value.detail)
    assert workspace.result_path.read_bytes() == result_before
    assert workspace.review_decisions_path.read_bytes() == ledger_before
    assert store.revision_path.read_bytes() == marker_before
    assert service.read_json(workspace.result_path)["revision"] == 10


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    following_functions = [
        index
        for marker in ("\nfunction ", "\nasync function ")
        if (index := source.find(marker, start + 1)) >= 0
    ]
    end = min(following_functions) if following_functions else len(source)
    return source[start:end]


def test_frontend_physical_ref_resolution_guards_actions_and_keeps_indexes_consistent():
    from pathlib import Path

    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    function_names = (
        "physicalRefs",
        "hasPhysicalRefs",
        "sourceRowIndex",
        "continuationParentRefs",
        "continuationParent",
        "continuationFragment",
        "rowRefsIndexKey",
        "rebuildResultIndexes",
        "updateIndexBucket",
        "replaceResultIndex",
        "rowById",
        "submitHumanDecision",
        "cellHtml",
        "escapeHtml",
    )
    functions = "\n".join(_extract_js_function(source, name) for name in function_names)
    script = f"""
const vm = require('vm');
const context = {{
  state: {{rows: [], document: {{document_id: 'doc'}}, reviewMutationQueues: new Map()}},
  CRITICAL_FIELDS: ['unit', 'quantity'],
  CRITICAL_LABELS: {{unit: 'Единица', quantity: 'Количество'}},
  isYandexCriticalRow: () => true,
  missingCriticalFields: row => ['unit', 'quantity'].filter(key => !String(row[key] || '').trim()),
  numericSuspectFields: row => row.suspect ? ['quantity'] : [],
  humanValueConfirmationMatches: () => false,
  humanAbsenceConfirmationMatches: () => false,
  apiCalls: 0,
  api: () => {{ context.apiCalls += 1; return Promise.resolve({{}}); }},
}};
vm.createContext(context);
vm.runInContext({json.dumps(functions)}, context);
const ref17 = [{{table: {{page_number: 25, table_index: 0}}, row_index: 17}}];
const ref16 = [{{table: {{page_number: 25, table_index: 0}}, row_index: 16}}];
const ref19 = [{{table: {{page_number: 25, table_index: 0}}, row_index: 19}}];
const rowA = {{id: 'pilot-row', page: 25, row_type: 'item', unit: '', quantity: '',
  physical_row_refs: ref17, ocr_metadata: {{physical_row_refs: []}},
  value_candidates: {{unit: {{value_candidate: 'шт.'}}}}}};
const resolvedA = context.physicalRefs(rowA);
const htmlA = context.cellHtml(rowA, 'unit');
const authoritative = context.physicalRefs({{physical_row_refs: ref17,
  ocr_metadata: {{physical_row_refs: ref16}}}});
const emptyRow = {{...rowA, physical_row_refs: [], ocr_metadata: {{physical_row_refs: []}}}};
const htmlEmpty = context.cellHtml(emptyRow, 'unit');
const suspectEmpty = {{...emptyRow, quantity: '87', suspect: true, value_candidates: {{}}}};
const htmlSuspectEmpty = context.cellHtml(suspectEmpty, 'quantity');
context.submitHumanDecision(emptyRow, {{decision: 'CONFIRM_FIELD_ABSENT'}}, 'test');
const parent = {{id: 'parent', page: 25, row_type: 'item', name: 'parent',
  physical_row_refs: ref16, ocr_metadata: {{physical_row_refs: []}}}};
const child = {{id: 'child', page: 25, row_type: 'semantic_review',
  physical_row_refs: ref17, ocr_metadata: {{physical_row_refs: []}},
  continuation_evidence: {{candidate_parent_physical_refs: ref16}}}};
context.state.rows = [parent, child];
context.rebuildResultIndexes();
const initialParent = context.continuationParent(child)?.id || null;
const childWithoutRefs = {{...child, physical_row_refs: [], ocr_metadata: {{physical_row_refs: []}}}};
const htmlContinuationEmpty = context.cellHtml(childWithoutRefs, 'name');
const nextParent = {{...parent, physical_row_refs: ref19, ocr_metadata: {{physical_row_refs: []}}}};
context.replaceResultIndex(parent, nextParent);
const oldKey = context.rowRefsIndexKey(25, ref16);
const newKey = context.rowRefsIndexKey(25, ref19);
const oldBucket = context.state.rowIndexes.byPageRefs.get(oldKey) || [];
const newBucket = context.state.rowIndexes.byPageRefs.get(newKey) || [];
const updatedChild = {{...child, continuation_evidence: {{candidate_parent_physical_refs: ref19}}}};
const updatedParent = context.continuationParent(updatedChild)?.id || null;
setImmediate(() => console.log(JSON.stringify({{
  resolvedA, metadataAuthoritative: authoritative, emptyCount: context.physicalRefs(emptyRow).length,
  acceptVisible: htmlA.includes('candidate-accept'), rejectVisible: htmlA.includes('candidate-reject'),
  absenceVisible: htmlA.includes('field-confirm-absent'), noEvidenceNotice: htmlEmpty.includes('Нет связанного OCR-доказательства'),
  emptyAcceptVisible: htmlEmpty.includes('candidate-accept'), emptyRejectVisible: htmlEmpty.includes('candidate-reject'),
  emptyAbsenceVisible: htmlEmpty.includes('field-confirm-absent'), emptyRequestCount: context.apiCalls,
  emptyValueConfirmationVisible: htmlSuspectEmpty.includes('field-confirm-value'),
  emptyContinuationVisible: htmlContinuationEmpty.includes('continuation-accept'),
  emptyContinuationNotice: htmlContinuationEmpty.includes('Нет связанного OCR-доказательства'),
  initialParent, oldBucket, newBucket, updatedParent, sourceRowIndex: context.sourceRowIndex(nextParent),
}})));
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, check=True, text=True
    )
    actual = json.loads(completed.stdout)
    ref17 = [{"table": {"page_number": 25, "table_index": 0}, "row_index": 17}]
    ref16 = [{"table": {"page_number": 25, "table_index": 0}, "row_index": 16}]
    ref19 = [{"table": {"page_number": 25, "table_index": 0}, "row_index": 19}]
    assert actual["resolvedA"] == ref17
    assert actual["metadataAuthoritative"] == ref16
    assert actual["emptyCount"] == 0
    assert actual["acceptVisible"] and actual["rejectVisible"] and actual["absenceVisible"]
    assert actual["noEvidenceNotice"]
    assert not actual["emptyAcceptVisible"]
    assert not actual["emptyRejectVisible"]
    assert not actual["emptyAbsenceVisible"]
    assert not actual["emptyValueConfirmationVisible"]
    assert not actual["emptyContinuationVisible"]
    assert actual["emptyContinuationNotice"]
    assert actual["emptyRequestCount"] == 0
    assert actual["initialParent"] == "parent"
    assert "parent" not in actual["oldBucket"]
    assert actual["newBucket"] == ["parent"]
    assert actual["updatedParent"] == "parent"
    assert actual["sourceRowIndex"] == 19
