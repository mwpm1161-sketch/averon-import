"""Isolated AI-first extraction foundation; not wired into production flows."""

from averon_import.services.ai_first.assembly import (
    assemble_document_draft, failed_page, page_from_payload,
)
from averon_import.services.ai_first.contracts import (
    AICell, AIColumn, AIDocumentDraft, AIPageExtraction, AIRow, AITable,
    AIReviewDecision, AIReviewEvent, AIReviewIssue, AIReviewState, AIValueProposal,
)

__all__ = [
    "AICell", "AIColumn", "AIDocumentDraft", "AIPageExtraction", "AIRow",
    "AITable", "AIReviewDecision", "AIReviewEvent", "AIReviewIssue",
    "AIReviewState", "AIValueProposal", "assemble_document_draft",
    "failed_page", "page_from_payload",
]

from averon_import.services.ai_first.review import (
    AIReviewError, assert_review_ready_for_export, confirm_page_complete,
    new_review_state, reconcile_reprocessed_pages, record_handwriting_confirmation,
    record_row_decision, record_structure_change, resolve_review_issue,
)

__all__ += [
    "AIReviewError", "assert_review_ready_for_export", "confirm_page_complete",
    "new_review_state", "reconcile_reprocessed_pages", "record_handwriting_confirmation",
    "record_row_decision", "record_structure_change", "resolve_review_issue",
]
