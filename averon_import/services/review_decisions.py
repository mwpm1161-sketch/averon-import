"""Audited human resolution for already-produced OCR evidence.

This layer is intentionally downstream of OCR and reconstruction.  It may
resolve an existing candidate or a bounded continuation relation, but it
cannot create a value without matching evidence and it never changes the raw
OCR evidence kept in ``ocr_metadata``.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.page_disposition import CONFIRMED_NON_SPEC, SPEC_OUTPUT
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    critical_field_count,
    refresh_review_state,
)
from averon_import.services.row_assembler import SpecificationRowAssembler


FIELD_DECISION = "ACCEPT_FIELD_CANDIDATE"
RELATION_DECISION = "ACCEPT_CONTINUATION_RELATION"
REJECT_DECISION = "REJECT_CANDIDATE"
DECISIONS = (FIELD_DECISION, RELATION_DECISION, REJECT_DECISION)


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable(value).encode("utf-8")).hexdigest()


def _refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result = [dict(item) for item in value if isinstance(item, Mapping)]
    return sorted(result, key=_stable)


def _ref_sets(value: Any) -> tuple[list[dict[str, Any]], ...]:
    """Normalize one or more bounded physical-ref sets."""
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    if all(isinstance(item, Mapping) for item in value):
        refs = _refs(value)
        return (refs,) if refs else ()
    result: list[list[dict[str, Any]]] = []
    for item in value:
        refs = _refs(item)
        if refs and refs not in result:
            result.append(refs)
    return tuple(result)


def _row_refs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = row.get("ocr_metadata")
    refs = row.get("physical_row_refs")
    if isinstance(metadata, Mapping) and metadata.get("physical_row_refs"):
        refs = metadata.get("physical_row_refs")
    return _refs(refs)


def _same_refs(left: Any, right: Any) -> bool:
    return _refs(left) == _refs(right) and bool(_refs(left))


def _evidence_mappings(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Return only the row-level evidence containers used by review decisions."""
    metadata = row.get("ocr_metadata")
    return tuple(
        value
        for value in (row, metadata if isinstance(metadata, Mapping) else {})
        if isinstance(value, Mapping)
    )


def _continuation_fragments(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exact textual fragments already present in review evidence."""
    fragments: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in fragments:
            fragments.append(text)

    for source in _evidence_mappings(row):
        candidates = source.get("value_candidates")
        if isinstance(candidates, Mapping):
            for field in ("name", "type_mark", "manufacturer", "note"):
                candidate = candidates.get(field)
                if isinstance(candidate, Mapping):
                    add(candidate.get("value_candidate"))
        add(source.get("semantic_review_preview"))
        continuation = source.get("continuation_evidence")
        if isinstance(continuation, Mapping):
            for key in ("candidate_value", "fragment"):
                add(continuation.get(key))
            for key in ("candidate_fragments", "fragments", "text_candidates"):
                values = continuation.get(key)
                if isinstance(values, (list, tuple)):
                    for value in values:
                        add(value)
    return tuple(fragments)


def _continuation_parent_candidates(row: Mapping[str, Any]) -> tuple[list[dict[str, Any]], ...]:
    """Read explicitly bounded parent refs from existing continuation evidence.

    The request target is deliberately not an evidence source.  In particular,
    this helper does not infer a parent from row order or proximity.
    """
    found: list[list[dict[str, Any]]] = []
    direct_keys = (
        "candidate_parent_physical_refs",
        "candidate_parent_refs",
        "parent_physical_refs",
        "candidate_target_refs",
    )
    nested_keys = (
        "continuation_evidence",
        "continuation_candidates",
        "candidate_parents",
        "parent_candidates",
    )

    def add(value: Any) -> None:
        for refs in _ref_sets(value):
            if refs not in found:
                found.append(refs)

    def visit(value: Any, *, allow_direct: bool = True) -> None:
        if isinstance(value, Mapping):
            if allow_direct:
                for key in direct_keys:
                    if key in value:
                        add(value.get(key))
            for key in nested_keys:
                nested = value.get(key)
                if isinstance(nested, (Mapping, list, tuple)):
                    visit(nested)
            for key in ("candidates", "candidate", "evidence"):
                nested = value.get(key)
                if isinstance(nested, (Mapping, list, tuple)):
                    visit(nested)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    visit(item)

    for source in _evidence_mappings(row):
        for key in direct_keys:
            if key in source:
                add(source.get(key))
        for key in nested_keys:
            nested = source.get(key)
            if isinstance(nested, (Mapping, list, tuple)):
                visit(nested)
    return tuple(found)


class ReviewDecision(BaseModel):
    """One immutable, human-originated review decision."""

    model_config = ConfigDict(extra="forbid")

    document_fingerprint: str
    evidence_fingerprint: str
    page: int = Field(ge=1)
    semantic_ref: str | None = None
    physical_refs: list[dict[str, Any]]
    field: str | None = None
    relation: str | None = None
    candidate_value: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    decision: Literal[FIELD_DECISION, RELATION_DECISION, REJECT_DECISION]
    created_at: datetime
    provenance: Literal["human"] = "human"

    @property
    def decision_key(self) -> str:
        return _sha256({
            "document_fingerprint": self.document_fingerprint,
            "page": self.page,
            "physical_refs": _refs(self.physical_refs),
            "field": self.field,
            "relation": self.relation,
            "parent_physical_refs": _refs(self.target.get("parent_physical_refs")),
        })


class ReviewDecisionStore:
    """Small atomic JSON store scoped to one document workspace."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[ReviewDecision]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = payload.get("decisions", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return []
        decisions: list[ReviewDecision] = []
        for item in raw:
            try:
                decisions.append(ReviewDecision.model_validate(item))
            except Exception:
                continue
        return decisions

    def save(self, decisions: list[ReviewDecision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"decisions": [item.model_dump(mode="json") for item in decisions]}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def upsert(self, decision: ReviewDecision) -> list[ReviewDecision]:
        decisions = [item for item in self.load() if item.decision_key != decision.decision_key]
        decisions.append(decision)
        decisions.sort(key=lambda item: item.created_at)
        self.save(decisions)
        return decisions


class HumanReviewService:
    """Validate, persist and apply bounded human resolutions."""

    def document_fingerprint(self, pdf_path: Path) -> str:
        digest = hashlib.sha256()
        with Path(pdf_path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _find_row(result: Mapping[str, Any], page: int, refs: Any) -> dict[str, Any] | None:
        for row in result.get("rows") or []:
            if int(row.get("page") or 0) == int(page) and _same_refs(_row_refs(row), refs):
                return row
        return None

    @staticmethod
    def _candidate(row: Mapping[str, Any], field: str | None) -> Mapping[str, Any] | None:
        if not field:
            return None
        candidates = row.get("value_candidates")
        if not isinstance(candidates, Mapping):
            metadata = row.get("ocr_metadata")
            candidates = metadata.get("value_candidates") if isinstance(metadata, Mapping) else {}
        candidate = candidates.get(field) if isinstance(candidates, Mapping) else None
        return candidate if isinstance(candidate, Mapping) else None

    @staticmethod
    def evidence_fingerprint(
        row: Mapping[str, Any],
        *,
        field: str | None = None,
        candidate_value: str | None = None,
        parent_refs: Any = None,
    ) -> str:
        metadata = row.get("ocr_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        candidate = HumanReviewService._candidate(row, field)
        evidence = {
            "page": row.get("page"),
            "physical_refs": _row_refs(row),
            "field": field,
            "candidate_value": candidate_value,
            "candidate": dict(candidate or {}),
            "raw_physical_cells": metadata.get("raw_physical_cells"),
            "semantic_review_preview": row.get("semantic_review_preview")
            or metadata.get("semantic_review_preview"),
            "continuation_fragments": list(_continuation_fragments(row)),
            "continuation_evidence": metadata.get("continuation_evidence"),
            "candidate_parent_physical_refs": [
                refs for refs in _continuation_parent_candidates(row)
            ],
            "parent_physical_refs": _refs(parent_refs),
        }
        return _sha256(evidence)

    def create_decision(
        self,
        result: Mapping[str, Any],
        *,
        document_fingerprint: str,
        page: int,
        physical_refs: Any,
        decision: str,
        field: str | None = None,
        relation: str | None = None,
        candidate_value: str | None = None,
        target: Mapping[str, Any] | None = None,
    ) -> ReviewDecision:
        if decision not in DECISIONS:
            raise ValueError("Недопустимое решение проверки")
        row = self._find_row(result, page, physical_refs)
        if row is None:
            raise ValueError("Физическая строка OCR не найдена в текущем результате")
        target_data = dict(target or {})
        if decision in {FIELD_DECISION, REJECT_DECISION}:
            if not field or not candidate_value:
                raise ValueError("Для решения по кандидату нужны поле и значение")
            candidate = self._candidate(row, field)
            if not candidate or str(candidate.get("value_candidate") or "") != str(candidate_value):
                raise ValueError("Кандидат отсутствует или не совпадает с текущим OCR-доказательством")
        if decision == RELATION_DECISION:
            if not relation:
                raise ValueError("Для связи продолжения укажите relation")
            parent_refs = target_data.get("parent_physical_refs")
            if not _refs(parent_refs):
                raise ValueError("Для связи продолжения нужна физическая ссылка родителя")
            parent_candidates = _continuation_parent_candidates(row)
            if not parent_candidates:
                raise ValueError(
                    "Для продолжения отсутствует bounded candidate-parent evidence"
                )
            if not any(_same_refs(parent_refs, candidate) for candidate in parent_candidates):
                raise ValueError(
                    "Родитель продолжения не совпадает с текущим OCR-доказательством"
                )
            fragments = _continuation_fragments(row)
            if not candidate_value:
                candidate_value = fragments[0] if fragments else ""
            if not candidate_value:
                raise ValueError("У продолжения отсутствует текстовый кандидат")
            if str(candidate_value).strip() not in fragments:
                raise ValueError(
                    "Текст продолжения не совпадает с текущим OCR-доказательством"
                )
        metadata = row.get("ocr_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        return ReviewDecision(
            document_fingerprint=document_fingerprint,
            evidence_fingerprint=self.evidence_fingerprint(
                row,
                field=field,
                candidate_value=candidate_value,
                parent_refs=target_data.get("parent_physical_refs"),
            ),
            page=int(page),
            semantic_ref=str(
                row.get("logical_id")
                or metadata.get("logical_id")
                or ""
            ) or None,
            physical_refs=_refs(physical_refs),
            field=field,
            relation=relation,
            candidate_value=str(candidate_value) if candidate_value is not None else None,
            target=target_data,
            decision=decision,
            created_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _continuation_text(row: Mapping[str, Any]) -> str:
        fragments = _continuation_fragments(row)
        return fragments[0] if fragments else ""

    @staticmethod
    def _mark_human(row: dict[str, Any], payload: Mapping[str, Any]) -> None:
        current = row.get("human_review")
        current = dict(current) if isinstance(current, Mapping) else {}
        current.update(dict(payload))
        current["provenance"] = "human"
        row["human_review"] = current
        row["verification_state"] = "HUMAN_VERIFIED"

    def _apply_one(self, result: dict[str, Any], decision: ReviewDecision) -> bool:
        row = self._find_row(result, decision.page, decision.physical_refs)
        if row is None:
            return False
        parent_refs = decision.target.get("parent_physical_refs")
        expected = self.evidence_fingerprint(
            row,
            field=decision.field,
            candidate_value=decision.candidate_value,
            parent_refs=parent_refs,
        )
        if expected != decision.evidence_fingerprint:
            return False
        if decision.decision == RELATION_DECISION:
            parent_candidates = _continuation_parent_candidates(row)
            if not parent_candidates or not any(
                _same_refs(parent_refs, candidate) for candidate in parent_candidates
            ):
                return False
            if str(decision.candidate_value or "").strip() not in _continuation_fragments(row):
                return False
        if decision.decision == REJECT_DECISION:
            rejected = list(row.get("human_rejected_candidates") or [])
            rejected.append({
                "field": decision.field,
                "candidate_value": decision.candidate_value,
                "decision_key": decision.decision_key,
                "evidence_fingerprint": decision.evidence_fingerprint,
                "provenance": "human",
            })
            row["human_rejected_candidates"] = rejected
            return True
        if decision.decision == FIELD_DECISION:
            candidate = self._candidate(row, decision.field)
            value = str(candidate.get("value_candidate") or "") if candidate else ""
            if not candidate or value != str(decision.candidate_value or ""):
                return False
            field = str(decision.field)
            current = str(row.get(field) or "").strip()
            if current and current != value:
                return False
            row[field] = value
            edited = list(row.get("edited_fields") or [])
            if field not in edited:
                edited.append(field)
            row["edited_fields"] = edited
            confirmed = list(row.get("human_verified_fields") or [])
            if field not in confirmed:
                confirmed.append(field)
            row["human_verified_fields"] = confirmed
            row["status"] = "verified"
            self._mark_human(row, {
                "decision": decision.decision,
                "decision_key": decision.decision_key,
                "field": field,
                "candidate_value": value,
                "evidence_fingerprint": decision.evidence_fingerprint,
            })
            refresh_review_state(row)
            if not critical_blockers_for_row(row) and row.get("row_type") not in {"section", "system", "skip"}:
                row["status"] = "verified"
            return True
        if decision.decision == RELATION_DECISION:
            if str(row.get("row_type") or "") != "semantic_review":
                return False
            parent = self._find_row(result, decision.page, parent_refs)
            if parent is None or parent is row:
                return False
            if str(parent.get("row_type") or "") not in {"item", "component", "item_candidate"}:
                return False
            fragment = str(decision.candidate_value or self._continuation_text(row)).strip()
            if not fragment:
                return False
            parent_name = str(parent.get("name") or "").strip()
            parent["name"] = f"{parent_name} {fragment}".strip()
            relations = list(parent.get("human_verified_relations") or [])
            relation_record = {
                "relation": decision.relation,
                "child_physical_refs": _row_refs(row),
                "parent_physical_refs": _refs(parent_refs),
                "fragment": fragment,
                "decision_key": decision.decision_key,
            }
            relations.append(relation_record)
            parent["human_verified_relations"] = relations
            self._mark_human(parent, {
                "decision": decision.decision,
                "decision_key": decision.decision_key,
                "relation": decision.relation,
                "evidence_fingerprint": decision.evidence_fingerprint,
            })
            row["row_type"] = "skip"
            row["status"] = "verified"
            row["semantic_state"] = "HUMAN_VERIFIED"
            row["semantic_review"] = False
            row["critical_fields"] = []
            row["critical_blockers"] = []
            row["review_reasons"] = []
            self._mark_human(row, {
                "decision": decision.decision,
                "decision_key": decision.decision_key,
                "relation": decision.relation,
                "evidence_fingerprint": decision.evidence_fingerprint,
            })
            return True
        return False

    def apply_decision(self, result: Mapping[str, Any], decision: ReviewDecision) -> dict[str, Any]:
        updated = deepcopy(dict(result))
        if decision.document_fingerprint != str(updated.get("document_fingerprint") or decision.document_fingerprint):
            return updated
        self._apply_one(updated, decision)
        return recalculate_page_safety(updated)

    def apply_saved_decisions(
        self,
        result: Mapping[str, Any],
        decisions: list[ReviewDecision],
        document_fingerprint: str,
    ) -> dict[str, Any]:
        updated = deepcopy(dict(result))
        updated["document_fingerprint"] = document_fingerprint
        for decision in sorted(decisions, key=lambda item: item.created_at):
            if decision.document_fingerprint != document_fingerprint:
                continue
            self._apply_one(updated, decision)
        return recalculate_page_safety(updated)


def recalculate_page_safety(result: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild page safety counters from the post-review canonical view."""
    updated = deepcopy(dict(result))
    rows = list(updated.get("rows") or [])
    statuses = dict(updated.get("page_statuses") or {})
    for page_key, original in statuses.items():
        if not isinstance(original, Mapping):
            continue
        page = int(original.get("page") or page_key)
        page_rows = [row for row in rows if int(row.get("page") or 0) == page]
        diagnostics = dict(original.get("diagnostics") or {})
        # Page status diagnostics are intentionally compact and do not carry
        # the original reconstruction object.  Rehydrate the contract inputs
        # from the serialized status before recalculating human-resolved
        # semantic blockers.
        diagnostics["geometry_grid"] = {
            "high_confidence": str(original.get("layout_status") or "") == "TRUSTED"
        }
        diagnostics["schema"] = {
            "status": str(original.get("schema_status") or "unknown").lower()
        }
        diagnostics["selected_mode"] = diagnostics.get("selected_mode") or "geometry_first"
        diagnostics["page_disposition"] = (
            diagnostics.get("page_disposition")
            if isinstance(diagnostics.get("page_disposition"), Mapping)
            else {"disposition": original.get("page_disposition")}
        )
        output_rows = [
            row for row in page_rows
            if row.get("row_type") not in {"section", "system", "skip"}
        ]
        review_rows = [
            row for row in output_rows
            if row.get("row_type") == "semantic_review"
            or row.get("semantic_review")
            or row.get("semantic_state") == "REVIEW"
        ]
        critical_missing = sum(
            critical_field_count(row)
            for row in output_rows
            if row.get("selected", True) is not False
        )
        unresolved = sum(
            1 for row in review_rows
            if critical_blockers_for_row(row)
            or str(row.get("row_type") or "") == "semantic_review"
        )
        relation_conflicts = sum(
            1 for row in review_rows
            if any("relation" in str(reason) for reason in (row.get("review_reasons") or []))
        )
        numeric_suspect = sum(
            1 for row in output_rows
            if "numeric_suspect" in critical_blockers_for_row(row)
        )
        secondary_conflict = sum(
            1 for row in output_rows
            if "secondary_conflict" in critical_blockers_for_row(row)
        )
        diagnostics.update({
            "semantic_critical_value_missing_count": critical_missing,
            "semantic_output_critical_unresolved_count": unresolved,
            "semantic_review_item_count": len(review_rows),
            "semantic_review_evidence_row_count": sum(
                1 for row in page_rows if row.get("row_type") == "semantic_review"
            ),
            "unresolved_physical_row_count": len(review_rows),
            "semantic_relation_conflict_count": relation_conflicts,
            "semantic_numeric_suspect_count": numeric_suspect,
            "semantic_secondary_conflict_count": secondary_conflict,
            "logical_item_count": len([
                row for row in page_rows
                if row.get("row_type") in {"item", "component", "item_candidate"}
            ]),
            "human_review_applied": any(row.get("human_review") for row in page_rows),
        })
        page_status = page_status_from_diagnostics(page, diagnostics, row_count=len(output_rows))
        dynamic_blockers = {
            "critical_value_missing",
            "physical_row_semantics_unresolved",
            "numeric_suspect",
            "secondary_conflict",
        }
        preserved_blockers = [
            str(item) for item in (original.get("blockers") or [])
            if str(item) not in dynamic_blockers
        ]
        disposition = str(original.get("page_disposition") or "")
        if disposition not in {SPEC_OUTPUT, CONFIRMED_NON_SPEC}:
            preserved_blockers.append("page_disposition_unresolved")
        page_status.blockers = list(dict.fromkeys(preserved_blockers + page_status.blockers))
        if page_status.blockers:
            page_status.output_status = "REVIEW_REQUIRED" if output_rows else "NO_SPEC_OUTPUT"
        page_status.diagnostics.update({
            "human_review_applied": diagnostics["human_review_applied"],
            "logical_item_count": diagnostics["logical_item_count"],
        })
        statuses[str(page)] = page_status.as_dict()
    updated["page_statuses"] = statuses
    updated["summary"] = SpecificationRowAssembler.summary(rows, updated.get("errors") or [])
    return updated


__all__ = [
    "DECISIONS",
    "FIELD_DECISION",
    "HumanReviewService",
    "RELATION_DECISION",
    "REJECT_DECISION",
    "ReviewDecision",
    "ReviewDecisionStore",
    "recalculate_page_safety",
]
