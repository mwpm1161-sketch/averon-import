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
    "resolve_issue", "page_reprocessed", "undo_structure_change",
    "classify_row_role",
]
AIHumanRowRole = Literal[
    "header", "column_numbering", "section", "standalone_item",
    "component", "continuation", "note", "unknown",
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
    # Supplied by the trusted caller from the source PDF, never inferred from OCR output.
    expected_page_numbers: list[int] = Field(default_factory=list)
    issues: list[AIReviewIssue] = Field(default_factory=list)
    source_envelope: dict[str, Any] = Field(default_factory=dict)
    extraction_is_verified: Literal[False] = False
    export_authorized: Literal[False] = False

    @model_validator(mode="after")
    def pages_are_unique_and_ordered(self) -> AIDocumentDraft:
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("document pages must be unique and ordered")
        if any(type(number) is not int or number < 1 for number in self.expected_page_numbers):
            raise ValueError("expected page numbers must be positive integers")
        if self.expected_page_numbers != sorted(set(self.expected_page_numbers)):
            raise ValueError("expected page numbers must be unique and ordered")
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
    invalidation_reason: Literal["role_change", "page_reprocessed", "structure_change"] | None = None


class AIReviewRowRole(_StrictModel):
    """Human-reviewed structural role, separate from immutable AI observations."""

    record_id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    page_extraction_fingerprint: str = Field(min_length=1)
    table_id: str | None = None
    role: AIHumanRowRole
    parent_record_id: str | None = None
    reviewer_id: str
    active: bool = True

    @model_validator(mode="after")
    def parent_matches_role(self) -> AIReviewRowRole:
        if not self.reviewer_id:
            raise ValueError("row role requires a reviewer")
        if self.role == "component":
            if not self.parent_record_id or self.parent_record_id == self.record_id:
                raise ValueError("component role requires a different parent record")
        elif self.parent_record_id is not None:
            raise ValueError("only component roles may have a parent record")
        return self


class AIReviewRoleChange(_StrictModel):
    record_id: str
    previous: AIReviewRowRole | None = None
    current: AIReviewRowRole
    invalidated_dependents: list[AIReviewRowRole] = Field(default_factory=list)
    invalidated_decisions: list[AIReviewDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def change_identity_matches(self) -> AIReviewRoleChange:
        if self.current.record_id != self.record_id or not self.current.active:
            raise ValueError("row role change identity mismatch")
        if self.previous is not None and self.previous.record_id != self.record_id:
            raise ValueError("previous row role identity mismatch")
        if any(item.record_id == self.record_id for item in self.invalidated_dependents):
            raise ValueError("target role is not a dependent")
        allowed_ids = {self.record_id, *(item.record_id for item in self.invalidated_dependents)}
        if any(item.record_id not in allowed_ids or item.active for item in self.invalidated_decisions):
            raise ValueError("invalidated row decision snapshot mismatch")
        return self


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
    role_change: AIReviewRoleChange | None = None
    invalidated_row_roles: list[AIReviewRowRole] = Field(default_factory=list)

    @model_validator(mode="after")
    def typed_role_event_matches_action(self) -> AIReviewEvent:
        if (self.action == "classify_row_role") != (self.role_change is not None):
            raise ValueError("typed row role event is required for role classification")
        if self.role_change is not None and (
            self.role_change.record_id not in self.source_record_ids
            or self.role_change.current.page_number != self.page_number
        ):
            raise ValueError("typed row role event provenance mismatch")
        return self


class AIManualRow(_StrictModel):
    id: str
    source_document_id: str
    page_number: int = Field(ge=1)
    page_extraction_fingerprint: str | None = None
    reviewer_id: str
    values: dict[str, str]
    source_record_ids: list[str] = Field(default_factory=list)
    active: bool = True


class AIReviewState(_StrictModel):
    # Legacy review.json files omit this version and row_roles. Pydantic defaults
    # preserve their existing decisions/history while the next write records v2.
    review_schema_version: Literal[2] = 2
    source_document_id: str
    revision_id: str
    decisions: dict[str, AIReviewDecision] = Field(default_factory=dict)
    row_roles: dict[str, AIReviewRowRole] = Field(default_factory=dict)
    manual_rows: list[AIManualRow] = Field(default_factory=list)
    confirmed_complete_pages: set[int] = Field(default_factory=set)
    resolved_issue_ids: set[str] = Field(default_factory=set)
    history: list[AIReviewEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def decision_keys_match(self) -> AIReviewState:
        if any(key != decision.record_id for key, decision in self.decisions.items()):
            raise ValueError("review decision key mismatch")
        for key, role in self.row_roles.items():
            if key != role.record_id or role.source_document_id != self.source_document_id:
                raise ValueError("review row role key or source mismatch")
            if not role.active or role.role != "component":
                continue
            parent = self.row_roles.get(role.parent_record_id or "")
            if (parent is None or not parent.active or parent.role != "standalone_item"
                    or parent.source_document_id != role.source_document_id
                    or parent.page_number != role.page_number
                    or parent.page_extraction_fingerprint != role.page_extraction_fingerprint
                    or (parent.table_id is not None and role.table_id is not None
                        and parent.table_id != role.table_id)):
                raise ValueError("active component role has no compatible active parent")
        return self
