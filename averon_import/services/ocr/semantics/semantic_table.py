"""Provider-neutral semantic table IR and conservation accounting.

The classes in this module are deliberately descriptive.  They do not infer
row roles, merge rows, repair values, or replace the existing assembler.  A
future analyzer may populate them in shadow mode and the conservation report
will fail closed when physical evidence has no explicit safe disposition.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from ..table_ir import (
    BBox,
    PhysicalCellRef,
    PhysicalRowIR,
    PhysicalRowRef,
    PhysicalTableIR,
    PhysicalWordRef,
    _bbox,
    _thaw,
    freeze_mapping,
)
from .row_evidence import (
    RowRelationAssessment,
    RowRelationState,
    RowRelationType,
    RowRole,
    RowRoleAssessment,
    RowRoleState,
    SemanticReviewImpact,
)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _enum(value: Enum | str, enum_type: type[Enum]) -> Enum:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))


def _snapshot(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if isinstance(value, Mapping):
        return _thaw(freeze_mapping(value))
    return value


@dataclass(frozen=True, slots=True)
class TableAnalysisContext:
    """Immutable boundary between physical evidence and semantic assessments."""

    physical_table: PhysicalTableIR
    header_mapping: Mapping[str, Any] = field(default_factory=dict)
    family_assessment: Mapping[str, Any] | None = None
    schema_assessment: Mapping[str, Any] | None = None
    observed_schema: Mapping[str, Any] | None = None
    profile_match: Mapping[str, Any] | None = None
    structural_evidence: Mapping[str, Any] | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "header_mapping", freeze_mapping(self.header_mapping))
        if self.family_assessment is not None:
            object.__setattr__(self, "family_assessment", freeze_mapping(self.family_assessment))
        if self.schema_assessment is not None:
            object.__setattr__(self, "schema_assessment", freeze_mapping(self.schema_assessment))
        if self.structural_evidence is not None:
            object.__setattr__(self, "structural_evidence", freeze_mapping(self.structural_evidence))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    @classmethod
    def from_assessments(
        cls,
        physical_table: PhysicalTableIR,
        *,
        header_mapping: Any = None,
        family_assessment: Any = None,
        schema_assessment: Any = None,
        observed_schema: Any = None,
        profile_match: Any = None,
        structural_evidence: Any = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> TableAnalysisContext:
        return cls(
            physical_table=physical_table,
            header_mapping=_snapshot(header_mapping) or {},
            family_assessment=_snapshot(family_assessment),
            schema_assessment=_snapshot(schema_assessment),
            observed_schema=_snapshot(observed_schema),
            profile_match=_snapshot(profile_match),
            structural_evidence=_snapshot(structural_evidence),
            provenance=provenance or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_table": self.physical_table.as_dict(),
            "header_mapping": _thaw(self.header_mapping),
            "family_assessment": _thaw(self.family_assessment) if self.family_assessment is not None else None,
            "schema_assessment": _thaw(self.schema_assessment) if self.schema_assessment is not None else None,
            "observed_schema": _thaw(self.observed_schema) if self.observed_schema is not None else None,
            "profile_match": _thaw(self.profile_match) if self.profile_match is not None else None,
            "structural_evidence": _thaw(self.structural_evidence) if self.structural_evidence is not None else None,
            "provenance": _thaw(self.provenance),
        }

class FieldOrigin(str, Enum):
    OCR = "OCR"
    HUMAN = "HUMAN"
    TRUSTED_RULE = "TRUSTED_RULE"


SemanticOrigin = FieldOrigin


@dataclass(frozen=True, slots=True)
class ValueCandidate:
    value: str
    origin: FieldOrigin = FieldOrigin.OCR
    auto_trusted: bool = False
    review_reason: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _enum(self.origin, FieldOrigin))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))
        if self.auto_trusted and self.origin == FieldOrigin.OCR:
            raise ValueError("OCR candidates must remain candidate-only")

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "origin": self.origin.value,
            "auto_trusted": self.auto_trusted,
            "review_reason": self.review_reason,
            "provenance": _thaw(self.provenance),
        }


FieldValueCandidate = ValueCandidate


@dataclass(frozen=True, slots=True)
class SourceFieldFragment:
    field: str
    text: str
    physical_row_ref: PhysicalRowRef
    physical_cell_ref: PhysicalCellRef | None = None
    word_refs: tuple[PhysicalWordRef, ...] = ()
    bbox: BBox = (0.0, 0.0, 0.0, 0.0)
    origin: FieldOrigin = FieldOrigin.OCR
    raw_text: str | None = None
    provider_refs: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.physical_row_ref, PhysicalRowRef):
            raise TypeError("source fragment requires a PhysicalRowRef")
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "origin", _enum(self.origin, FieldOrigin))
        object.__setattr__(self, "word_refs", tuple(self.word_refs))
        object.__setattr__(self, "provider_refs", _strings(self.provider_refs))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "text": self.text,
            "raw_text": self.raw_text,
            "origin": self.origin.value,
            "physical_row_ref": self.physical_row_ref.as_dict(),
            "physical_cell_ref": self.physical_cell_ref.as_dict() if self.physical_cell_ref else None,
            "word_refs": [ref.as_dict() for ref in self.word_refs],
            "bbox": list(self.bbox),
            "provider_refs": list(self.provider_refs),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class LogicalFieldValue:
    field: str
    canonical_text: str | None = None
    source_fragments: tuple[SourceFieldFragment, ...] = ()
    candidates: tuple[ValueCandidate, ...] = ()
    origin: FieldOrigin = FieldOrigin.OCR
    review_reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_fragments", tuple(self.source_fragments))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "origin", _enum(self.origin, FieldOrigin))
        object.__setattr__(self, "review_reasons", _strings(self.review_reasons))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))
        if self.origin == FieldOrigin.OCR and self.canonical_text is not None and not self.source_fragments:
            raise ValueError("OCR canonical value requires a supporting source fragment")
        if any(fragment.field != self.field for fragment in self.source_fragments):
            raise ValueError("source fragment field must match logical field")

    @property
    def candidate_only(self) -> bool:
        return self.canonical_text is None and bool(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "canonical_text": self.canonical_text,
            "source_fragments": [fragment.as_dict() for fragment in self.source_fragments],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "origin": self.origin.value,
            "review_reasons": list(self.review_reasons),
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class LogicalSpecificationItem:
    logical_id: str
    physical_row_refs: tuple[PhysicalRowRef, ...] = ()
    fields: Mapping[str, LogicalFieldValue] = field(default_factory=dict)
    bbox: BBox = (0.0, 0.0, 0.0, 0.0)
    review_reasons: tuple[str, ...] = ()
    context_refs: tuple[PhysicalRowRef, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        physical_row_refs = tuple(self.physical_row_refs)
        if len(set(ref.key for ref in physical_row_refs)) != len(physical_row_refs):
            raise ValueError("logical item physical row refs must be unique")
        object.__setattr__(self, "physical_row_refs", physical_row_refs)
        object.__setattr__(self, "context_refs", tuple(self.context_refs))
        normalized_fields: dict[str, LogicalFieldValue] = {}
        for field_name, field_value in self.fields.items():
            if not isinstance(field_value, LogicalFieldValue):
                raise TypeError("logical item fields must contain LogicalFieldValue values")
            if not isinstance(field_name, str) or field_name != field_value.field:
                raise ValueError("logical item field key must match LogicalFieldValue.field")
            if field_value.origin == FieldOrigin.OCR:
                if any(fragment.physical_row_ref.key not in {ref.key for ref in physical_row_refs}
                       for fragment in field_value.source_fragments):
                    raise ValueError("OCR source fragment row must belong to the logical item")
            normalized_fields[str(field_name)] = field_value
        object.__setattr__(self, "fields", freeze_mapping(normalized_fields))
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "review_reasons", _strings(self.review_reasons))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "physical_row_refs": [ref.as_dict() for ref in self.physical_row_refs],
            "fields": {name: value.as_dict() for name, value in self.fields.items()},
            "bbox": list(self.bbox),
            "review_reasons": list(self.review_reasons),
            "context_refs": [ref.as_dict() for ref in self.context_refs],
            "provenance": _thaw(self.provenance),
        }

    @property
    def field_values(self) -> Mapping[str, LogicalFieldValue]:
        return self.fields


class DispositionValidation(str, Enum):
    VALIDATED = "VALIDATED"
    UNVALIDATED = "UNVALIDATED"


class InvalidDispositionState(str, Enum):
    UNKNOWN = "UNKNOWN"
    UNRESOLVED = "UNRESOLVED"
    ORPHAN = "ORPHAN"
    ROLE_CONFLICT = "ROLE_CONFLICT"
    RELATION_CONFLICT = "RELATION_CONFLICT"
    MULTIPLE_DISPOSITIONS = "MULTIPLE_DISPOSITIONS"
    UNACCOUNTED = "UNACCOUNTED"


@dataclass(frozen=True, slots=True)
class ResolvedPhysicalDisposition:
    physical_row_ref: PhysicalRowRef
    role: RowRole = RowRole.UNKNOWN
    qualifier: str | None = None
    validation_state: DispositionValidation = DispositionValidation.UNVALIDATED
    role_state: RowRoleState = RowRoleState.UNRESOLVED
    logical_item_id: str | None = None
    relation: RowRelationAssessment | None = None
    evidence_score: float = 0.0
    evidence_strength: str = "none"
    evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    review_impact: SemanticReviewImpact = SemanticReviewImpact.NONE
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, RowRole))
        object.__setattr__(self, "validation_state", _enum(self.validation_state, DispositionValidation))
        object.__setattr__(self, "role_state", _enum(self.role_state, RowRoleState))
        object.__setattr__(self, "evidence", _strings(self.evidence))
        object.__setattr__(self, "contradictions", _strings(self.contradictions))
        object.__setattr__(self, "reasons", _strings(self.reasons))
        object.__setattr__(
            self,
            "review_impact",
            _enum(self.review_impact, SemanticReviewImpact),
        )
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    @property
    def validated(self) -> bool:
        return self.validation_state == DispositionValidation.VALIDATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_row_ref": self.physical_row_ref.as_dict(),
            "role": self.role.value,
            "qualifier": self.qualifier,
            "validation_state": self.validation_state.value,
            "role_state": self.role_state.value,
            "logical_item_id": self.logical_item_id,
            "relation": self.relation.as_dict() if self.relation else None,
            "evidence_score": self.evidence_score,
            "evidence_strength": self.evidence_strength,
            "evidence": list(self.evidence),
            "contradictions": list(self.contradictions),
            "reasons": list(self.reasons),
            "review_impact": self.review_impact.value,
            "provenance": _thaw(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class SemanticConservationReport:
    total_physical_rows: int
    resolved_physical_rows: int
    unaccounted_physical_rows: int
    invalid_physical_rows: tuple[PhysicalRowRef, ...] = ()
    unresolved_rows: tuple[PhysicalRowRef, ...] = ()
    reasons: tuple[str, ...] = ()
    invalid_reasons_by_row: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    continuation_count: int = 0
    logical_item_count: int = 0
    component_count: int = 0
    note_count: int = 0
    service_count: int = 0
    context_count: int = 0
    semantic_conservation_rate: float = 0.0
    semantic_conservation_pass: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "invalid_physical_rows", tuple(self.invalid_physical_rows))
        object.__setattr__(self, "unresolved_rows", tuple(self.unresolved_rows))
        object.__setattr__(self, "reasons", _strings(self.reasons))
        object.__setattr__(
            self,
            "invalid_reasons_by_row",
            freeze_mapping({key: tuple(value) for key, value in self.invalid_reasons_by_row.items()}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_physical_rows": self.total_physical_rows,
            "resolved_physical_rows": self.resolved_physical_rows,
            "unaccounted_physical_rows": self.unaccounted_physical_rows,
            "invalid_physical_rows": [ref.as_dict() for ref in self.invalid_physical_rows],
            "unresolved_rows": [ref.as_dict() for ref in self.unresolved_rows],
            "reasons": list(self.reasons),
            "invalid_reasons_by_row": _thaw(self.invalid_reasons_by_row),
            "continuation_count": self.continuation_count,
            "logical_item_count": self.logical_item_count,
            "component_count": self.component_count,
            "note_count": self.note_count,
            "service_count": self.service_count,
            "context_count": self.context_count,
            "semantic_conservation_rate": self.semantic_conservation_rate,
            "semantic_conservation_pass": self.semantic_conservation_pass,
        }


def _row_key(ref: PhysicalRowRef) -> str:
    return ref.key


def _physical_refs(rows: Iterable[PhysicalRowIR | PhysicalRowRef]) -> tuple[PhysicalRowRef, ...]:
    refs = []
    for row in rows:
        if isinstance(row, PhysicalRowIR):
            if row.nonempty:
                refs.append(row.ref)
        else:
            refs.append(row)
    return tuple(refs)


def evaluate_semantic_conservation(
    physical_rows: Iterable[PhysicalRowIR | PhysicalRowRef],
    dispositions: Iterable[ResolvedPhysicalDisposition],
    *,
    logical_items: Iterable[LogicalSpecificationItem] = (),
    relations: Iterable[RowRelationAssessment] = (),
) -> SemanticConservationReport:
    """Evaluate explicit physical-row accounting and relation-graph safety."""

    refs = _physical_refs(physical_rows)
    dispositions_tuple = tuple(dispositions)
    items = tuple(logical_items)
    relation_tuple = tuple(relations)
    ref_by_key: dict[str, PhysicalRowRef] = {}
    invalid_refs: dict[str, PhysicalRowRef] = {}
    invalid: dict[str, set[str]] = defaultdict(set)

    def mark(ref: PhysicalRowRef | None, reason: str) -> None:
        if ref is None:
            return
        key = _row_key(ref)
        invalid_refs.setdefault(key, ref)
        invalid[key].add(reason)

    for ref in refs:
        key = _row_key(ref)
        if key in ref_by_key:
            mark(ref, "ROOT_CONFLICT")
            mark(ref_by_key[key], "ROOT_CONFLICT")
        else:
            ref_by_key[key] = ref

    by_key: dict[str, list[ResolvedPhysicalDisposition]] = defaultdict(list)
    for disposition in dispositions_tuple:
        key = _row_key(disposition.physical_row_ref)
        by_key[key].append(disposition)
        if key not in ref_by_key or disposition.physical_row_ref != ref_by_key[key]:
            mark(disposition.physical_row_ref, "FOREIGN_PHYSICAL_REF")

    counts = Counter()
    for ref in refs:
        key = _row_key(ref)
        candidates = by_key.get(key, [])
        if not candidates:
            mark(ref, "UNACCOUNTED")
            continue
        if len(candidates) != 1:
            mark(ref, "MULTIPLE_DISPOSITIONS")
            continue
        disposition = candidates[0]
        counts[disposition.role] += 1
        if disposition.role == RowRole.UNKNOWN:
            mark(ref, "UNKNOWN")
        if disposition.validation_state != DispositionValidation.VALIDATED:
            mark(ref, "UNRESOLVED")
        if disposition.role_state != RowRoleState.CONFIRMED:
            mark(ref, "UNRESOLVED")
        if not disposition.evidence:
            mark(ref, "UNRESOLVED")
        if disposition.role in {RowRole.ITEM_ROOT, RowRole.CONTINUATION} and not disposition.logical_item_id:
            mark(ref, "ORPHAN")

    authoritative_by_source: dict[str, list[RowRelationAssessment]] = defaultdict(list)
    for relation in relation_tuple:
        source_key = _row_key(relation.source_row_ref)
        target_key = _row_key(relation.target_row_ref) if relation.target_row_ref else None
        source_internal = source_key in ref_by_key and relation.source_row_ref == ref_by_key[source_key]
        target_internal = target_key is not None and target_key in ref_by_key and relation.target_row_ref == ref_by_key[target_key]
        if not source_internal:
            mark(relation.source_row_ref, "FOREIGN_PHYSICAL_REF")
        if relation.target_row_ref is not None and not target_internal:
            mark(relation.target_row_ref, "FOREIGN_PHYSICAL_REF")
        if not source_internal:
            continue
        authoritative_by_source[source_key].append(relation)
        if relation.state == RowRelationState.CONFIRMED and (
            relation.relation_type != RowRelationType.CONTINUATION_OF
            or not target_internal
            or relation.target_row_ref is None
        ):
            mark(relation.source_row_ref, "RELATION_CONFLICT")

    def relation_signature(relation: RowRelationAssessment) -> tuple[Any, ...]:
        return (
            relation.source_row_ref.key,
            relation.target_row_ref.key if relation.target_row_ref else None,
            relation.relation_type,
            relation.state,
        )

    # The relation embedded in a disposition is evidence that must agree with
    # the authoritative relation collection; it is not a second graph.
    for key, row_dispositions in by_key.items():
        if len(row_dispositions) != 1 or key not in ref_by_key:
            continue
        disposition = row_dispositions[0]
        attached = disposition.relation
        if attached is not None:
            attached_source_key = _row_key(attached.source_row_ref)
            if attached_source_key != key:
                mark(attached.source_row_ref, "FOREIGN_PHYSICAL_REF")
            if attached.target_row_ref is not None and _row_key(attached.target_row_ref) not in ref_by_key:
                mark(attached.target_row_ref, "FOREIGN_PHYSICAL_REF")
        if disposition.role == RowRole.CONTINUATION:
            if attached is None:
                mark(disposition.physical_row_ref, "ORPHAN")
                continue
            if (
                attached.relation_type != RowRelationType.CONTINUATION_OF
                or attached.state != RowRelationState.CONFIRMED
                or attached.target_row_ref is None
            ):
                mark(disposition.physical_row_ref, "RELATION_CONFLICT")
            authoritative = authoritative_by_source.get(key, [])
            if len(authoritative) != 1:
                mark(disposition.physical_row_ref, "RELATION_CONFLICT")
            elif relation_signature(attached) != relation_signature(authoritative[0]):
                mark(disposition.physical_row_ref, "RELATION_CONFLICT")
        elif attached is not None:
            mark(disposition.physical_row_ref, "RELATION_CONFLICT")

    parent_by_source: dict[str, PhysicalRowRef] = {}
    for source_key, source_relations in authoritative_by_source.items():
        confirmed = [relation for relation in source_relations if relation.state == RowRelationState.CONFIRMED]
        if not confirmed:
            continue
        source_ref = ref_by_key[source_key]
        source_dispositions = by_key.get(source_key, [])
        if len(confirmed) != 1 or len(source_dispositions) != 1:
            mark(source_ref, "RELATION_CONFLICT")
            continue
        relation = confirmed[0]
        if source_dispositions[0].role != RowRole.CONTINUATION or relation.target_row_ref is None:
            mark(source_ref, "RELATION_CONFLICT")
            continue
        parent_by_source[source_key] = relation.target_row_ref

    def resolve_root(start_key: str, logical_item_id: str | None) -> str | None:
        current = start_key
        visited: list[str] = []
        while True:
            if current in visited:
                for cycle_key in visited[visited.index(current):]:
                    mark(ref_by_key.get(cycle_key), "RELATION_CYCLE")
                return None
            visited.append(current)
            current_dispositions = by_key.get(current, [])
            if len(current_dispositions) != 1 or current not in ref_by_key:
                mark(ref_by_key.get(current), "ORPHAN")
                mark(ref_by_key.get(start_key), "ORPHAN")
                return None
            current_disposition = current_dispositions[0]
            if current_disposition.role == RowRole.ITEM_ROOT:
                if current_disposition.logical_item_id != logical_item_id:
                    mark(current_disposition.physical_row_ref, "ROOT_CONFLICT")
                    mark(ref_by_key.get(start_key), "ROOT_CONFLICT")
                    return None
                return current
            if current_disposition.role != RowRole.CONTINUATION:
                mark(current_disposition.physical_row_ref, "ROOT_CONFLICT")
                mark(ref_by_key.get(start_key), "ROOT_CONFLICT")
                return None
            if current_disposition.logical_item_id != logical_item_id:
                mark(current_disposition.physical_row_ref, "ROOT_CONFLICT")
                mark(ref_by_key.get(start_key), "ROOT_CONFLICT")
                return None
            parent = parent_by_source.get(current)
            if parent is None:
                mark(current_disposition.physical_row_ref, "ORPHAN")
                mark(ref_by_key.get(start_key), "ORPHAN")
                return None
            current = parent.key

    for key, row_dispositions in by_key.items():
        if len(row_dispositions) != 1 or key not in ref_by_key:
            continue
        disposition = row_dispositions[0]
        if disposition.role == RowRole.CONTINUATION:
            resolve_root(key, disposition.logical_item_id)

    item_rows: dict[str, list[str]] = defaultdict(list)
    for item in items:
        seen_in_item: set[str] = set()
        for ref in item.physical_row_refs:
            key = _row_key(ref)
            if key in seen_in_item:
                mark(ref, "ROOT_CONFLICT")
            seen_in_item.add(key)
            item_rows[item.logical_id].append(key)
            if key not in ref_by_key or ref != ref_by_key[key]:
                mark(ref, "FOREIGN_PHYSICAL_REF")

    seen_item_rows: dict[str, str] = {}
    for item_id, row_keys in item_rows.items():
        for key in row_keys:
            previous = seen_item_rows.get(key)
            if previous is not None and previous != item_id:
                mark(ref_by_key.get(key), "ROOT_CONFLICT")
            seen_item_rows[key] = item_id

    for key, row_dispositions in by_key.items():
        if len(row_dispositions) != 1 or key not in ref_by_key:
            continue
        disposition = row_dispositions[0]
        if disposition.role in {RowRole.ITEM_ROOT, RowRole.CONTINUATION}:
            item_id = disposition.logical_item_id
            if not item_id or key not in item_rows.get(item_id, ()):
                mark(disposition.physical_row_ref, "ORPHAN")

    for item_id, row_keys in item_rows.items():
        for key in row_keys:
            row_dispositions = by_key.get(key, [])
            if len(row_dispositions) == 1 and row_dispositions[0].role not in {
                RowRole.ITEM_ROOT,
                RowRole.CONTINUATION,
            }:
                mark(row_dispositions[0].physical_row_ref, "ROOT_CONFLICT")

    valid_count = sum(1 for key in ref_by_key if not invalid.get(key))
    invalid_rows = tuple(invalid_refs[key] for key in sorted(invalid_refs))
    unresolved_rows = invalid_rows
    total = len(refs)
    rate = valid_count / total if total else 1.0
    reasons = tuple(sorted({reason for values in invalid.values() for reason in values}))
    return SemanticConservationReport(
        total_physical_rows=total,
        resolved_physical_rows=max(0, valid_count),
        unaccounted_physical_rows=sum(1 for key in ref_by_key if not by_key.get(key)),
        invalid_physical_rows=invalid_rows,
        unresolved_rows=unresolved_rows,
        reasons=reasons,
        invalid_reasons_by_row={key: tuple(sorted(values)) for key, values in invalid.items()},
        continuation_count=counts[RowRole.CONTINUATION],
        logical_item_count=len(items),
        component_count=counts[RowRole.COMPONENT],
        note_count=counts[RowRole.NOTE],
        service_count=counts[RowRole.SERVICE],
        context_count=counts[RowRole.CONTEXT],
        semantic_conservation_rate=rate,
        semantic_conservation_pass=not invalid_rows and valid_count == total,
    )


@dataclass(frozen=True, slots=True)
class SemanticTableIR:
    analysis_context: TableAnalysisContext
    row_roles: tuple[RowRoleAssessment, ...] = ()
    relations: tuple[RowRelationAssessment, ...] = ()
    dispositions: tuple[ResolvedPhysicalDisposition, ...] = ()
    logical_items: tuple[LogicalSpecificationItem, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    conservation: SemanticConservationReport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_roles", tuple(self.row_roles))
        object.__setattr__(self, "relations", tuple(self.relations))
        object.__setattr__(self, "dispositions", tuple(self.dispositions))
        object.__setattr__(self, "logical_items", tuple(self.logical_items))
        object.__setattr__(self, "diagnostics", freeze_mapping(self.diagnostics))
        if self.conservation is None:
            object.__setattr__(
                self,
                "conservation",
                evaluate_semantic_conservation(
                    self.analysis_context.physical_table.rows,
                    self.dispositions,
                    logical_items=self.logical_items,
                    relations=self.relations,
                ),
            )

    @classmethod
    def from_parts(
        cls,
        analysis_context: TableAnalysisContext,
        *,
        row_roles: Iterable[RowRoleAssessment] = (),
        relations: Iterable[RowRelationAssessment] = (),
        dispositions: Iterable[ResolvedPhysicalDisposition] = (),
        logical_items: Iterable[LogicalSpecificationItem] = (),
        diagnostics: Mapping[str, Any] | None = None,
    ) -> SemanticTableIR:
        return cls(
            analysis_context=analysis_context,
            row_roles=tuple(row_roles),
            relations=tuple(relations),
            dispositions=tuple(dispositions),
            logical_items=tuple(logical_items),
            diagnostics=diagnostics or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_context": self.analysis_context.as_dict(),
            "row_roles": [assessment.as_dict() for assessment in self.row_roles],
            "relations": [relation.as_dict() for relation in self.relations],
            "dispositions": [disposition.as_dict() for disposition in self.dispositions],
            "logical_items": [item.as_dict() for item in self.logical_items],
            "diagnostics": _thaw(self.diagnostics),
            "conservation": self.conservation.as_dict() if self.conservation else None,
        }


__all__ = [
    "DispositionValidation",
    "FieldOrigin",
    "FieldValueCandidate",
    "InvalidDispositionState",
    "LogicalFieldValue",
    "LogicalSpecificationItem",
    "SemanticConservationReport",
    "SemanticOrigin",
    "SemanticTableIR",
    "ResolvedPhysicalDisposition",
    "SourceFieldFragment",
    "TableAnalysisContext",
    "ValueCandidate",
    "evaluate_semantic_conservation",
]
