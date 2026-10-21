from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from averon_import.services.ai_first.assembly import (
    assemble_document_draft, failed_page, page_from_payload,
)
from averon_import.services.ai_first.contracts import AIRow
from averon_import.services.ai_first.review import (
    AIReviewError, assert_review_ready_for_export, confirm_page_complete,
    new_review_state, record_handwriting_confirmation, record_row_decision,
    record_structure_change, reconcile_reprocessed_pages, resolve_review_issue,
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
    state = record_structure_change(
        draft, state, action="split_row", source_record_ids=[row_id],
        result_rows=[{"Наименование": "Деталь А"}, {"Наименование": "Деталь Б"}], reviewer_id="reviewer",
    )
    assert state.decisions[row_id].disposition == "excluded"
    assert len(state.manual_rows) == 2
    new_id = state.manual_rows[0].id
    state = record_structure_change(
        draft, state, action="merge_rows", source_record_ids=[new_id, state.manual_rows[1].id],
        result_rows=[{"Наименование": "Детали А и Б"}], reviewer_id="reviewer",
    )
    assert len(state.manual_rows) == 3
    state = record_structure_change(
        draft, state, action="add_row", source_record_ids=[],
        result_rows=[{"Наименование": "Добавленная позиция"}], reviewer_id="reviewer", page_number=1,
    )
    assert len(state.manual_rows) == 4
    assert state.history[-1].action == "add_row"


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
    output = tmp_path / "output.json"
    source.write_text(json.dumps({"format": "test", "pages": [{"page": 1, "transcription": raw}]}), encoding="utf-8")
    assert replay_main(["--input", str(source), "--output", str(output)]) == 0
    out = capsys.readouterr()
    assert "PRIVATE-CELL-CONTENT" not in out.out
    assert "row_observation_count" in out.out
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["pages"][0]["raw_model_payload"]["unknown"] == {"keep": 1}
    assert saved["export_authorized"] is False
    assert replay_main(["--input", str(source), "--output", str(output)]) == 2
