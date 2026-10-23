from __future__ import annotations

import json

import fitz
import pytest
from pydantic import ValidationError

from averon_import.services.ai_first.assembly import (
    AIInputError, assemble_document_draft, failed_page, page_from_payload,
)
from averon_import.services.ai_first.contracts import AIRow
from averon_import.services.ai_first.review import (
    AIReviewError, assert_review_ready_for_export, confirm_page_complete,
    new_review_state, record_handwriting_confirmation, record_row_decision,
    record_structure_change, reconcile_reprocessed_pages, resolve_review_issue,
    undo_structure_change,
)
from averon_import.tools.ai_first_replay import main as replay_main


def _cell(index: int, text: str, **overrides):
    result = {
        "column_index": index, "column_span": 1, "row_span": 1,
        "raw_text": text, "alternatives": [], "handwritten_replacements": [],
        "unreadable": False, "struck_through": False, "handwritten": False,
        "bbox_candidate": None,
    }
    result.update(overrides)
    return result


def _row(index: int, number: str, name: str, **overrides):
    result = {
        "kind": "header", "logical_row_index": index, "continuation_of": None,
        "continues_previous_page": False, "struck_through": False, "handwritten": False,
        "handwritten_replacements": [], "bbox_candidate": None,
        "cells": [_cell(0, number), _cell(1, name)],
    }
    result.update(overrides)
    return result


def _payload(page: int = 1, *, rows=None, headers=None, tables=None, **extra):
    value = {
        "page": page,
        "tables": tables if tables is not None else [{
            "headers": headers if headers is not None else ["Поз.", "Наименование"],
            "rows": rows if rows is not None else [_row(0, "1", "Насос")],
            "continues_previous_page": False,
            "bbox_candidate": None,
        }],
        "unreadable_fragments": [],
        "handwritten_replacements": [],
    }
    value.update(extra)
    return value


def _draft(payload=None, *, requested=(1,), document_id="doc-test"):
    payload = payload or _payload()
    page = page_from_payload(payload, source_document_id=document_id)
    return assemble_document_draft(document_id, [page], requested_pages=requested)


def _all_rows(draft):
    return [row for page in draft.pages for table in page.tables for row in table.rows]


def test_arbitrary_headers_columns_tables_and_missing_geometry_are_preserved():
    payload = _payload(tables=[
        {"headers": ["Габарит", "Класс", "Неизвестный столбец", "Примечание поставщика"],
         "rows": [{"kind": "component", "cells": [_cell(0, '970008'), _cell(1, "К2"), _cell(2, "X-7"), _cell(3, "условие")], "vendor_extension": {"a": 7}}]},
        {"headers": ["Свободный заголовок"], "rows": [{"kind": "note", "cells": [_cell(0, "текст")]}]},
    ], future_page_field={"retained": True})
    page = page_from_payload(payload, source_document_id="doc-test")
    draft = assemble_document_draft("doc-test", [page])
    assert [len(table.columns) for table in page.tables] == [4, 1]
    assert [len(table.rows) for table in page.tables] == [1, 1]
    assert page.tables[0].columns[2].raw_header == "Неизвестный столбец"
    assert page.tables[0].rows[0].raw_cells[2].raw_value == "X-7"
    assert page.tables[0].rows[0].raw_cells[0].bbox_candidate is None
    assert page.raw_model_payload["future_page_field"] == {"retained": True}
    assert page.tables[0].rows[0].raw_model_payload["vendor_extension"] == {"a": 7}
    assert len(draft.pages) == 1


def test_source_cell_order_unknown_values_and_model_role_are_not_rewritten():
    payload = _payload(rows=[_row(0, "1", "Насос", custom_row_key="kept")])
    row = _all_rows(_draft(payload))[0]
    assert [cell.raw_value for cell in row.raw_cells] == ["1", "Насос"]
    assert row.model_kind == "header"
    assert row.server_role_candidate is None
    assert row.role_verified is False
    assert row.canonical_value_proposals == []
    assert row.raw_model_payload["custom_row_key"] == "kept"
    with pytest.raises(ValidationError):
        AIRow.model_validate({**row.model_dump(), "role_verified": True})


def test_struck_quantity_and_handwritten_replacement_are_preserved_as_uncertain():
    quantity = _cell(1, '970006', struck_through=True, handwritten_replacements=[{
        "text": '970007', "bbox_candidate": None, "source_note": "red ink",
    }])
    payload = _payload(headers=["Наименование", "Количество"], rows=[{
        "kind": "item", "cells": [_cell(0, "Клапан"), quantity],
        "struck_through": False, "handwritten": True, "handwritten_replacements": [],
    }])
    cell = _all_rows(_draft(payload))[0].raw_cells[1]
    assert cell.raw_value == '970006'
    assert cell.struck_through is True
    assert cell.handwritten_replacements[0].text == '970007'
    assert cell.handwritten_replacements[0].raw_model_payload["source_note"] == "red ink"
    assert cell.raw_value != cell.handwritten_replacements[0].text
    assert any(issue.code == "row_has_uncertain_visual_evidence" for issue in _draft(payload).issues)


def test_empty_page_and_requested_missing_page_are_explicit_and_unverified():
    empty = page_from_payload(_payload(tables=[]), source_document_id="doc-test")
    draft = assemble_document_draft("doc-test", [empty], requested_pages=[1, 2])
    assert draft.pages[0].status == "no_specification"
    assert draft.pages[1].status == "failed"
    assert draft.pages[1].error_code == "page_result_missing"
    assert draft.extraction_is_verified is False
    assert draft.export_authorized is False
    assert draft.expected_page_numbers == [1, 2]


def test_page_payload_identity_must_match_server_page_argument():
    with pytest.raises(AIInputError, match="page_identity_mismatch"):
        page_from_payload(_payload(page=2), source_document_id="doc-test", page_number=1)
    with pytest.raises(AIInputError, match="page_identity_mismatch"):
        page_from_payload(_payload(page="1"), source_document_id="doc-test", page_number=1)
    assert page_from_payload(
        {"tables": []}, source_document_id="doc-test", page_number=1,
    ).page_number == 1


def test_incomplete_page_and_cross_page_continuation_remain_unmerged():
    incomplete_payload = _payload(tables=[], unreadable_fragments=["[unreadable]"])
    incomplete = page_from_payload(incomplete_payload, source_document_id="doc-test")
    page_two = page_from_payload(_payload(2, tables=[{
        "headers": ["Поз.", "Наименование"],
        "continues_previous_page": True,
        "rows": [_row(0, "", "продолжение", continues_previous_page=True)],
    }]), source_document_id="doc-test")
    draft = assemble_document_draft("doc-test", [page_two, incomplete])
    assert [page.page_number for page in draft.pages] == [1, 2]
    assert draft.pages[0].status == "incomplete"
    assert len(draft.pages[1].tables[0].rows) == 1
    assert any(issue.code == "continuation_unverified" for issue in draft.issues)
    assert any(issue.code == "page_transcription_incomplete" for issue in draft.issues)


def test_duplicate_and_gap_positions_are_review_issues_only():
    duplicate = _draft(_payload(rows=[_row(0, "1", "Клапан A"), _row(1, "1", "Клапан B")]))
    assert any(issue.code == "duplicate_position_candidate" for issue in duplicate.issues)
    assert all(row.role_verified is False for row in _all_rows(duplicate))
    gap = _draft(_payload(rows=[_row(0, "1", "Клапан A"), _row(1, "3", "Клапан C")]))
    assert any(issue.code == "position_gap_candidate" for issue in gap.issues)
    assert gap.export_authorized is False


def test_extreme_position_gap_does_not_materialize_a_large_range():
    draft = _draft(_payload(rows=[
        _row(0, "1", "Первый элемент"),
        _row(1, "1000000000", "Последний элемент"),
    ]))
    assert any(issue.code == "position_gap_candidate" for issue in draft.issues)


def test_page_and_row_fingerprints_keep_ids_stable_only_for_same_revision():
    first = _draft(_payload())
    same = _draft(_payload())
    changed = _draft(_payload(rows=[_row(0, "1", "Новый насос")]))
    assert _all_rows(first)[0].id == _all_rows(same)[0].id
    assert _all_rows(first)[0].id != _all_rows(changed)[0].id
    assert first.revision_id == same.revision_id
    assert first.revision_id != changed.revision_id


def test_review_guard_requires_explicit_row_page_and_issue_decisions():
    draft = _draft(_payload(rows=[_row(0, "1", "Клапан A"), _row(1, "3", "Клапан C")]))
    state = new_review_state(draft)
    with pytest.raises(AIReviewError, match="page_completeness_not_human_confirmed"):
        assert_review_ready_for_export(draft, state)
    for row in _all_rows(draft):
        state = record_row_decision(draft, state, row.id, disposition="confirmed", reviewer_id="reviewer")
    state = confirm_page_complete(draft, state, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="review_issues_unresolved"):
        assert_review_ready_for_export(draft, state)
    for issue in draft.issues:
        state = resolve_review_issue(draft, state, issue.id, reviewer_id="reviewer", detail="Checked against source.")
    assert_review_ready_for_export(draft, state) is None
    assert draft.export_authorized is False


def test_export_guard_rejects_empty_documents_and_missing_manifest():
    empty = assemble_document_draft("doc-test", [], requested_pages=[])
    with pytest.raises(AIReviewError, match="empty_document"):
        assert_review_ready_for_export(empty, new_review_state(empty))

    no_rows = _draft(_payload(tables=[]))
    state = confirm_page_complete(no_rows, new_review_state(no_rows), 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="empty_document"):
        assert_review_ready_for_export(no_rows, state)

    all_excluded = _draft(_payload())
    row_id = _all_rows(all_excluded)[0].id
    state = record_row_decision(
        all_excluded, new_review_state(all_excluded), row_id,
        disposition="excluded", reviewer_id="reviewer",
    )
    state = confirm_page_complete(all_excluded, state, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="empty_document"):
        assert_review_ready_for_export(all_excluded, state)

    partial = _draft(_payload()).model_copy(update={"expected_page_numbers": [1, 2]})
    state = record_row_decision(
        partial, new_review_state(partial), _all_rows(partial)[0].id,
        disposition="confirmed", reviewer_id="reviewer",
    )
    state = confirm_page_complete(partial, state, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="page_coverage_incomplete"):
        assert_review_ready_for_export(partial, state)

    no_manifest = assemble_document_draft(
        "doc-test", [page_from_payload(_payload(), source_document_id="doc-test")],
    )
    state = record_row_decision(
        no_manifest, new_review_state(no_manifest), _all_rows(no_manifest)[0].id,
        disposition="confirmed", reviewer_id="reviewer",
    )
    state = confirm_page_complete(no_manifest, state, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="expected_page_manifest_required"):
        assert_review_ready_for_export(no_manifest, state)


@pytest.mark.parametrize("failed_status", ["failed", "incomplete"])
def test_export_guard_rejects_failed_or_incomplete_page_even_after_available_issue_resolutions(failed_status):
    page_one = page_from_payload(_payload(), source_document_id="doc-test")
    page_two = (
        failed_page("doc-test", 2, "transport_failed")
        if failed_status == "failed"
        else page_from_payload(
            _payload(2, tables=[], unreadable_fragments=["[unreadable]"]),
            source_document_id="doc-test",
        )
    )
    draft = assemble_document_draft(
        "doc-test", [page_one, page_two], requested_pages=[1, 2],
    )
    state = new_review_state(draft)
    for row in _all_rows(draft):
        state = record_row_decision(draft, state, row.id, disposition="confirmed", reviewer_id="reviewer")
    for page in draft.pages:
        state = confirm_page_complete(draft, state, page.page_number, reviewer_id="reviewer")
    for issue in draft.issues:
        if issue.code == "page_extraction_failed":
            with pytest.raises(AIReviewError, match="processing_issue_requires_retry"):
                resolve_review_issue(draft, state, issue.id, reviewer_id="reviewer", detail="Reviewed source.")
            continue
        state = resolve_review_issue(draft, state, issue.id, reviewer_id="reviewer", detail="Reviewed source.")
    with pytest.raises(AIReviewError, match="page_processing_incomplete"):
        assert_review_ready_for_export(draft, state)


def test_human_can_correct_handwriting_add_split_merge_and_exclude():
    draft = _draft(_payload(rows=[_row(0, "1", "Слитая запись")]))
    row_id = _all_rows(draft)[0].id
    state = new_review_state(draft)
    state = record_handwriting_confirmation(
        draft, state, row_id, reviewer_id="reviewer", values={"Количество": "12"},
    )
    assert state.decisions[row_id].values == {"Количество": "12"}
    assert state.history[0].payload["values"] == {"Количество": "12"}
    assert state.history[0].event_id.startswith("review-event:")
    assert state.history[0].occurred_at.tzinfo is not None
    assert any(event.action == "confirm_handwriting" for event in state.history)
    state = confirm_page_complete(draft, state, 1, reviewer_id="reviewer")
    state = record_structure_change(
        draft, state, action="split_row", source_record_ids=[row_id],
        result_rows=[{"Наименование": "Деталь А"}, {"Наименование": "Деталь Б"}], reviewer_id="reviewer",
    )
    assert state.decisions[row_id].disposition == "excluded"
    assert state.confirmed_complete_pages == set()
    assert len(state.manual_rows) == 2
    new_id = state.manual_rows[0].id
    assert all(row.active for row in state.manual_rows)
    state = record_structure_change(
        draft, state, action="merge_rows", source_record_ids=[new_id, state.manual_rows[1].id],
        result_rows=[{"Наименование": "Детали А и Б"}], reviewer_id="reviewer",
    )
    assert len(state.manual_rows) == 3
    assert [row.active for row in state.manual_rows] == [False, False, True]
    assert state.decisions[new_id].active is False
    state = record_structure_change(
        draft, state, action="add_row", source_record_ids=[],
        result_rows=[{"Наименование": "Добавленная позиция"}], reviewer_id="reviewer", page_number=1,
    )
    assert len(state.manual_rows) == 4
    assert state.history[-1].action == "add_row"


def test_superseded_source_cannot_be_reactivated_and_guard_detects_conflicting_state():
    draft = _draft(_payload())
    source_id = _all_rows(draft)[0].id
    state = record_structure_change(
        draft, new_review_state(draft), action="split_row", source_record_ids=[source_id],
        result_rows=[{"Наименование": "Часть А"}, {"Наименование": "Часть Б"}],
        reviewer_id="reviewer",
    )
    with pytest.raises(AIReviewError, match="review_source_superseded"):
        record_row_decision(draft, state, source_id, disposition="confirmed", reviewer_id="reviewer")

    # Simulate malformed persisted state to prove the export boundary also fails closed.
    state.decisions[source_id] = state.decisions[source_id].model_copy(
        update={"disposition": "confirmed"},
    )
    state = confirm_page_complete(draft, state, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="conflicting_active_lineage"):
        assert_review_ready_for_export(draft, state)


def test_latest_structure_change_can_be_undone_with_audit_and_invalidated_completeness():
    draft = _draft(_payload())
    source_id = _all_rows(draft)[0].id
    state = record_row_decision(
        draft, new_review_state(draft), source_id, disposition="confirmed", reviewer_id="reviewer",
    )
    state = confirm_page_complete(draft, state, 1, reviewer_id="reviewer")
    state = record_structure_change(
        draft, state, action="split_row", source_record_ids=[source_id],
        result_rows=[{"Наименование": "Часть А"}, {"Наименование": "Часть Б"}],
        reviewer_id="reviewer",
    )
    structure_event = state.history[-1]
    restored = undo_structure_change(draft, state, structure_event.event_id, reviewer_id="reviewer")
    assert restored.decisions[source_id].disposition == "confirmed"
    assert restored.decisions[source_id].active is True
    assert all(not row.active for row in restored.manual_rows)
    assert all(not restored.decisions[row.id].active for row in restored.manual_rows)
    assert restored.confirmed_complete_pages == set()
    assert restored.history[-1].action == "undo_structure_change"
    assert restored.history[-1].payload["reverted_event_id"] == structure_event.event_id
    assert any(event.event_id == structure_event.event_id for event in restored.history)

    restored = confirm_page_complete(draft, restored, 1, reviewer_id="reviewer")
    assert_review_ready_for_export(draft, restored)


def test_merge_can_be_undone_in_the_same_extraction_revision():
    draft = _draft(_payload(rows=[_row(0, "1", "Строка А"), _row(1, "2", "Строка Б")]))
    rows = _all_rows(draft)
    state = new_review_state(draft)
    for row in rows:
        state = record_row_decision(draft, state, row.id, disposition="confirmed", reviewer_id="reviewer")
    state = record_structure_change(
        draft, state, action="merge_rows", source_record_ids=[row.id for row in rows],
        result_rows=[{"Наименование": "Объединённая строка"}], reviewer_id="reviewer",
    )
    merge_event = state.history[-1]
    assert all(state.decisions[row.id].active for row in rows)
    assert state.decisions[merge_event.result_record_ids[0]].active

    restored = undo_structure_change(draft, state, merge_event.event_id, reviewer_id="reviewer")
    assert all(restored.decisions[row.id].disposition == "confirmed" for row in rows)
    assert all(restored.decisions[row.id].active for row in rows)
    assert not restored.manual_rows[0].active
    assert not restored.decisions[merge_event.result_record_ids[0]].active
    assert restored.history[-1].payload["reverted_event_id"] == merge_event.event_id


def test_reprocessing_blocks_undo_and_cannot_restore_stale_manual_rows_or_decisions():
    old = _draft(_payload())
    state = record_structure_change(
        old, new_review_state(old), action="add_row", source_record_ids=[],
        result_rows=[{"Наименование": "Ручная запись М"}], reviewer_id="reviewer", page_number=1,
    )
    manual_m = state.manual_rows[-1]
    state = record_structure_change(
        old, state, action="split_row", source_record_ids=[manual_m.id],
        result_rows=[{"Наименование": "Ручная запись Н"}, {"Наименование": "Ручная запись О"}],
        reviewer_id="reviewer",
    )
    split_event = state.history[-1]
    assert manual_m.page_extraction_fingerprint == old.pages[0].extraction_fingerprint

    new = _draft(_payload(rows=[_row(0, "1", "Изменённая исходная строка")]))
    reconciled = reconcile_reprocessed_pages(old, new, state)
    assert reconciled.history[-1].action == "page_reprocessed"
    assert all(not row.active for row in reconciled.manual_rows)
    assert all(not reconciled.decisions[row.id].active for row in reconciled.manual_rows)

    before = reconciled.model_dump(mode="json")
    with pytest.raises(AIReviewError, match="structure_change_stale_revision"):
        undo_structure_change(new, reconciled, split_event.event_id, reviewer_id="reviewer")
    assert reconciled.model_dump(mode="json") == before

    # The fingerprint binding also protects old events if a persisted audit history is incomplete.
    without_reprocess_event = reconciled.model_copy(deep=True)
    without_reprocess_event.history = [
        event for event in without_reprocess_event.history if event.action != "page_reprocessed"
    ]
    with pytest.raises(AIReviewError, match="structure_change_stale_revision"):
        undo_structure_change(new, without_reprocess_event, split_event.event_id, reviewer_id="reviewer")

    # Simulate corrupted persisted state that reactivates the stale row and its decision.
    stale_manual_id = split_event.result_record_ids[0]
    corrupted = reconciled.model_copy(deep=True)
    corrupted.manual_rows = [
        row.model_copy(update={"active": True}) if row.id == stale_manual_id else row
        for row in corrupted.manual_rows
    ]
    corrupted.decisions[stale_manual_id] = corrupted.decisions[stale_manual_id].model_copy(
        update={"active": True},
    )
    current_source_row = _all_rows(new)[0]
    corrupted = record_row_decision(
        new, corrupted, current_source_row.id, disposition="confirmed", reviewer_id="reviewer",
    )
    corrupted = confirm_page_complete(new, corrupted, 1, reviewer_id="reviewer")
    with pytest.raises(AIReviewError, match="manual_row_page_revision_mismatch"):
        assert_review_ready_for_export(new, corrupted)


def test_structure_change_cannot_be_undone_after_dependent_row_review():
    draft = _draft(_payload())
    source_id = _all_rows(draft)[0].id
    state = record_structure_change(
        draft, new_review_state(draft), action="split_row", source_record_ids=[source_id],
        result_rows=[{"Наименование": "Часть А"}, {"Наименование": "Часть Б"}],
        reviewer_id="reviewer",
    )
    result_id = state.manual_rows[0].id
    state = record_row_decision(
        draft, state, result_id, disposition="confirmed", reviewer_id="reviewer",
    )
    with pytest.raises(AIReviewError, match="structure_change_has_dependent_edits"):
        undo_structure_change(draft, state, state.history[0].event_id, reviewer_id="reviewer")


def test_reprocessing_invalidates_changed_page_decisions_and_confirmations():
    old = _draft(_payload())
    row = _all_rows(old)[0]
    state = new_review_state(old)
    state = record_row_decision(old, state, row.id, disposition="confirmed", reviewer_id="reviewer")
    state = confirm_page_complete(old, state, 1, reviewer_id="reviewer")
    new = _draft(_payload(rows=[_row(0, "1", "Переработанное значение")]))
    reconciled = reconcile_reprocessed_pages(old, new, state)
    assert reconciled.revision_id == new.revision_id
    assert reconciled.decisions[row.id].active is False
    assert reconciled.confirmed_complete_pages == set()
    assert not any(new_row.id in reconciled.decisions for new_row in _all_rows(new))
    assert reconciled.history[-1].action == "page_reprocessed"
    with pytest.raises(AIReviewError):
        assert_review_ready_for_export(new, reconciled)


def test_reprocessing_identical_page_retains_decisions():
    old = _draft(_payload())
    row = _all_rows(old)[0]
    state = record_row_decision(old, new_review_state(old), row.id, disposition="confirmed", reviewer_id="reviewer")
    same = _draft(_payload())
    reconciled = reconcile_reprocessed_pages(old, same, state)
    assert reconciled.decisions[row.id].active is True
    assert reconciled.revision_id == same.revision_id


def test_offline_replay_cli_preserves_payload_and_only_prints_metadata(tmp_path, capsys):
    raw = {"page": 1, "tables": [{"headers": ["Поз.", "Наименование"], "rows": [
        _row(0, "1", "PRIVATE-CELL-CONTENT"),
    ]}], "unreadable_fragments": [], "handwritten_replacements": [], "unknown": {"keep": 1}}
    source = tmp_path / "input.json"
    source_pdf = tmp_path / "source.pdf"
    output = tmp_path / "output.json"
    with fitz.open() as document:
        document.new_page()
        document.save(source_pdf)
    source.write_text(json.dumps({"format": "test", "pages": [{"page": 1, "transcription": raw}]}), encoding="utf-8")
    assert replay_main(["--input", str(source), "--source-pdf", str(source_pdf), "--output", str(output)]) == 0
    out = capsys.readouterr()
    assert "PRIVATE-CELL-CONTENT" not in out.out
    assert "row_observation_count" in out.out
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["pages"][0]["raw_model_payload"]["unknown"] == {"keep": 1}
    assert saved["export_authorized"] is False
    assert replay_main(["--input", str(source), "--source-pdf", str(source_pdf), "--output", str(output)]) == 2


def test_offline_replay_uses_source_pdf_page_count_not_model_pages(tmp_path, capsys):
    raw = _payload()
    source = tmp_path / "input.json"
    source_pdf = tmp_path / "source.pdf"
    output = tmp_path / "output.json"
    with fitz.open() as document:
        document.new_page()
        document.new_page()
        document.save(source_pdf)
    source.write_text(json.dumps({
        "pages": [{"page": 1, "transcription": raw}],
    }), encoding="utf-8")

    assert replay_main([
        "--input", str(source), "--source-pdf", str(source_pdf), "--output", str(output),
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert summary["expected_page_count"] == 2
    assert summary["page_status_counts"] == {"extracted": 1, "failed": 1}
    assert saved["expected_page_numbers"] == [1, 2]
    assert saved["pages"][1]["error_code"] == "page_result_missing"
