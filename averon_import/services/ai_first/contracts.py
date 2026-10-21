"""Loss-preserving AI-first draft and Human Review V2 contracts.

These DTOs carry untrusted model observations. They never confer correctness,
completeness, export permission, or sourcing permission.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

PageStatus = Literal["extracted", "no_specification", "incomplete", "failed"]
ReviewDisposition = Literal["confirmed", "corrected", "excluded"]
ReviewAction = Literal[
    "confirm_row", "edit_value", "exclude_row", "add_row", "split_row",
    "merge_rows", "confirm_handwriting", "confirm_page_complete",
    "resolve_issue", "page_reprocessed",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class AIValueProposal(_StrictModel):
    """An optional canonical-value suggestion, always linked to raw evidence."""

    field_name: str
    suggested_value: str | None
    source_cell_ids: list[str] = Field(min_length=1)
    uncertainty_reasons: list[str] = Field(default_factory=list)
    reviewed: Literal[False] = False


class AIColumn(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    table_id: str
    index: int = Field(ge=0)
    raw_header: str | None
    canonical_field_candidate: str | None = None
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)


class AIHandwritingMark(_StrictModel):
    text: str | None
    scope: Literal["cell", "row", "page"]
    bbox_candidate: dict[str, Any] | None = None
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)


class AICell(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    table_id: str
    row_id: str
    column_index: int = Field(ge=0)
    raw_value: str | None
    alternatives: list[str] | None = None
    unreadable: bool | None = None
    struck_through: bool | None = None
    handwritten: bool | None = None
    handwritten_replacements: list[AIHandwritingMark] | None = None
    row_span: int | None = Field(default=None, ge=1)
    column_span: int | None = Field(default=None, ge=1)
    bbox_candidate: dict[str, Any] | None = None
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)


class AIRow(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    table_id: str
    index: int = Field(ge=0)
    model_kind: str | None
    server_role_candidate: str | None = None
    role_verified: Literal[False] = False
    raw_cells: list[AICell] = Field(default_factory=list)
    canonical_value_proposals: list[AIValueProposal] = Field(default_factory=list)
    struck_through: bool | None = None
    handwritten: bool | None = None
    handwritten_replacements: list[AIHandwritingMark] | None = None
    continuation_of: int | None = Field(default=None, ge=0)
    continues_previous_page: bool | None = None
    bbox_candidate: dict[str, Any] | None = None
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def never_verified_by_model(self) -> AIRow:
        if self.role_verified is not False:
            raise ValueError("AI-first model rows cannot be verified by extraction")
        return self


class AITable(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    index: int = Field(ge=0)
    columns: list[AIColumn]
    rows: list[AIRow]
    continues_previous_page: bool | None = None
    bbox_candidate: dict[str, Any] | None = None
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)


class AIReviewIssue(_StrictModel):
    id: str
    source_document_id: str
    code: str
    severity: Literal["info", "review", "blocking"]
    page_number: int | None = Field(default=None, ge=1)
    record_ids: list[str] = Field(default_factory=list)
    detail: str
    resolved: Literal[False] = False


class AIPageExtraction(_StrictModel):
    source_document_id: str
    page_number: int = Field(ge=1)
    extraction_fingerprint: str
    status: PageStatus
    tables: list[AITable] = Field(default_factory=list)
    unreadable_fragments: list[str] = Field(default_factory=list)
    page_handwriting: list[AIHandwritingMark] = Field(default_factory=list)
    error_code: str | None = None
    issues: list[AIReviewIssue] = Field(default_factory=list)
    raw_model_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_is_consistent(self) -> AIPageExtraction:
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed page requires an error code")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("only failed pages may carry an error code")
        if self.status == "no_specification" and self.tables:
            raise ValueError("page with tables cannot be marked no_specification")
        return self


class AIDocumentDraft(_StrictModel):
    source_document_id: str
    revision_id: str
    pages: list[AIPageExtraction]
    issues: list[AIReviewIssue] = Field(default_factory=list)
    source_envelope: dict[str, Any] = Field(default_factory=dict)
    extraction_is_verified: Literal[False] = False
    export_authorized: Literal[False] = False

    @model_validator(mode="after")
    def pages_are_unique_and_ordered(self) -> AIDocumentDraft:
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("document pages must be unique and ordered")
        if any(page.source_document_id != self.source_document_id for page in self.pages):
            raise ValueError("page source document mismatch")
        return self


class AIReviewDecision(_StrictModel):
    record_id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    disposition: ReviewDisposition
    reviewer_id: str
    values: dict[str, str] = Field(default_factory=dict)
    active: bool = True


class AIReviewEvent(_StrictModel):
    event_id: str = Field(default_factory=lambda: f"review-event:{uuid4().hex}")
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: ReviewAction
    source_document_id: str
    reviewer_id: str
    page_number: int = Field(ge=1)
    source_record_ids: list[str] = Field(default_factory=list)
    result_record_ids: list[str] = Field(default_factory=list)
    detail: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AIManualRow(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    reviewer_id: str
    values: dict[str, str]
    source_record_ids: list[str] = Field(default_factory=list)
    active: bool = True


class AIReviewState(_StrictModel):
    source_document_id: str
    revision_id: str
    decisions: dict[str, AIReviewDecision] = Field(default_factory=dict)
    manual_rows: list[AIManualRow] = Field(default_factory=list)
    confirmed_complete_pages: set[int] = Field(default_factory=set)
    resolved_issue_ids: set[str] = Field(default_factory=set)
    history: list[AIReviewEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def decision_keys_match(self) -> AIReviewState:
        if any(key != decision.record_id for key, decision in self.decisions.items()):
            raise ValueError("review decision key mismatch")
        return self
