"""Owned-copy batching must preserve the sequential public review semantics."""
from datetime import datetime, timezone
from itertools import count
import json
from uuid import UUID

import pytest

from averon_import.services.ai_first import bulk_review as bulk
from averon_import.services.ai_first import contracts
from averon_import.services.ai_first.contracts import AIReviewEvent, AIReviewState
from averon_import.services.ai_first.editor_draft import EditorEdits, review_token, save_editor
from averon_import.services.ai_first.local_workflow import AIWorkspaceStore
from averon_import.services.ai_first.review import (
    AIReviewError, new_review_state, record_row_decision,
    record_row_role_classification, record_structure_change,
)
from benchmark_ai_first_bulk_review import fixture
from test_ai_first_atomic_bulk_review import request_for, setup_state
from test_ai_first_foundation import _draft, _payload


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 1, tzinfo=timezone.utc)


def case_state(tmp_path, case):
    _, draft, state = setup_state(tmp_path)
    rows = draft.pages[0].tables[0].rows
    parent, child, other = [row.id for row in rows]
    if case in {"component", "section"}:
        state = record_row_role_classification(draft, state, parent, role="standalone_item", reviewer_id="reviewer")
        state = record_row_decision(draft, state, parent, disposition="corrected", values={"cell:1": "Parent edit"}, reviewer_id="reviewer")
    if case == "section":
        state = record_row_role_classification(draft, state, child, role="component", parent_record_id=parent, reviewer_id="reviewer")
        state = record_row_decision(draft, state, child, disposition="confirmed", reviewer_id="reviewer")
    if case == "manual":
        draft = _draft(_payload(tables=[]))
        state = new_review_state(draft)
        state.resolved_issue_ids = {issue.id for issue in [*draft.issues, *(i for p in draft.pages for i in p.issues)]}
        state = record_structure_change(draft, state, action="add_row", source_record_ids=[],
            result_rows=[{"cell:0": "Synthetic manual", "cell:1": "5"}], reviewer_id="reviewer", page_number=1)
        manual = state.manual_rows[-1]
        selected, role = [manual.id], "standalone_item"
    else:
        selected = [parent, other] if case == "section" else [child, other]
        role = case if case in {"section", "component"} else "standalone_item"
    # Historical nested payloads must remain independent of the prepared output.
    state.history.insert(0, AIReviewEvent(action="confirm_row", source_document_id=draft.source_document_id,
        reviewer_id="historical", page_number=1, source_record_ids=[], payload={"nested": {"values": ["keep"]}}))
    editor = save_editor(draft, state, None, EditorEdits(row_values={selected[0]: {"cell:1": "5"}}),
                         expected_token=review_token(draft, state, None), reviewer_id="reviewer")
    overrides = {"parents": {record_id: parent for record_id in selected}} if case == "component" else {}
    if case == "manual":
        overrides.update(values={manual.id: dict(manual.values)}, provenance={manual.id: {
            "page_number": 1, "table_id": None, "extraction_fingerprint": draft.pages[0].extraction_fingerprint}})
    request = request_for(draft, state, selected, editor=editor, role=role, **overrides)
    request.values[selected[0]]["cell:1"] = "5"
    return draft, state, editor, request


@pytest.mark.parametrize("case", ["standalone", "component", "section", "manual"])
def test_owned_copy_matches_sequential_public_operations_exactly(tmp_path, monkeypatch, case):
    draft, state, editor, request = case_state(tmp_path, case)
    before = state.model_dump(mode="json")
    raw = draft.model_dump(mode="json")
    editor_before = json.loads(json.dumps(editor))
    observed = []
    for use_public in (False, True):
        store = AIWorkspaceStore(tmp_path / str(use_public))
        store.write_many({"transcription.json": raw, "review.json": before, "editor-draft.json": editor})
        with monkeypatch.context() as context:
            sequence = count(1)
            context.setattr(contracts, "uuid4", lambda: UUID(int=next(sequence)))
            context.setattr(contracts, "datetime", FrozenDatetime)
            if use_public:
                # Reference path retains the former full clone on every action.
                context.setattr(bulk, "_record_row_role_classification_in_place", record_row_role_classification)
                context.setattr(bulk, "_record_row_decision_in_place", record_row_decision)
            preview = bulk.execute_bulk_review(store, draft, state, editor,
                request.model_copy(update={"mode": "preview"}), "reviewer")
            # Reset event identity/time so preview cannot influence the commit comparison.
            sequence = count(1)
            result = bulk.execute_bulk_review(store, draft, state, editor, request, "reviewer")
        checked = AIReviewState.model_validate_json(json.dumps(store.read("review.json")))
        consumed = store.read("editor-draft.json")
        persisted = store.read("review.json")
        replay = bulk.execute_bulk_review(store, draft, checked, consumed, request, "reviewer")
        assert replay["replayed"] and store.read("review.json") == persisted
        assert state.model_dump(mode="json") == before and draft.model_dump(mode="json") == raw
        assert editor == editor_before
        assert checked.history[0].payload == {"nested": {"values": ["keep"]}}
        if case == "section":
            child = draft.pages[0].tables[0].rows[1].id
            assert child in preview["invalidated_role_ids"] and child in preview["invalidated_decision_ids"]
            assert not checked.row_roles[child].active and not checked.decisions[child].active
        observed.append((preview, result, persisted, consumed, replay))
    assert observed[0] == observed[1], "roles, event order, invalidations, receipt and editor consumption must match"


@pytest.mark.parametrize("role", ["standalone_item", "section"])
def test_large_batch_clones_ledger_once_without_aliasing_input(tmp_path, monkeypatch, role):
    draft, state, request = fixture(100, 1000)
    if role == "section":
        request = request.model_copy(update={"role": role, "intent": "exclude_from_positions"})
    before = state.model_dump(mode="json")
    original = AIReviewState.model_copy
    deep_copies = []

    def counted(self, **kwargs):
        if kwargs.get("deep"):
            deep_copies.append(self)
        return original(self, **kwargs)

    monkeypatch.setattr(AIReviewState, "model_copy", counted)
    updated, summary = bulk.prepare_bulk_review(draft, state, None, request, "reviewer")
    assert len(deep_copies) == 1 and deep_copies[0] is state
    assert summary["row_count"] == 100
    assert state.model_dump(mode="json") == before
    assert len(updated.history) == 1000 + (300 if role == "section" else 200)
    updated.history[0].payload["values"]["cell:1"] = "Output-only mutation"
    updated.decisions[request.record_ids[0]].values["cell:0"] = "Output-only mutation"
    updated.resolved_issue_ids.clear()
    assert state.model_dump(mode="json") == before


def test_late_apply_failure_discards_owned_working_copy_without_publication(tmp_path, monkeypatch):
    store, draft, state = setup_state(tmp_path)
    request = request_for(draft, state)
    before = state.model_dump(mode="json")
    original = bulk._record_row_decision_in_place
    calls = []

    def fail_after_apply(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise AIReviewError("synthetic_late_apply_failure")
        return result

    monkeypatch.setattr(bulk, "_record_row_decision_in_place", fail_after_apply)
    with pytest.raises(AIReviewError, match="synthetic_late_apply_failure"):
        bulk.execute_bulk_review(store, draft, state, None, request, "reviewer")
    assert len(calls) == 2 and calls[0] is not state
    assert state.model_dump(mode="json") == before and store.read("review.json") == before
    assert store.read("editor-draft.json") is None


def test_public_single_row_operations_still_isolate_input(tmp_path):
    _, draft, state = setup_state(tmp_path)
    record_id = draft.pages[0].tables[0].rows[0].id
    before = state.model_dump(mode="json")
    classified = record_row_role_classification(draft, state, record_id, role="standalone_item", reviewer_id="reviewer")
    assert classified is not state and state.model_dump(mode="json") == before
    classified_before = classified.model_dump(mode="json")
    corrected = record_row_decision(draft, classified, record_id, disposition="corrected",
                                    values={"cell:1": "5"}, reviewer_id="reviewer")
    assert corrected is not classified and classified.model_dump(mode="json") == classified_before
    corrected.row_roles[record_id].active = False
    corrected.history[0].source_record_ids.clear()
    assert classified.model_dump(mode="json") == classified_before
    with pytest.raises(AIReviewError, match="invalid_review_decision"):
        record_row_decision(draft, classified, record_id, disposition="invalid", reviewer_id="reviewer")
    assert classified.model_dump(mode="json") == classified_before
