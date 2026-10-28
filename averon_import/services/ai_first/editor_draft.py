"""Private, versioned editor buffers. Saving never confirms an observation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contracts import AIDocumentDraft, AIHumanRowRole, AIReviewDecision, AIReviewState


class DraftEditError(ValueError):
    def __init__(self, code: str, record_id: str | None = None):
        super().__init__(code)
        self.record_id = record_id


class PendingRole(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    role: AIHumanRowRole
    # Incomplete component assignments may be saved, but cannot become decisions.
    parent_record_id: str | None = Field(default=None, max_length=256)


class EditorEdits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    row_values: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=500)
    row_roles: dict[str, PendingRole] = Field(default_factory=dict, max_length=500)
    issue_notes: dict[str, str] = Field(default_factory=dict, max_length=500)
    add_row_page: int | None = Field(default=None, ge=1)
    add_row_values: dict[str, str] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def bounded_text(self):
        groups = [*self.row_values.values(), self.issue_notes, self.add_row_values]
        for values in groups:
            if len(values) > 500 or any(len(k) > 256 or len(v) > 2000 for k, v in values.items()):
                raise ValueError("draft_text_out_of_range")
        if sum(len(k) + len(v) for values in groups for k, v in values.items()) > 1_000_000:
            raise ValueError("draft_text_out_of_range")
        return self


class EditorDraftEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    saved_at: str
    reviewer_id: str
    edits: EditorEdits


class EditorDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    version: Literal[1]
    revision: int = Field(ge=1)
    source_document_id: str
    draft_revision_id: str
    edits: EditorEdits
    history: list[EditorDraftEvent]


def review_token(draft: AIDocumentDraft, review: AIReviewState, editor: dict | None) -> str:
    review_data = review.model_dump(mode="json")
    review_data["confirmed_complete_pages"] = sorted(review.confirmed_complete_pages)
    review_data["resolved_issue_ids"] = sorted(review.resolved_issue_ids)
    data = [draft.source_document_id, draft.revision_id, review_data, editor]
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def current_editor(draft: AIDocumentDraft, editor: dict | None) -> dict[str, Any]:
    if not editor:
        return {"version": 1, "revision": 0, "edits": EditorEdits().model_dump(), "stale": False}
    try:
        EditorDraft.model_validate(editor)
    except ValidationError:
        raise DraftEditError("editor_draft_invalid") from None
    return {**editor, "stale": editor.get("draft_revision_id") != draft.revision_id
            or editor.get("source_document_id") != draft.source_document_id}


def save_editor(draft, review, previous, edits: EditorEdits, *, expected_token: str, reviewer_id: str):
    if expected_token != review_token(draft, review, previous):
        raise DraftEditError("review_revision_conflict")
    rows = {r.id: (p, r) for p in draft.pages for t in p.tables for r in t.rows}
    rows.update({r.id: (next((p for p in draft.pages if p.page_number == r.page_number), None), r)
                 for r in review.manual_rows if r.active})
    for record_id in set(edits.row_values) | set(edits.row_roles):
        if record_id not in rows or rows[record_id][0] is None:
            raise DraftEditError("draft_row_not_current", record_id)
        page, row = rows[record_id]
        allowed = {f"cell:{c.column_index}" for c in getattr(row, "raw_cells", [])}
        if not hasattr(row, "raw_cells"):
            allowed = set(row.values)
        if any(key not in allowed for key in edits.row_values.get(record_id, {})):
            raise DraftEditError("draft_column_not_current", record_id)
        role = edits.row_roles.get(record_id)
        if role and role.parent_record_id and role.parent_record_id not in rows:
            raise DraftEditError("draft_parent_not_current", record_id)
    issue_ids = {i.id for i in draft.issues} | {i.id for p in draft.pages for i in p.issues}
    if any(key not in issue_ids for key in edits.issue_notes):
        raise DraftEditError("draft_issue_not_current")
    if edits.add_row_values and edits.add_row_page not in {p.page_number for p in draft.pages}:
        raise DraftEditError("draft_page_not_current")
    event = {"saved_at": datetime.now(timezone.utc).isoformat(), "reviewer_id": reviewer_id,
             "edits": edits.model_dump(mode="json")}
    return {"version": 1, "revision": int((previous or {}).get("revision", 0)) + 1,
            "source_document_id": draft.source_document_id, "draft_revision_id": draft.revision_id,
            "edits": event["edits"], "history": [*(previous or {}).get("history", []), event]}


def consume_editor(editor: dict | None, *, values=(), roles=(), issue_id=None, add_row=False):
    if not editor:
        return None
    updated = json.loads(json.dumps(editor))
    edits = updated["edits"]
    for key in values:
        edits["row_values"].pop(key, None)
    for key in roles:
        edits["row_roles"].pop(key, None)
    if issue_id:
        edits["issue_notes"].pop(issue_id, None)
    if add_row:
        edits["add_row_values"] = {}
        edits["add_row_page"] = None
    updated["revision"] += 1
    return updated


def export_review(draft: AIDocumentDraft, review: AIReviewState, editor: dict | None) -> AIReviewState:
    """Use saved edits as unconfirmed values; never mutate the actual review ledger."""
    if not editor:
        return review
    if current_editor(draft, editor)["stale"]:
        raise DraftEditError("editor_draft_revision_mismatch")
    edits = EditorEdits.model_validate(editor["edits"])
    result = review.model_copy(deep=True)
    if edits.add_row_page and any(value.strip() for value in edits.add_row_values.values()):
        result.confirmed_complete_pages.discard(edits.add_row_page)
    rows = {r.id: p.page_number for p in draft.pages for t in p.tables for r in t.rows}
    rows.update({r.id: r.page_number for r in review.manual_rows if r.active})
    for record_id in set(edits.row_values) | set(edits.row_roles):
        if record_id not in rows:
            continue
        previous = result.decisions.get(record_id)
        values = {**(previous.values if previous else {}), **edits.row_values.get(record_id, {})}
        result.decisions[record_id] = AIReviewDecision(
            record_id=record_id, source_document_id=draft.source_document_id,
            page_number=rows[record_id], reviewer_id="saved-editor-draft",
            disposition="excluded" if previous and previous.disposition == "excluded" else "corrected",
            values=values, human_confirmed=False,
        )
    return result


def editor_export_snapshot(draft, review, editor: dict | None, page_numbers) -> dict | None:
    """A fragment workbook must not reveal editor input from unselected pages."""
    if editor is None:
        return None
    selected = set(page_numbers)
    row_ids = {row.id for page in draft.pages if page.page_number in selected
               for table in page.tables for row in table.rows}
    row_ids.update(row.id for row in review.manual_rows if row.page_number in selected)
    evidence_ids = row_ids | {table.id for page in draft.pages if page.page_number in selected for table in page.tables}
    evidence_ids.update(cell.id for page in draft.pages if page.page_number in selected
                        for table in page.tables for row in table.rows for cell in row.raw_cells)
    issue_ids = {issue.id for issue in [*draft.issues, *(i for p in draft.pages for i in p.issues)]
                 if issue.page_number in selected or (issue.record_ids and evidence_ids.intersection(issue.record_ids))}

    def scoped(values):
        edits = EditorEdits.model_validate(values).model_dump(mode="json")
        edits["row_values"] = {key: value for key, value in edits["row_values"].items() if key in row_ids}
        edits["row_roles"] = {key: value for key, value in edits["row_roles"].items() if key in row_ids}
        edits["issue_notes"] = {key: value for key, value in edits["issue_notes"].items() if key in issue_ids}
        if edits["add_row_page"] not in selected:
            edits["add_row_page"], edits["add_row_values"] = None, {}
        return edits

    return {**editor, "edits": scoped(editor["edits"]),
            "history": [{**event, "edits": scoped(event["edits"])} for event in editor["history"]],
            "export_page_numbers": sorted(selected)}
