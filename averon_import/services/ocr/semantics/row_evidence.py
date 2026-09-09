"""Provider-neutral row role and relation evidence DTOs.

This module intentionally does not decide roles or relations.  It only gives
future analyzers a typed, immutable place to exchange evidence and decisions.
The existing mapper/assembler remain the source of current production
behavior until a later stage explicitly consumes these DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..table_ir import PhysicalRowRef, freeze_mapping, thaw_value


class RowRole(str, Enum):
    HEADER = "HEADER"
    SERVICE = "SERVICE"
    ITEM_ROOT = "ITEM_ROOT"
    CONTINUATION = "CONTINUATION"
    CONTEXT = "CONTEXT"
    COMPONENT = "COMPONENT"
    NOTE = "NOTE"
    UNKNOWN = "UNKNOWN"


class RowQualifier(str, Enum):
    COLUMN_HEADER = "COLUMN_HEADER"
    REPEATED_HEADER = "REPEATED_HEADER"
    NUMBERING_BAND = "NUMBERING_BAND"
    TITLE_BLOCK = "TITLE_BLOCK"
    SECTION = "SECTION"
    SYSTEM = "SYSTEM"
    TITLE = "TITLE"
    SERVICE_TAIL = "SERVICE_TAIL"
    BULLET = "BULLET"
    IDENTITY_ONLY = "IDENTITY_ONLY"


class RowRoleState(str, Enum):
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class SemanticReviewImpact(str, Enum):
    """Safety impact of an unresolved physical semantic row."""

    OUTPUT_CRITICAL = "OUTPUT_CRITICAL"
    NON_OUTPUT = "NON_OUTPUT"
    SAFETY_SPECIAL = "SAFETY_SPECIAL"
    NONE = "NONE"


class RowRelationType(str, Enum):
    CONTINUATION_OF = "CONTINUATION_OF"


class RowRelationState(str, Enum):
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    REJECTED = "REJECTED"


def _enum_value(value: Enum | str, enum_type: type[Enum]) -> Enum:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))


def _tuple_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(item.value if isinstance(item, Enum) else str(item) for item in value)


def _qualifier_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else str(value)


@dataclass(frozen=True, slots=True)
class RoleCandidate:
    """One role hypothesis plus the evidence supplied by an analyzer."""

    role: RowRole
    qualifier: str | None = None
    qualifiers: tuple[str, ...] = ()
    evidence_score: float = 0.0
    evidence_strength: str = "none"
    evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum_value(self.role, RowRole))
        qualifiers = _tuple_strings(self.qualifiers)
        qualifier = _qualifier_value(self.qualifier)
        if qualifier and qualifier not in qualifiers:
            qualifiers = (qualifier,) + qualifiers
        object.__setattr__(self, "qualifier", qualifier)
        object.__setattr__(self, "qualifiers", qualifiers)
        object.__setattr__(self, "evidence", _tuple_strings(self.evidence))
        object.__setattr__(self, "contradictions", _tuple_strings(self.contradictions))
        object.__setattr__(self, "reasons", _tuple_strings(self.reasons))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "qualifier": self.qualifier,
            "qualifiers": list(self.qualifiers),
            "evidence_score": self.evidence_score,
            "evidence_strength": self.evidence_strength,
            "evidence": list(self.evidence),
            "contradictions": list(self.contradictions),
            "reasons": list(self.reasons),
            "provenance": thaw_value(self.provenance),
        }


RowRoleCandidate = RoleCandidate


@dataclass(frozen=True, slots=True)
class RowRoleAssessment:
    physical_row_ref: PhysicalRowRef
    candidates: tuple[RoleCandidate, ...] = ()
    selected_role: RowRole | None = None
    selected_qualifier: str | None = None
    state: RowRoleState = RowRoleState.UNRESOLVED
    evidence_score: float = 0.0
    evidence_strength: str = "none"
    decision_margin: float = 0.0
    evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if any(not isinstance(candidate, RoleCandidate) for candidate in candidates):
            raise TypeError("candidates must contain RoleCandidate values")
        object.__setattr__(self, "candidates", candidates)
        if self.selected_role is not None:
            object.__setattr__(self, "selected_role", _enum_value(self.selected_role, RowRole))
        object.__setattr__(self, "selected_qualifier", _qualifier_value(self.selected_qualifier))
        object.__setattr__(self, "state", _enum_value(self.state, RowRoleState))
        object.__setattr__(self, "evidence", _tuple_strings(self.evidence))
        object.__setattr__(self, "contradictions", _tuple_strings(self.contradictions))
        object.__setattr__(self, "reasons", _tuple_strings(self.reasons))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))
        if self.state == RowRoleState.CONFIRMED:
            if self.selected_role is None:
                raise ValueError("confirmed row role must select a role")
            matching = [candidate for candidate in candidates if candidate.role == self.selected_role]
            if self.selected_qualifier is not None:
                matching = [
                    candidate
                    for candidate in matching
                    if self.selected_qualifier in candidate.qualifiers
                    or candidate.qualifier == self.selected_qualifier
                ]
            if len(matching) != 1:
                raise ValueError("confirmed row role must resolve exactly one candidate")

    @property
    def selected(self) -> RoleCandidate | None:
        if self.selected_role is None:
            return None
        matching = [candidate for candidate in self.candidates if candidate.role == self.selected_role]
        if self.selected_qualifier is not None:
            matching = [
                candidate
                for candidate in matching
                if self.selected_qualifier in candidate.qualifiers
                or candidate.qualifier == self.selected_qualifier
            ]
        return matching[0] if len(matching) == 1 else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_row_ref": self.physical_row_ref.as_dict(),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "selected_role": self.selected_role.value if self.selected_role else None,
            "selected_qualifier": self.selected_qualifier,
            "state": self.state.value,
            "evidence_score": self.evidence_score,
            "evidence_strength": self.evidence_strength,
            "decision_margin": self.decision_margin,
            "evidence": list(self.evidence),
            "contradictions": list(self.contradictions),
            "reasons": list(self.reasons),
            "provenance": thaw_value(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class RowRelationAssessment:
    source_row_ref: PhysicalRowRef
    target_row_ref: PhysicalRowRef | None = None
    candidate_target_refs: tuple[PhysicalRowRef, ...] = ()
    relation_type: RowRelationType = RowRelationType.CONTINUATION_OF
    state: RowRelationState = RowRelationState.UNRESOLVED
    evidence_score: float = 0.0
    evidence_strength: str = "none"
    decision_margin: float = 0.0
    evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_type", _enum_value(self.relation_type, RowRelationType))
        object.__setattr__(self, "state", _enum_value(self.state, RowRelationState))
        object.__setattr__(self, "candidate_target_refs", tuple(self.candidate_target_refs))
        object.__setattr__(self, "evidence", _tuple_strings(self.evidence))
        object.__setattr__(self, "contradictions", _tuple_strings(self.contradictions))
        object.__setattr__(self, "reasons", _tuple_strings(self.reasons))
        object.__setattr__(self, "provenance", freeze_mapping(self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_row_ref": self.source_row_ref.as_dict(),
            "target_row_ref": self.target_row_ref.as_dict() if self.target_row_ref else None,
            "candidate_target_refs": [ref.as_dict() for ref in self.candidate_target_refs],
            "relation_type": self.relation_type.value,
            "state": self.state.value,
            "evidence_score": self.evidence_score,
            "evidence_strength": self.evidence_strength,
            "decision_margin": self.decision_margin,
            "evidence": list(self.evidence),
            "contradictions": list(self.contradictions),
            "reasons": list(self.reasons),
            "provenance": thaw_value(self.provenance),
        }


__all__ = [
    "RoleCandidate",
    "RowRelationAssessment",
    "RowRelationState",
    "RowRelationType",
    "RowRole",
    "RowRoleAssessment",
    "RowRoleCandidate",
    "RowRoleState",
    "SemanticReviewImpact",
    "RowQualifier",
]
