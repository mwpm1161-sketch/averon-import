"""Audited human resolution for already-produced OCR evidence.

This layer is intentionally downstream of OCR and reconstruction.  It may
resolve an existing candidate or a bounded continuation relation, but it
cannot create a value without matching evidence and it never changes the raw
OCR evidence kept in ``ocr_metadata``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.page_disposition import CONFIRMED_NON_SPEC, SPEC_OUTPUT
from averon_import.services.review_policy import (
    CRITICAL_FIELDS,
    human_confirmed_absence_applies,
    human_verified_value_applies,
    critical_blockers_for_row,
    critical_field_count,
    missing_critical_fields,
    refresh_review_state,
    row_is_ready,
    row_requires_review,
)
from averon_import.services.row_assembler import SpecificationRowAssembler


FIELD_DECISION = "ACCEPT_FIELD_CANDIDATE"
RELATION_DECISION = "ACCEPT_CONTINUATION_RELATION"
REJECT_DECISION = "REJECT_CANDIDATE"
CONFIRM_FIELD_VALUE_DECISION = "CONFIRM_FIELD_VALUE"
CONFIRM_FIELD_ABSENT_DECISION = "CONFIRM_FIELD_ABSENT"
REVIEW_PROJECTION_VERSION = 1
DECISIONS = (
    FIELD_DECISION,
    RELATION_DECISION,
    REJECT_DECISION,
    CONFIRM_FIELD_VALUE_DECISION,
    CONFIRM_FIELD_ABSENT_DECISION,
)
REVIEW_LEDGER_CORRUPT_MESSAGE = (
    "История ручной проверки повреждена. Требуется восстановление."
)


class ReviewDecisionLedgerCorrupt(ValueError):
    """The persisted decision ledger is present but cannot be trusted."""

    def __init__(self) -> None:
        super().__init__(REVIEW_LEDGER_CORRUPT_MESSAGE)


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _detached_review_copy(value: Any) -> Any:
    """Detach a public review DTO without depending on IR pickleability.

    Authoritative OCR/semantic IR values may contain recursively frozen
    mappings and tuples.  Review mutates only a JSON-shaped DTO, so mappings
    become plain dictionaries and sequence containers become detached lists.
    Unknown scalar/object values are preserved rather than stringified.
    """
    if isinstance(value, Mapping):
        return {
            key: _detached_review_copy(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_detached_review_copy(item) for item in value]
    return value


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
    confirmed_value: str | None = None
    decision_id: str | None = None
    decision: Literal[
        FIELD_DECISION,
        RELATION_DECISION,
        REJECT_DECISION,
        CONFIRM_FIELD_VALUE_DECISION,
        CONFIRM_FIELD_ABSENT_DECISION,
    ]
    created_at: datetime
    provenance: Literal["human"] = "human"

    @property
    def decision_key(self) -> str:
        identity = {
            "document_fingerprint": self.document_fingerprint,
            "page": self.page,
            "physical_refs": _refs(self.physical_refs),
            "field": self.field,
            "relation": self.relation,
            "parent_physical_refs": _refs(self.target.get("parent_physical_refs")),
        }
        if self.decision in {CONFIRM_FIELD_VALUE_DECISION, CONFIRM_FIELD_ABSENT_DECISION}:
            identity.update({
                "decision": self.decision,
                "confirmed_value": self.confirmed_value,
                "evidence_fingerprint": self.evidence_fingerprint,
                "decision_id": self.decision_id,
            })
        return _sha256(identity)


class ReviewDecisionStore:
    """Small atomic JSON store scoped to one document workspace."""

    metrics = {
        "ledger_reads": 0,
        "ledger_writes": 0,
        "revision_reads": 0,
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        self.revision_path = self.path.with_suffix(self.path.suffix + ".revision")

    def _write_revision(self, revision: int) -> None:
        self.revision_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.revision_path.with_suffix(self.revision_path.suffix + ".tmp")
        temporary.write_text(str(max(0, int(revision))), encoding="ascii")
        temporary.replace(self.revision_path)

    def _sync_revision(self, revision: int) -> None:
        try:
            current = int(self.revision_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            current = -1
        if current != revision:
            self._write_revision(revision)

    def load_revision(self) -> int:
        """Read the small revision marker without parsing a steady-state ledger."""
        self.metrics["revision_reads"] += 1
        if self.revision_path.is_file():
            try:
                return max(0, int(self.revision_path.read_text(encoding="ascii").strip()))
            except (OSError, ValueError):
                pass
        if not self.path.is_file():
            return 0
        _, revision = self.load_snapshot()
        return revision

    def load_snapshot(self) -> tuple[list[ReviewDecision], int]:
        """Return the canonical ledger and its persisted revision.

        Legacy list payloads and dictionaries without a revision are read as
        revision zero. The next write upgrades them to the versioned shape.
        """
        try:
            self.path.stat()
        except FileNotFoundError:
            if self.revision_path.is_file():
                self._sync_revision(0)
            return [], 0
        except OSError as exc:
            raise ReviewDecisionLedgerCorrupt() from exc
        self.metrics["ledger_reads"] += 1
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewDecisionLedgerCorrupt() from exc
        if isinstance(payload, dict):
            if "decisions" not in payload or not isinstance(payload["decisions"], list):
                raise ReviewDecisionLedgerCorrupt()
            raw = payload["decisions"]
            revision_value = payload.get("revision", 0)
            if type(revision_value) is not int or revision_value < 0:
                raise ReviewDecisionLedgerCorrupt()
            revision = revision_value
        else:
            if not isinstance(payload, list):
                raise ReviewDecisionLedgerCorrupt()
            raw = payload
            revision = 0
        decisions: list[ReviewDecision] = []
        for item in raw:
            try:
                decisions.append(ReviewDecision.model_validate(item))
            except Exception as exc:
                raise ReviewDecisionLedgerCorrupt() from exc
        self._sync_revision(revision)
        return decisions, revision

    def load(self) -> list[ReviewDecision]:
        return self.load_snapshot()[0]

    def save(self, decisions: list[ReviewDecision], *, revision: int | None = None) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if revision is None:
            revision = self.load_snapshot()[1] + 1
        revision = max(0, int(revision))
        payload = {
            "revision": revision,
            "decisions": [item.model_dump(mode="json") for item in decisions],
        }
        # Write the marker first. If the process stops before replacing the
        # ledger, a reader detects the revision mismatch and reloads the
        # authoritative ledger before deciding whether recovery is needed.
        self._write_revision(revision)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.metrics["ledger_writes"] += 1
        return revision

    @staticmethod
    def _same_decision(left: ReviewDecision, right: ReviewDecision) -> bool:
        left_payload = left.model_dump(mode="json", exclude={"created_at"})
        right_payload = right.model_dump(mode="json", exclude={"created_at"})
        return _stable(left_payload) == _stable(right_payload)

    def upsert_snapshot(
        self, decision: ReviewDecision
    ) -> tuple[list[ReviewDecision], int, bool]:
        decisions, revision = self.load_snapshot()
        existing = next(
            (item for item in decisions if item.decision_key == decision.decision_key),
            None,
        )
        if existing is not None and self._same_decision(existing, decision):
            return decisions, revision, False
        decisions = [item for item in decisions if item.decision_key != decision.decision_key]
        decisions.append(decision)
        decisions.sort(key=lambda item: item.created_at)
        revision = self.save(decisions, revision=revision + 1)
        return decisions, revision, True

    def upsert(self, decision: ReviewDecision) -> list[ReviewDecision]:
        return self.upsert_snapshot(decision)[0]


class HumanReviewService:
    """Validate, persist and apply bounded human resolutions."""

    def __init__(self) -> None:
        # Bounded process-local counters for tests and local performance audits.
        # They are never logged or included in application responses.
        self.metrics = {
            "review_decisions_replayed": 0,
            "page_safety_pages_recalculated": 0,
            "full_result_copies": 0,
            "review_rows_detached": 0,
        }

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
    def _find_row_index(result: Mapping[str, Any], page: int, refs: Any) -> int | None:
        for index, row in enumerate(result.get("rows") or []):
            if int(row.get("page") or 0) == int(page) and _same_refs(_row_refs(row), refs):
                return index
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
        decision: str | None = None,
        confirmed_value: str | None = None,
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
            "continuation_evidence": _detached_review_copy(
                metadata.get("continuation_evidence")
            ),
            "candidate_parent_physical_refs": [
                refs for refs in _continuation_parent_candidates(row)
            ],
            "parent_physical_refs": _refs(parent_refs),
        }
        if decision in {CONFIRM_FIELD_VALUE_DECISION, CONFIRM_FIELD_ABSENT_DECISION}:
            normalization = metadata.get("normalization")
            structural_safety = metadata.get("target_cell_structural_safety")
            evidence.update({
                "row_identity": row.get("id")
                or row.get("logical_id")
                or metadata.get("logical_id"),
                "decision": decision,
                "current_canonical_value": str(row.get(field or "", "") or ""),
                "confirmed_value": confirmed_value,
                "field_normalization": (
                    normalization.get(field)
                    if isinstance(normalization, Mapping) and field
                    else None
                ),
                "field_structural_safety": (
                    structural_safety.get(field)
                    if isinstance(structural_safety, Mapping) and field
                    else None
                ),
            })
        return _sha256(evidence)

    @staticmethod
    def _confirmation_decision_id(
        row: Mapping[str, Any],
        *,
        decision: str,
        field: str,
        evidence_fingerprint: str,
        confirmed_value: str | None,
    ) -> str:
        map_name = (
            "human_verified_field_values"
            if decision == CONFIRM_FIELD_VALUE_DECISION
            else "human_confirmed_absent_fields"
        )
        records = row.get(map_name)
        record = records.get(field) if isinstance(records, Mapping) else None
        if (
            isinstance(record, Mapping)
            and not record.get("invalidated")
            and record.get("decision") == decision
            and record.get("evidence_fingerprint") == evidence_fingerprint
            and str(record.get("value", "")) == str(confirmed_value or "")
            and record.get("decision_id")
        ):
            return str(record["decision_id"])
        return _sha256({
            "decision": decision,
            "field": field,
            "document_fingerprint": row.get("document_fingerprint"),
            "page": row.get("page"),
            "physical_refs": _row_refs(row),
            "evidence_fingerprint": evidence_fingerprint,
            "confirmed_value": confirmed_value,
            "supersedes": (
                record.get("decision_id") or record.get("decision_key")
                if isinstance(record, Mapping)
                else None
            ),
        })

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
        confirmed_value: str | None = None,
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
        if decision in {CONFIRM_FIELD_VALUE_DECISION, CONFIRM_FIELD_ABSENT_DECISION}:
            if field not in CRITICAL_FIELDS:
                raise ValueError("Для подтверждения нужно применимое критичное поле")
            current_value = str(row.get(field, "") or "")
            if decision == CONFIRM_FIELD_VALUE_DECISION:
                if not current_value.strip():
                    raise ValueError("Нельзя подтвердить пустое значение поля")
                if confirmed_value != current_value:
                    raise ValueError("Подтверждаемое значение не совпадает с текущим значением поля")
            else:
                if current_value.strip():
                    raise ValueError("Поле уже содержит значение; подтверждение отсутствия устарело")
                if field not in missing_critical_fields(row) and not human_confirmed_absence_applies(row, field):
                    raise ValueError("Поле не является применимым отсутствующим критичным значением")
                confirmed_value = None
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
        evidence_fingerprint = self.evidence_fingerprint(
            row,
            field=field,
            candidate_value=candidate_value,
            parent_refs=target_data.get("parent_physical_refs"),
            decision=decision,
            confirmed_value=confirmed_value,
        )
        return ReviewDecision(
            document_fingerprint=document_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
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
            confirmed_value=confirmed_value,
            decision_id=(
                self._confirmation_decision_id(
                    row,
                    decision=decision,
                    field=str(field),
                    evidence_fingerprint=evidence_fingerprint,
                    confirmed_value=confirmed_value,
                )
                if decision in {CONFIRM_FIELD_VALUE_DECISION, CONFIRM_FIELD_ABSENT_DECISION}
                else None
            ),
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

    def _apply_one(
        self,
        result: dict[str, Any],
        decision: ReviewDecision,
        *,
        row_override: dict[str, Any] | None = None,
        parent_override: dict[str, Any] | None = None,
        allow_invalidated_reaffirmation: bool = False,
    ) -> bool:
        row = row_override or self._find_row(result, decision.page, decision.physical_refs)
        if row is None:
            return False
        parent_refs = decision.target.get("parent_physical_refs")
        expected = self.evidence_fingerprint(
            row,
            field=decision.field,
            candidate_value=decision.candidate_value,
            parent_refs=parent_refs,
            decision=decision.decision,
            confirmed_value=decision.confirmed_value,
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
            if any(
                isinstance(item, Mapping)
                and item.get("decision_key") == decision.decision_key
                for item in rejected
            ):
                return True
            rejected.append({
                "field": decision.field,
                "candidate_value": decision.candidate_value,
                "decision_key": decision.decision_key,
                "evidence_fingerprint": decision.evidence_fingerprint,
                "provenance": "human",
            })
            row["human_rejected_candidates"] = rejected
            return True
        if decision.decision in {CONFIRM_FIELD_VALUE_DECISION, CONFIRM_FIELD_ABSENT_DECISION}:
            field = str(decision.field or "")
            current_value = str(row.get(field, "") or "")
            if decision.decision == CONFIRM_FIELD_VALUE_DECISION:
                if not current_value.strip() or current_value != str(decision.confirmed_value or ""):
                    return False
                records = row.get("human_verified_field_values")
                records = dict(records) if isinstance(records, Mapping) else {}
                existing = records.get(field)
                if isinstance(existing, Mapping) and existing.get("decision_key") == decision.decision_key:
                    return True
                records[field] = {
                    "value": current_value,
                    "decision": decision.decision,
                    "decision_key": decision.decision_key,
                    "decision_id": decision.decision_id,
                    "evidence_fingerprint": decision.evidence_fingerprint,
                    "provenance": "human",
                }
                row["human_verified_field_values"] = records
                confirmed = list(row.get("human_verified_fields") or [])
                if field not in confirmed:
                    confirmed.append(field)
                row["human_verified_fields"] = confirmed
                row["status"] = "review"
                self._mark_human(row, {
                    "decision": decision.decision,
                    "decision_key": decision.decision_key,
                    "field": field,
                    "confirmed_value": current_value,
                    "evidence_fingerprint": decision.evidence_fingerprint,
                })
            else:
                if current_value.strip():
                    return False
                if field not in missing_critical_fields(row) and not human_confirmed_absence_applies(row, field):
                    return False
                records = row.get("human_confirmed_absent_fields")
                records = dict(records) if isinstance(records, Mapping) else {}
                existing = records.get(field)
                if isinstance(existing, Mapping) and existing.get("decision_key") == decision.decision_key:
                    return True
                records[field] = {
                    "decision": decision.decision,
                    "decision_key": decision.decision_key,
                    "decision_id": decision.decision_id,
                    "evidence_fingerprint": decision.evidence_fingerprint,
                    "provenance": "human",
                }
                row["human_confirmed_absent_fields"] = records
                row["status"] = "review"
                self._mark_human(row, {
                    "decision": decision.decision,
                    "decision_key": decision.decision_key,
                    "field": field,
                    "evidence_fingerprint": decision.evidence_fingerprint,
                })
            refresh_review_state(row)
            if not critical_blockers_for_row(row) and row.get("row_type") not in {"section", "system", "skip"}:
                row["status"] = "verified"
            return True
        if decision.decision == FIELD_DECISION:
            candidate = self._candidate(row, decision.field)
            value = str(candidate.get("value_candidate") or "") if candidate else ""
            if not candidate or value != str(decision.candidate_value or ""):
                return False
            field = str(decision.field)
            current = str(row.get(field) or "").strip()
            if current and current != value:
                records = row.get("human_verified_field_values")
                records = dict(records) if isinstance(records, Mapping) else {}
                existing = records.get(field)
                if (
                    not isinstance(existing, Mapping)
                    or existing.get("decision_key") == decision.decision_key
                ):
                    stale_record = dict(existing) if isinstance(existing, Mapping) else {}
                    stale_record.update({
                        "value": value,
                        "decision": decision.decision,
                        "decision_key": decision.decision_key,
                        "evidence_fingerprint": decision.evidence_fingerprint,
                        "provenance": "human",
                        "invalidated": True,
                    })
                    records[field] = stale_record
                    row["human_verified_field_values"] = records
                if not human_verified_value_applies(row, field):
                    row["status"] = "review"
                    refresh_review_state(row)
                return True
            records = row.get("human_verified_field_values")
            records = dict(records) if isinstance(records, Mapping) else {}
            existing = records.get(field)
            if (
                isinstance(existing, Mapping)
                and existing.get("decision_key") == decision.decision_key
                and existing.get("invalidated")
                and not allow_invalidated_reaffirmation
            ):
                row["status"] = "review"
                refresh_review_state(row)
                return True
            if (
                isinstance(existing, Mapping)
                and existing.get("decision_key") == decision.decision_key
                and not existing.get("invalidated")
                and str(existing.get("value", "")) == value
            ):
                return True
            row[field] = value
            edited = list(row.get("edited_fields") or [])
            if field not in edited:
                edited.append(field)
            row["edited_fields"] = edited
            records[field] = {
                "value": value,
                "decision": decision.decision,
                "decision_key": decision.decision_key,
                "evidence_fingerprint": decision.evidence_fingerprint,
                "provenance": "human",
            }
            row["human_verified_field_values"] = records
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
            parent = parent_override or self._find_row(result, decision.page, parent_refs)
            if parent is None or parent is row:
                return False
            if str(parent.get("row_type") or "") not in {"item", "component", "item_candidate"}:
                return False
            fragment = str(decision.candidate_value or self._continuation_text(row)).strip()
            if not fragment:
                return False
            parent_name = str(parent.get("name") or "").strip()
            relations = list(parent.get("human_verified_relations") or [])
            relation_record = {
                "relation": decision.relation,
                "child_physical_refs": _row_refs(row),
                "parent_physical_refs": _refs(parent_refs),
                "fragment": fragment,
                "decision_key": decision.decision_key,
            }
            already_related = any(
                isinstance(item, Mapping)
                and item.get("decision_key") == decision.decision_key
                for item in relations
            )
            if not already_related:
                parent["name"] = f"{parent_name} {fragment}".strip()
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
        self.metrics["full_result_copies"] += 1
        updated = _detached_review_copy(result)
        if decision.document_fingerprint != str(updated.get("document_fingerprint") or decision.document_fingerprint):
            return updated
        self._apply_one(
            updated, decision, allow_invalidated_reaffirmation=True
        )
        return recalculate_page_safety(updated, metrics=self.metrics)

    def apply_decision_incremental(
        self, result: dict[str, Any], decision: ReviewDecision
    ) -> tuple[dict[str, Any], list[dict[str, Any]], set[int], bool]:
        """Apply one decision to a freshly loaded canonical JSON projection.

        Only the decision's row (and a bounded continuation parent) are
        detached. OCR metadata is shared unchanged with the original row so
        review policy can update the projection without rewriting OCR evidence.
        The caller must hold the per-document mutation lock.
        """
        if decision.document_fingerprint != str(
            result.get("document_fingerprint") or decision.document_fingerprint
        ):
            return result, [], set(), False
        row_index = self._find_row_index(result, decision.page, decision.physical_refs)
        if row_index is None:
            return result, [], set(), False
        indexes = {row_index}
        parent_index = None
        if decision.decision == RELATION_DECISION:
            parent_index = self._find_row_index(
                result,
                decision.page,
                decision.target.get("parent_physical_refs"),
            )
            if parent_index is None or parent_index == row_index:
                return result, [], set(), False
            indexes.add(parent_index)

        original_rows = result.get("rows") or []
        rows = list(original_rows)
        original_by_index = {index: original_rows[index] for index in indexes}
        for index in indexes:
            row_copy = _detached_review_copy(original_rows[index])
            rows[index] = row_copy
        self.metrics["review_rows_detached"] += len(indexes)
        updated = dict(result)
        updated["rows"] = rows
        if decision.decision == RELATION_DECISION:
            applied = self._apply_one(
                updated,
                decision,
                row_override=rows[row_index],
                parent_override=rows[parent_index],
                allow_invalidated_reaffirmation=True,
            )
        else:
            applied = self._apply_one(
                updated,
                decision,
                row_override=rows[row_index],
                allow_invalidated_reaffirmation=True,
            )
        if not applied:
            return result, [], set(), False

        # Projection refresh may update derived flags under ocr_metadata. Keep
        # the exact source evidence object from the canonical row unchanged.
        for index in indexes:
            rows[index]["ocr_metadata"] = original_by_index[index].get("ocr_metadata")

        changed_indexes = [
            index for index in indexes if rows[index] != original_by_index[index]
        ]
        if not changed_indexes:
            return result, [], set(), True
        changed_rows = [rows[index] for index in changed_indexes]
        affected_pages = {
            int(row.get("page") or decision.page) for row in changed_rows
        }
        updated["document_fingerprint"] = decision.document_fingerprint
        recalculate_page_safety(
            updated,
            metrics=self.metrics,
            pages=affected_pages,
            copy_result=False,
            refresh_summary=False,
        )
        updated["summary"] = self._summary_after_row_changes(
            result.get("summary"),
            [original_by_index[index] for index in changed_indexes],
            changed_rows,
            rows,
            result.get("errors") or [],
        )
        return updated, changed_rows, affected_pages, True

    @staticmethod
    def _summary_after_row_changes(
        summary: Any,
        old_rows: list[dict[str, Any]],
        new_rows: list[dict[str, Any]],
        all_rows: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(summary, Mapping) or not {
            "total_rows", "status_counts", "type_counts", "page_errors",
            "unresolved_critical", "critical_rows", "review_rows", "ready_rows",
        }.issubset(summary):
            return SpecificationRowAssembler.summary(all_rows, errors)
        updated = dict(summary)
        status_counts = dict(summary.get("status_counts") or {})
        type_counts = dict(summary.get("type_counts") or {})
        unresolved = int(summary.get("unresolved_critical") or 0)
        critical_rows = int(summary.get("critical_rows") or 0)
        review_rows = int(summary.get("review_rows") or 0)
        ready_rows = int(summary.get("ready_rows") or 0)
        for row, delta in [*((row, -1) for row in old_rows), *((row, 1) for row in new_rows)]:
            status = str(row.get("status") or "")
            row_type = str(row.get("row_type") or "")
            status_counts[status] = status_counts.get(status, 0) + delta
            type_counts[row_type] = type_counts.get(row_type, 0) + delta
            unresolved += delta * critical_field_count(row)
            critical_rows += delta * int(bool(critical_blockers_for_row(row)))
            review_rows += delta * int(row_requires_review(row))
            ready_rows += delta * int(row_is_ready(row))
        updated["status_counts"] = {
            key: count for key, count in status_counts.items() if count > 0
        }
        updated["type_counts"] = {
            key: count for key, count in type_counts.items() if count > 0
        }
        updated["unresolved_critical"] = max(0, unresolved)
        updated["critical_rows"] = max(0, critical_rows)
        updated["review_rows"] = max(0, review_rows)
        updated["ready_rows"] = max(0, ready_rows)
        return updated

    def apply_saved_decisions(
        self,
        result: Mapping[str, Any],
        decisions: list[ReviewDecision],
        document_fingerprint: str,
    ) -> dict[str, Any]:
        self.metrics["full_result_copies"] += 1
        updated = _detached_review_copy(result)
        updated["document_fingerprint"] = document_fingerprint
        legacy_projection = (
            result.get("review_projection_version") != REVIEW_PROJECTION_VERSION
        )
        for decision in sorted(decisions, key=lambda item: item.created_at):
            if decision.document_fingerprint != document_fingerprint:
                continue
            self.metrics["review_decisions_replayed"] += 1
            self._apply_one(updated, decision)
        if legacy_projection:
            for row in updated.get("rows") or []:
                legacy_fields = row.get("human_verified_fields")
                legacy_fields = (
                    [str(field) for field in legacy_fields if str(field).strip()]
                    if isinstance(legacy_fields, (list, tuple, set))
                    else []
                )
                human_review = row.get("human_review")
                if (
                    isinstance(human_review, Mapping)
                    and human_review.get("decision") == FIELD_DECISION
                    and human_review.get("field")
                ):
                    legacy_fields.append(str(human_review["field"]))
                unresolved = {
                    field for field in legacy_fields
                    if not human_verified_value_applies(row, field)
                }
                if not unresolved:
                    continue
                remaining = [
                    field for field in legacy_fields
                    if field not in unresolved
                ]
                if remaining:
                    row["human_verified_fields"] = list(dict.fromkeys(remaining))
                else:
                    row.pop("human_verified_fields", None)
                if row.get("status") == "verified":
                    row["status"] = "review"
                refresh_review_state(row)
        updated["review_projection_version"] = REVIEW_PROJECTION_VERSION
        return recalculate_page_safety(updated, metrics=self.metrics)


def recalculate_page_safety(
    result: Mapping[str, Any],
    *,
    metrics: dict[str, int] | None = None,
    pages: set[int] | None = None,
    copy_result: bool = True,
    refresh_summary: bool = True,
) -> dict[str, Any]:
    """Rebuild page safety counters from the post-review canonical view."""
    if copy_result:
        if metrics is not None:
            metrics["full_result_copies"] += 1
        updated = _detached_review_copy(result)
    else:
        updated = result
        if not isinstance(updated, dict):
            raise TypeError("Canonical result must be a mutable dictionary")
    rows = list(updated.get("rows") or [])
    statuses = dict(updated.get("page_statuses") or {})
    affected_pages = {int(page) for page in pages} if pages is not None else None
    rows_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            row_page = int(row.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if affected_pages is None or row_page in affected_pages:
            rows_by_page.setdefault(row_page, []).append(row)
    for page_key, original in statuses.items():
        if not isinstance(original, Mapping):
            continue
        page = int(original.get("page") or page_key)
        if affected_pages is not None and page not in affected_pages:
            continue
        if metrics is not None:
            metrics["page_safety_pages_recalculated"] += 1
        if not copy_result:
            original = _detached_review_copy(original)
        page_rows = rows_by_page.get(page, [])
        diagnostics = _detached_review_copy(original.get("diagnostics") or {})
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
        numeric_non_scalar = sum(
            1 for row in output_rows
            if "numeric_non_scalar" in critical_blockers_for_row(row)
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
            "semantic_numeric_non_scalar_count": numeric_non_scalar,
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
            "numeric_non_scalar",
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
    if refresh_summary:
        updated["summary"] = SpecificationRowAssembler.summary(rows, updated.get("errors") or [])
    return updated


__all__ = [
    "CONFIRM_FIELD_ABSENT_DECISION",
    "CONFIRM_FIELD_VALUE_DECISION",
    "DECISIONS",
    "FIELD_DECISION",
    "HumanReviewService",
    "RELATION_DECISION",
    "REJECT_DECISION",
    "ReviewDecision",
    "ReviewDecisionStore",
    "recalculate_page_safety",
]
