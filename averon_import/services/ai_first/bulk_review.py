"""Bounded, explicit Human Review batches; observations remain immutable."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from .contracts import AIDocumentDraft, AIReviewState
from .editor_draft import consume_editor, current_editor, review_token
from .review import (
    AIReviewError, _active_lineage_sources, _row_role_source,
    assert_no_open_review_conflicts, record_row_decision, record_row_role_classification,
)

BulkRole = Literal["standalone_item", "component", "header", "column_numbering", "section", "continuation", "note"]
SERVICE_ROLES = {"header", "column_numbering", "section", "continuation", "note"}


class RowProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    page_number: StrictInt = Field(ge=1)
    extraction_fingerprint: StrictStr = Field(min_length=1, max_length=256)
    table_id: StrictStr | None = Field(default=None, max_length=256)


class BulkReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    mode: Literal["preview", "commit"]
    operation_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    review_token: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    draft_revision_id: StrictStr = Field(min_length=1, max_length=256)
    editor_revision: StrictInt = Field(ge=0)
    record_ids: list[StrictStr] = Field(min_length=1, max_length=100)
    role: BulkRole
    intent: Literal["confirm_values", "exclude_from_positions"]
    parents: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=100)
    provenance: dict[StrictStr, RowProvenance] = Field(max_length=100)
    values: dict[StrictStr, dict[StrictStr, StrictStr]] = Field(max_length=100)

    @model_validator(mode="after")
    def complete_bounded_snapshot(self):
        ids = set(self.record_ids)
        if len(ids) != len(self.record_ids) or any(not key or len(key) > 256 for key in ids):
            raise ValueError("bulk_record_ids_invalid")
        if set(self.values) != ids or set(self.provenance) != ids:
            raise ValueError("bulk_snapshot_incomplete")
        if (self.role in SERVICE_ROLES) != (self.intent == "exclude_from_positions"):
            raise ValueError("bulk_role_intent_mismatch")
        if self.role == "component":
            if set(self.parents) != ids or any(not parent or len(parent) > 256 for parent in self.parents.values()):
                raise ValueError("bulk_parent_snapshot_incomplete")
        elif self.parents:
            raise ValueError("bulk_parent_only_for_component")
        total = 0
        for values in self.values.values():
            size = sum(len(key) + len(value) for key, value in values.items())
            if not values or len(values) > 64 or size > 30_000 or any(len(key) > 64 or len(value) > 2000 for key, value in values.items()):
                raise ValueError("bulk_values_out_of_range")
            total += size
        if total > 1_000_000:
            raise ValueError("bulk_values_out_of_range")
        return self


class BulkReviewError(AIReviewError):
    def __init__(self, code: str, record_id: str | None = None):
        super().__init__(code)
        self.record_id = record_id


class _BulkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    record_ids: list[StrictStr] = Field(min_length=1, max_length=100)
    row_count: StrictInt = Field(ge=1, le=100)
    role: BulkRole
    intent: Literal["confirm_values", "exclude_from_positions"]
    invalidated_decision_ids: list[StrictStr]
    invalidated_role_ids: list[StrictStr]
    corrected_cell_count: StrictInt = Field(ge=0, le=6400)


class _OperationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    version: Literal[1]
    operation_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    request_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: StrictStr = Field(min_length=1, max_length=256)
    summary: _BulkSummary


def _digest(request: BulkReviewRequest, reviewer_id: str) -> str:
    payload = {"version": 1, "reviewer_id": reviewer_id,
               "request": request.model_dump(mode="json", exclude={"mode"})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _receipt(state: AIReviewState, operation_id: str):
    found = None
    for event in state.history:
        item = event.payload.get("bulk_operation")
        if item is None:
            continue
        try:
            item = _OperationReceipt.model_validate(item).model_dump()
            if (item["summary"]["row_count"] != len(item["summary"]["record_ids"])
                    or item["summary"]["row_count"] != len(set(item["summary"]["record_ids"]))
                    or (item["summary"]["role"] in SERVICE_ROLES) != (item["summary"]["intent"] == "exclude_from_positions")):
                raise ValueError("receipt_count_mismatch")
        except (ValidationError, ValueError):
            raise BulkReviewError("bulk_receipt_invalid") from None
        if item["operation_id"] == operation_id:
            if found is not None:
                raise BulkReviewError("bulk_receipt_invalid")
            found = item
    return found


def _source(draft, state, record_id):
    if record_id in _active_lineage_sources(state):
        raise BulkReviewError("review_source_superseded", record_id)
    page_number, page, table_id = _row_role_source(draft, state, record_id)
    if page.status not in {"extracted", "no_specification"} or not page.extraction_fingerprint:
        raise BulkReviewError("page_processing_incomplete", record_id)
    if table_id is None:
        row = next(row for row in state.manual_rows if row.id == record_id and row.active)
        if row.source_document_id != draft.source_document_id:
            raise BulkReviewError("bulk_row_provenance_mismatch", record_id)
        return page, None, row, dict(row.values)
    if page.status != "extracted":
        raise BulkReviewError("page_processing_incomplete", record_id)
    table = next((item for item in page.tables if item.id == table_id), None)
    row = next((item for item in (table.rows if table else []) if item.id == record_id), None)
    if table is None or row is None:
        raise BulkReviewError("bulk_column_provenance_invalid", record_id)
    indexes = [column.index for column in table.columns]
    if (len(indexes) != len(set(indexes)) or not indexes
            or any(column.source_document_id != draft.source_document_id or column.page_number != page_number
                   or column.table_id != table.id for column in table.columns)
            or row.source_document_id != draft.source_document_id or row.page_number != page_number
            or table.source_document_id != draft.source_document_id or table.page_number != page_number
            or len({cell.column_index for cell in row.raw_cells}) != len(row.raw_cells)
            or any(cell.source_document_id != draft.source_document_id or cell.page_number != page_number
                   or cell.table_id != table.id or cell.row_id != row.id or cell.column_index not in indexes
                   for cell in row.raw_cells)):
        raise BulkReviewError("bulk_column_provenance_invalid", record_id)
    cells = {cell.column_index: cell.raw_value or "" for cell in row.raw_cells}
    return page, table, row, {f"cell:{index}": cells.get(index, "") for index in indexes}


def prepare_bulk_review(draft: AIDocumentDraft, state: AIReviewState, editor, request, reviewer_id):
    if request.draft_revision_id != draft.revision_id or state.revision_id != draft.revision_id:
        raise BulkReviewError("review_state_revision_mismatch")
    view = current_editor(draft, editor)
    if view["stale"] or request.editor_revision != view["revision"]:
        raise BulkReviewError("bulk_editor_revision_conflict")
    if not reviewer_id:
        raise BulkReviewError("invalid_review_decision")
    all_ids = [value for page in draft.pages for table in page.tables
               for value in [table.id, *(column.id for column in table.columns),
                   *(row.id for row in table.rows), *(cell.id for row in table.rows for cell in row.raw_cells)]]
    all_ids.extend(row.id for row in state.manual_rows if row.active)
    if len(all_ids) != len(set(all_ids)):
        raise BulkReviewError("bulk_source_identity_invalid")
    sources = {}
    for record_id in request.record_ids:
        page, table, row, original = _source(draft, state, record_id)
        expected = request.provenance[record_id]
        if (expected.page_number != page.page_number or expected.extraction_fingerprint != page.extraction_fingerprint
                or expected.table_id != (table.id if table else None)):
            raise BulkReviewError("bulk_row_provenance_mismatch", record_id)
        if set(request.values[record_id]) != set(original):
            raise BulkReviewError("bulk_column_provenance_invalid", record_id)
        prior = state.decisions.get(record_id)
        if prior and (prior.active or prior.invalidation_reason == "role_change") and not set(prior.values) <= set(original):
            raise BulkReviewError("bulk_column_provenance_invalid", record_id)
        saved_values = view["edits"]["row_values"].get(record_id, {})
        if any(key not in request.values[record_id] or request.values[record_id][key] != value
               for key, value in saved_values.items()):
            raise BulkReviewError("bulk_editor_values_mismatch", record_id)
        pending_role = view["edits"]["row_roles"].get(record_id)
        if pending_role and (pending_role["role"] != request.role
                             or pending_role.get("parent_record_id") != request.parents.get(record_id)):
            raise BulkReviewError("bulk_editor_role_mismatch", record_id)
        try:
            assert_no_open_review_conflicts(draft, state, [record_id])
            if request.role == "component":
                parent_id = request.parents[record_id]
                if parent_id in request.record_ids:
                    raise AIReviewError("component_parent_not_confirmed_item")
                parent_page, parent_table, _, parent_columns = _source(draft, state, parent_id)
                parent_role, parent_decision = state.row_roles.get(parent_id), state.decisions.get(parent_id)
                if (parent_role is None or not parent_role.active or parent_role.role != "standalone_item"
                        or parent_role.page_extraction_fingerprint != parent_page.extraction_fingerprint
                        or parent_role.source_document_id != draft.source_document_id
                        or parent_role.page_number != parent_page.page_number
                        or parent_role.table_id != (parent_table.id if parent_table else None)
                        or parent_decision is None or not parent_decision.active or not parent_decision.human_confirmed
                        or parent_decision.invalidation_reason is not None
                        or parent_decision.source_document_id != draft.source_document_id or parent_decision.page_number != parent_page.page_number
                        or parent_decision.disposition not in {"confirmed", "corrected"}
                        or not set(parent_decision.values) <= set(parent_columns)
                        or parent_page.page_number != page.page_number
                        or (parent_table is not None and table is not None and parent_table.id != table.id)):
                    raise AIReviewError("component_parent_not_confirmed_item")
                assert_no_open_review_conflicts(draft, state, [parent_id])
        except AIReviewError as exc:
            raise BulkReviewError(str(exc), record_id) from None
        sources[record_id] = original
    updated = state.model_copy(deep=True)
    start = len(updated.history)
    for record_id in request.record_ids:
        updated = record_row_role_classification(draft, updated, record_id, role=request.role,
                    parent_record_id=request.parents.get(record_id), reviewer_id=reviewer_id)
        values = request.values[record_id]
        if request.intent == "exclude_from_positions":
            # Exclusion retains any explicitly edited cells in the existing ledger.
            updated = record_row_decision(draft, updated, record_id, disposition="corrected",
                        values=values, human_confirmed=False, reviewer_id=reviewer_id)
            updated = record_row_decision(draft, updated, record_id, disposition="excluded", reviewer_id=reviewer_id)
        else:
            changed = values != sources[record_id]
            updated = record_row_decision(draft, updated, record_id,
                        disposition="corrected" if changed else "confirmed", values=values,
                        human_confirmed=True, reviewer_id=reviewer_id)
    invalidated = sorted({decision.record_id for event in updated.history[start:]
                          if event.role_change for decision in event.role_change.invalidated_decisions})
    summary = {"record_ids": request.record_ids, "row_count": len(request.record_ids),
               "role": request.role, "intent": request.intent, "invalidated_decision_ids": invalidated,
               "invalidated_role_ids": sorted({role.record_id for event in updated.history[start:]
                    if event.role_change for role in event.role_change.invalidated_dependents}),
               "corrected_cell_count": sum(value != sources[key].get(column, "")
                    for key, values in request.values.items() for column, value in values.items())}
    try:
        updated = AIReviewState.model_validate(updated.model_dump())
    except ValidationError:
        raise BulkReviewError("bulk_review_state_invalid") from None
    return updated, summary


def execute_bulk_review(store, draft, state, editor, request: BulkReviewRequest, reviewer_id: str):
    """Caller holds the existing document mutation lock for this entire function.

    The receipt lives in the same review.json as the decisions. write_many's
    existing redo journal publishes that ledger and consumed editor together.
    """
    digest = _digest(request, reviewer_id)
    receipt = _receipt(state, request.operation_id)
    if receipt:
        if receipt["request_sha256"] != digest or receipt["reviewer_id"] != reviewer_id:
            raise BulkReviewError("bulk_operation_id_reused")
        return {"saved": True, "replayed": True, "can_commit": False, "operation_id": request.operation_id,
                "review_token": review_token(draft, state, editor), **receipt["summary"]}
    if request.review_token != review_token(draft, state, editor):
        raise BulkReviewError("review_revision_conflict")
    try:
        updated, summary = prepare_bulk_review(draft, state, editor, request, reviewer_id)
    except AIReviewError as exc:
        if request.mode == "preview":
            return {"saved": False, "can_commit": False, "operation_id": request.operation_id,
                    "errors": [{"code": str(exc), "record_id": getattr(exc, "record_id", None)}]}
        raise
    if request.mode == "preview":
        return {"saved": False, "can_commit": True, "replayed": False, "operation_id": request.operation_id,
                "errors": [], **summary}
    updated.history[-1].payload["bulk_operation"] = {"version": 1, "operation_id": request.operation_id,
        "request_sha256": digest, "reviewer_id": reviewer_id, "summary": summary}
    consumed = consume_editor(editor, values=request.record_ids, roles=request.record_ids)
    writes = {"review.json": updated.model_dump(mode="json")}
    if consumed is not None:
        writes["editor-draft.json"] = consumed
    store.write_many(writes)
    return {"saved": True, "can_commit": False, "replayed": False, "operation_id": request.operation_id,
            "review_token": review_token(draft, updated, consumed), **summary}
