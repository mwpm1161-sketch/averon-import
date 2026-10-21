"""Human Review V2 state operations, isolated from the existing review flow."""
from __future__ import annotations

from uuid import uuid4
from typing import Iterable

from averon_import.services.ai_first.contracts import (
    AIDocumentDraft, AIManualRow, AIReviewDecision, AIReviewEvent, AIReviewState,
)


class AIReviewError(ValueError):
    """Safe state/guard failure; messages never include document contents."""


def new_review_state(draft: AIDocumentDraft) -> AIReviewState:
    return AIReviewState(source_document_id=draft.source_document_id, revision_id=draft.revision_id)


def _rows(draft: AIDocumentDraft) -> dict[str, tuple[int, object]]:
    return {
        row.id: (page.page_number, row)
        for page in draft.pages for table in page.tables for row in table.rows
    }


def _active_lineage_sources(state: AIReviewState) -> set[str]:
    """Return every ancestor replaced by a currently active manual row."""
    manual_by_id = {row.id: row for row in state.manual_rows}
    pending = [
        source_id
        for row in state.manual_rows if row.active
        for source_id in row.source_record_ids
    ]
    sources: set[str] = set()
    while pending:
        source_id = pending.pop()
        if source_id in sources:
            continue
        sources.add(source_id)
        ancestor = manual_by_id.get(source_id)
        if ancestor is not None:
            pending.extend(ancestor.source_record_ids)
    return sources


def _check_current(draft: AIDocumentDraft, state: AIReviewState) -> None:
    if draft.source_document_id != state.source_document_id or draft.revision_id != state.revision_id:
        raise AIReviewError("review_state_revision_mismatch")


def record_row_decision(
    draft: AIDocumentDraft, state: AIReviewState, record_id: str, *,
    disposition: str, reviewer_id: str, values: dict[str, str] | None = None,
) -> AIReviewState:
    """Record an explicit human decision; nothing in extraction calls this."""
    _check_current(draft, state)
    if not reviewer_id or disposition not in {"confirmed", "corrected", "excluded"}:
        raise AIReviewError("invalid_review_decision")
    values = dict(values or {})
    if disposition == "corrected" and not values:
        raise AIReviewError("corrected_decision_requires_values")
    if disposition != "corrected" and values:
        raise AIReviewError("values_require_corrected_decision")
    row_pages = {row_id: page for row_id, (page, _) in _rows(draft).items()}
    row_pages.update({row.id: row.page_number for row in state.manual_rows if row.active})
    page_number = row_pages.get(record_id)
    if page_number is None:
        raise AIReviewError("unknown_review_record")
    if disposition != "excluded" and record_id in _active_lineage_sources(state):
        raise AIReviewError("review_source_superseded")
    action = {"confirmed": "confirm_row", "corrected": "edit_value", "excluded": "exclude_row"}[disposition]
    updated = state.model_copy(deep=True)
    updated.decisions[record_id] = AIReviewDecision(
        record_id=record_id, source_document_id=draft.source_document_id,
        page_number=page_number, disposition=disposition, reviewer_id=reviewer_id,
        values=values, active=True,
    )
    updated.history.append(AIReviewEvent(
        action=action, source_document_id=draft.source_document_id, reviewer_id=reviewer_id, page_number=page_number,
        source_record_ids=[record_id], payload={"disposition": disposition, "values": values},
    ))
    return updated


def record_handwriting_confirmation(
    draft: AIDocumentDraft, state: AIReviewState, record_id: str, *,
    reviewer_id: str, values: dict[str, str],
) -> AIReviewState:
    """Attach a human-entered reading to the row after visual confirmation."""
    updated = record_row_decision(
        draft, state, record_id, disposition="corrected", reviewer_id=reviewer_id, values=values,
    )
    page_number = _rows(draft).get(record_id, (None, None))[0]
    if page_number is None:
        page_number = next(row.page_number for row in updated.manual_rows if row.id == record_id)
    updated.history.append(AIReviewEvent(
        action="confirm_handwriting", source_document_id=draft.source_document_id, reviewer_id=reviewer_id, page_number=page_number,
        source_record_ids=[record_id], payload={"values": values},
    ))
    return updated


def record_structure_change(
    draft: AIDocumentDraft, state: AIReviewState, *, action: str,
    source_record_ids: Iterable[str], result_rows: list[dict[str, str]],
    reviewer_id: str, page_number: int | None = None,
) -> AIReviewState:
    """Record add/split/merge as explicit source-linked human edits."""
    _check_current(draft, state)
    if not reviewer_id or action not in {"add_row", "split_row", "merge_rows"}:
        raise AIReviewError("invalid_structure_action")
    source_ids = list(source_record_ids)
    if len(set(source_ids)) != len(source_ids):
        raise AIReviewError("duplicate_structure_source")
    if action == "add_row" and (source_ids or len(result_rows) != 1):
        raise AIReviewError("invalid_add_row_shape")
    if action == "split_row" and (len(source_ids) != 1 or len(result_rows) < 2):
        raise AIReviewError("invalid_split_shape")
    if action == "merge_rows" and (len(source_ids) < 2 or len(result_rows) != 1):
        raise AIReviewError("invalid_merge_shape")
    known = _rows(draft)
    known.update({row.id: (row.page_number, row) for row in state.manual_rows if row.active})
    if any(record_id not in known for record_id in source_ids):
        raise AIReviewError("unknown_review_record")
    superseded = _active_lineage_sources(state)
    if any(record_id in superseded for record_id in source_ids):
        raise AIReviewError("review_source_superseded")
    pages = {known[record_id][0] for record_id in source_ids}
    if len(pages) > 1:
        raise AIReviewError("cross_page_structure_change_requires_explicit_review")
    if not source_ids:
        if type(page_number) is not int:
            raise AIReviewError("manual_row_page_required")
    else:
        inferred_page = next(iter(pages))
        if page_number is not None and page_number != inferred_page:
            raise AIReviewError("source_page_mismatch")
        page_number = inferred_page
    page_numbers = {page.page_number for page in draft.pages}
    if page_number not in page_numbers:
        raise AIReviewError("unknown_page")
    page = next(page for page in draft.pages if page.page_number == page_number)
    updated = state.model_copy(deep=True)
    prior_decisions = {
        record_id: (
            state.decisions[record_id].model_dump(mode="json")
            if record_id in state.decisions else None
        )
        for record_id in source_ids
    }
    prior_manual_active = {
        record_id: next(row.active for row in state.manual_rows if row.id == record_id)
        for record_id in source_ids
        if any(row.id == record_id for row in state.manual_rows)
    }
    for record_id in source_ids:
        is_manual = any(row.id == record_id for row in updated.manual_rows)
        if is_manual:
            updated.manual_rows = [
                row.model_copy(update={"active": False}) if row.id == record_id else row
                for row in updated.manual_rows
            ]
        updated.decisions[record_id] = AIReviewDecision(
            record_id=record_id, source_document_id=draft.source_document_id,
            page_number=page_number, disposition="excluded", reviewer_id=reviewer_id,
            active=not is_manual,
        )
    result_ids: list[str] = []
    for proposed in result_rows:
        values = dict(proposed)
        if not values:
            raise AIReviewError("manual_row_values_required")
        manual_id = f"manual:{uuid4().hex}"
        manual = AIManualRow(
            id=manual_id, source_document_id=draft.source_document_id,
            page_number=page_number, page_extraction_fingerprint=page.extraction_fingerprint,
            reviewer_id=reviewer_id, values=values,
            source_record_ids=source_ids,
        )
        updated.manual_rows.append(manual)
        updated.decisions[manual_id] = AIReviewDecision(
            record_id=manual_id, source_document_id=draft.source_document_id,
            page_number=page_number, disposition="corrected", reviewer_id=reviewer_id,
            values=values, active=True,
        )
        result_ids.append(manual_id)
    updated.history.append(AIReviewEvent(
        action=action, source_document_id=draft.source_document_id, reviewer_id=reviewer_id, page_number=page_number,
        source_record_ids=source_ids, result_record_ids=result_ids,
        payload={
            "result_count": len(result_ids),
            "prior_decisions": prior_decisions,
            "prior_manual_active": prior_manual_active,
            "page_extraction_fingerprint": page.extraction_fingerprint,
            "page_status": page.status,
            "draft_revision_id": draft.revision_id,
        },
    ))
    updated.confirmed_complete_pages.discard(page_number)
    return updated


def undo_structure_change(
    draft: AIDocumentDraft, state: AIReviewState, event_id: str, *, reviewer_id: str,
) -> AIReviewState:
    """Undo the latest non-dependent structural edit while preserving its audit event."""
    _check_current(draft, state)
    if not reviewer_id:
        raise AIReviewError("invalid_reviewer")
    index = next((i for i, event in enumerate(state.history) if event.event_id == event_id), None)
    if index is None:
        raise AIReviewError("unknown_structure_change")
    event = state.history[index]
    if event.action not in {"add_row", "split_row", "merge_rows"}:
        raise AIReviewError("not_a_structure_change")
    later = state.history[index + 1:]
    if any(
        later_event.action == "page_reprocessed" and later_event.page_number == event.page_number
        for later_event in later
    ):
        raise AIReviewError("structure_change_stale_revision")
    page = next((page for page in draft.pages if page.page_number == event.page_number), None)
    recorded_fingerprint = event.payload.get("page_extraction_fingerprint")
    recorded_status = event.payload.get("page_status")
    if (page is None or not isinstance(recorded_fingerprint, str) or not recorded_fingerprint
            or recorded_fingerprint != page.extraction_fingerprint or recorded_status != page.status):
        raise AIReviewError("structure_change_stale_revision")
    if any(
        later_event.page_number == event.page_number
        and later_event.action in {"add_row", "split_row", "merge_rows"}
        for later_event in later
    ):
        raise AIReviewError("structure_change_not_latest_for_page")
    affected = set(event.source_record_ids) | set(event.result_record_ids)
    if any(
        affected.intersection(later_event.source_record_ids + later_event.result_record_ids)
        for later_event in later
    ):
        raise AIReviewError("structure_change_has_dependent_edits")

    updated = state.model_copy(deep=True)
    result_ids = set(event.result_record_ids)
    updated.manual_rows = [
        row.model_copy(update={"active": False}) if row.id in result_ids else row
        for row in updated.manual_rows
    ]
    for record_id in result_ids:
        decision = updated.decisions.get(record_id)
        if decision is not None:
            updated.decisions[record_id] = decision.model_copy(update={"active": False})

    prior_decisions = event.payload.get("prior_decisions", {})
    prior_manual_active = event.payload.get("prior_manual_active", {})
    manual_ids = {row.id for row in updated.manual_rows}
    for record_id in event.source_record_ids:
        if record_id in manual_ids:
            was_active = prior_manual_active.get(record_id, True)
            updated.manual_rows = [
                row.model_copy(update={"active": was_active}) if row.id == record_id else row
                for row in updated.manual_rows
            ]
        previous = prior_decisions.get(record_id)
        if previous is None:
            updated.decisions.pop(record_id, None)
        else:
            updated.decisions[record_id] = AIReviewDecision.model_validate(previous)

    updated.confirmed_complete_pages.discard(event.page_number)
    updated.history.append(AIReviewEvent(
        action="undo_structure_change", source_document_id=draft.source_document_id,
        reviewer_id=reviewer_id, page_number=event.page_number,
        source_record_ids=event.source_record_ids, result_record_ids=event.result_record_ids,
        payload={"reverted_event_id": event.event_id},
    ))
    return updated


def confirm_page_complete(
    draft: AIDocumentDraft, state: AIReviewState, page_number: int, *, reviewer_id: str,
) -> AIReviewState:
    _check_current(draft, state)
    if not reviewer_id or page_number not in {page.page_number for page in draft.pages}:
        raise AIReviewError("invalid_page_confirmation")
    updated = state.model_copy(deep=True)
    updated.confirmed_complete_pages.add(page_number)
    updated.history.append(AIReviewEvent(
        action="confirm_page_complete", source_document_id=draft.source_document_id, reviewer_id=reviewer_id, page_number=page_number,
    ))
    return updated


def resolve_review_issue(
    draft: AIDocumentDraft, state: AIReviewState, issue_id: str, *, reviewer_id: str,
    detail: str,
) -> AIReviewState:
    _check_current(draft, state)
    issues = {issue.id: issue for issue in draft.issues}
    issue = issues.get(issue_id)
    if issue is None or not reviewer_id or not detail.strip():
        raise AIReviewError("invalid_issue_resolution")
    updated = state.model_copy(deep=True)
    updated.resolved_issue_ids.add(issue_id)
    updated.history.append(AIReviewEvent(
        action="resolve_issue", source_document_id=draft.source_document_id, reviewer_id=reviewer_id,
        page_number=issue.page_number or 1, source_record_ids=issue.record_ids,
        detail=detail,
    ))
    return updated


def reconcile_reprocessed_pages(
    old_draft: AIDocumentDraft, new_draft: AIDocumentDraft, state: AIReviewState,
) -> AIReviewState:
    """Preserve approvals only for byte-equivalent page observations.

    Changed pages lose active decisions, completeness confirmation, and issue
    resolutions. Their old decisions remain as inactive history for audit.
    """
    _check_current(old_draft, state)
    if old_draft.source_document_id != new_draft.source_document_id:
        raise AIReviewError("source_document_changed")
    old_pages = {page.page_number: page for page in old_draft.pages}
    new_pages = {page.page_number: page for page in new_draft.pages}
    changed = {
        page_number for page_number in set(old_pages) | set(new_pages)
        if page_number not in old_pages or page_number not in new_pages
        or old_pages[page_number].extraction_fingerprint != new_pages[page_number].extraction_fingerprint
        or old_pages[page_number].status != new_pages[page_number].status
    }
    updated = state.model_copy(deep=True)
    if not changed:
        updated.revision_id = new_draft.revision_id
        return updated
    old_rows = {
        row.id: page.page_number
        for page in old_draft.pages for table in page.tables for row in table.rows
    }
    old_rows.update({row.id: row.page_number for row in state.manual_rows})
    for record_id, decision in list(updated.decisions.items()):
        if old_rows.get(record_id) in changed:
            updated.decisions[record_id] = decision.model_copy(update={"active": False})
    updated.manual_rows = [
        row.model_copy(update={"active": False}) if row.page_number in changed else row
        for row in updated.manual_rows
    ]
    updated.confirmed_complete_pages.difference_update(changed)
    old_issue_pages = {issue.id: issue.page_number for issue in old_draft.issues}
    updated.resolved_issue_ids = {
        issue_id for issue_id in updated.resolved_issue_ids
        if old_issue_pages.get(issue_id) not in changed
    }
    updated.revision_id = new_draft.revision_id
    for page_number in sorted(changed):
        old_fingerprint = old_pages[page_number].extraction_fingerprint if page_number in old_pages else None
        new_fingerprint = new_pages[page_number].extraction_fingerprint if page_number in new_pages else None
        updated.history.append(AIReviewEvent(
            action="page_reprocessed", source_document_id=new_draft.source_document_id, reviewer_id="system", page_number=page_number,
            detail="Prior page decisions were invalidated because the extraction changed.",
            payload={"old_fingerprint": old_fingerprint, "new_fingerprint": new_fingerprint},
        ))
    return updated


def assert_review_ready_for_export(draft: AIDocumentDraft, state: AIReviewState) -> None:
    """Safety guard only; this package contains no exporter or sourcing adapter."""
    _check_current(draft, state)
    if not draft.pages:
        raise AIReviewError("empty_document")
    expected_page_numbers = set(draft.expected_page_numbers)
    if not expected_page_numbers:
        raise AIReviewError("expected_page_manifest_required")
    page_numbers = {page.page_number for page in draft.pages}
    if page_numbers != expected_page_numbers:
        raise AIReviewError("page_coverage_incomplete")
    if any(page.status in {"failed", "incomplete"} or page.error_code for page in draft.pages):
        raise AIReviewError("page_processing_incomplete")
    pages_by_number = {page.page_number: page for page in draft.pages}
    for row in state.manual_rows:
        if not row.active:
            continue
        page = pages_by_number.get(row.page_number)
        if row.source_document_id != draft.source_document_id or page is None:
            raise AIReviewError("manual_row_provenance_mismatch")
        if (not row.page_extraction_fingerprint
                or row.page_extraction_fingerprint != page.extraction_fingerprint):
            raise AIReviewError("manual_row_page_revision_mismatch")
    if state.confirmed_complete_pages != page_numbers:
        raise AIReviewError("page_completeness_not_human_confirmed")
    required = {row_id: page_number for row_id, (page_number, _) in _rows(draft).items()}
    required.update({row.id: row.page_number for row in state.manual_rows if row.active})
    if not required:
        raise AIReviewError("empty_document")
    for record_id, page_number in required.items():
        decision = state.decisions.get(record_id)
        if decision is None or not decision.active:
            raise AIReviewError("row_has_no_active_human_decision")
        if (decision.record_id != record_id
                or decision.source_document_id != draft.source_document_id
                or decision.page_number != page_number):
            raise AIReviewError("review_decision_provenance_mismatch")
    active_lineage_sources = _active_lineage_sources(state)
    included = {
        record_id for record_id in required
        if state.decisions[record_id].disposition in {"confirmed", "corrected"}
    }
    if included.intersection(active_lineage_sources):
        raise AIReviewError("conflicting_active_lineage")
    if not included:
        raise AIReviewError("empty_document")
    unresolved = {
        issue.id for issue in draft.issues if issue.severity in {"review", "blocking"}
    } - state.resolved_issue_ids
    if unresolved:
        raise AIReviewError("review_issues_unresolved")
    if draft.extraction_is_verified or draft.export_authorized:
        # These are fixed-false contract fields; reject any malformed serialized draft.
        raise AIReviewError("model_authorization_forbidden")
