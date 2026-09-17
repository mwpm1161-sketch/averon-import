"""Shadow functional row evidence for provider-neutral physical tables.

Stage 7.1.2B intentionally stops at evidence production.  The analyzer does
not merge rows, repair OCR, decide page status, or produce canonical output.
It consumes only a typed physical table and bounded semantic snapshots.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Iterable, Mapping

from averon_import.services.ocr.physical_evidence import PhysicalEvidenceSnapshot
from averon_import.services.ocr.semantics.row_evidence import (
    RoleCandidate,
    RowRelationAssessment,
    RowRole,
    RowRoleAssessment,
    RowRoleState,
)
from averon_import.services.ocr.semantics.semantic_table import TableAnalysisContext
from averon_import.services.ocr.table_ir import (
    PhysicalCellIR,
    PhysicalCellRef,
    PhysicalRowIR,
    PhysicalRowRef,
    PhysicalTableIR,
    PhysicalTableRef,
    PhysicalWordIR,
    PhysicalWordRef,
    _bbox,
    freeze_mapping,
    thaw_value,
)


class EvidenceTier(str, Enum):
    HARD_CONTRADICTION = "HARD_CONTRADICTION"
    STRONG = "STRONG"
    SUPPORTING = "SUPPORTING"
    WEAK = "WEAK"

    @property
    def rank(self) -> int:
        return {
            EvidenceTier.HARD_CONTRADICTION: 0,
            EvidenceTier.WEAK: 1,
            EvidenceTier.SUPPORTING: 2,
            EvidenceTier.STRONG: 3,
        }[self]


_INTEGER_RE = re.compile(r"^\d+$")
_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_SYSTEM_ALNUM_RE = re.compile(
    r"^[A-ZА-ЯЁ]{1,4}\s*\d{1,3}(?:[.,]\d+)?\s*\*?"
    r"(?:\s*,\s*[A-ZА-ЯЁ]{1,4}\s*\d{1,3}(?:[.,]\d+)?\s*\*?)*$",
    re.IGNORECASE,
)
_SYSTEM_ALPHA_RE = re.compile(
    r"^[А-ЯЁ]{2,4}\s*\*?(?:\s*,\s*[А-ЯЁ]{2,4}\s*\*?)*$",
    re.IGNORECASE,
)
_NOTE_RE = re.compile(
    r"^(?:примеч|см\.?\s|согласно\b|по месту\b|в соответствии\b|для\s+справ|\*|•|—\s*прим)",
    re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:[IVXLCDM]+\s*[_.)-]?|\d+(?:\.\d+)+\s*[.)-]?)\s+\S",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping_from_context(context: TableAnalysisContext) -> dict[int, tuple[str, ...]]:
    snapshot = context.header_mapping
    raw = snapshot.get("selected_mapping") or snapshot.get("mapping") or {}
    result: dict[int, tuple[str, ...]] = {}
    if not isinstance(raw, Mapping):
        return result
    for column, fields in raw.items():
        try:
            column_index = int(column)
        except (TypeError, ValueError):
            continue
        if isinstance(fields, str):
            result[column_index] = (fields,)
        else:
            result[column_index] = tuple(str(field) for field in fields)
    return result


def _header_rows_from_context(context: TableAnalysisContext) -> set[int]:
    raw = context.header_mapping.get("header_candidate_rows")
    if raw is None:
        raw = context.header_mapping.get("header_rows") or ()
    try:
        return {int(row) for row in raw}
    except (TypeError, ValueError):
        return set()


def _row_occupied(row: PhysicalRowIR) -> list[PhysicalCellIR]:
    return [
        cell for cell in sorted(row.cells, key=lambda item: item.ref.column_index)
        if cell.raw_text.strip() or cell.word_refs
    ]


def _field_texts(row: PhysicalRowIR, mapping: Mapping[int, tuple[str, ...]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for cell in _row_occupied(row):
        for field_name in mapping.get(cell.ref.column_index, ()):
            result.setdefault(field_name, []).append(_text(cell.raw_text))
    return result


def _has_letters(value: str) -> bool:
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", value))


def _is_numeric(value: str) -> bool:
    return bool(_NUMBER_RE.fullmatch(value.replace(" ", "")))


def _candidate(
    role: RowRole,
    *,
    qualifier: str | None,
    tier: EvidenceTier,
    evidence: Iterable[str],
    contradictions: Iterable[str] = (),
    reasons: Iterable[str] = (),
    provenance: Mapping[str, Any] | None = None,
) -> RoleCandidate:
    return RoleCandidate(
        role=role,
        qualifier=qualifier,
        evidence_score=tier.rank,
        evidence_strength=tier.value,
        evidence=tuple(evidence),
        contradictions=tuple(contradictions),
        reasons=tuple(reasons),
        provenance=dict(provenance or {}),
    )


def _header_adjacency_evidence(
    row: PhysicalRowIR,
    header_rows: set[int],
) -> tuple[RoleCandidate, ...]:
    if row.ref.row_index not in header_rows:
        return ()
    return (
        _candidate(
            RowRole.HEADER,
            qualifier="COLUMN_HEADER",
            tier=EvidenceTier.STRONG,
            evidence=("header_mapping_selected_row", "physical_header_row"),
            provenance={"source": "HeaderMappingResult"},
        ),
    )


def _numbering_evidence(
    row: PhysicalRowIR,
    *,
    header_rows: set[int],
    mapping: Mapping[int, tuple[str, ...]],
) -> tuple[tuple[RoleCandidate, ...], dict[str, Any]]:
    diagnostics: dict[str, Any] = {}
    if not header_rows or row.ref.row_index <= max(header_rows):
        return (), diagnostics
    if row.ref.row_index - max(header_rows) > 2:
        return (), diagnostics

    occupied = _row_occupied(row)
    if len(occupied) < 2:
        return (), diagnostics
    values = [_text(cell.raw_text) for cell in occupied]
    numeric_cells = [
        (cell, int(value))
        for cell, value in zip(occupied, values)
        if _INTEGER_RE.fullmatch(value)
    ]
    if len(numeric_cells) < 2:
        return (), diagnostics

    field_text = _field_texts(row, mapping)
    product_fields = {
        field
        for field in ("name", "type_mark", "code", "manufacturer")
        if any(_has_letters(value) for value in field_text.get(field, ()))
    }
    if product_fields:
        diagnostics["rejected_due_to_independent_item_anchor"] = sorted(product_fields)
        return (
            _candidate(
                RowRole.SERVICE,
                qualifier="NUMBERING_BAND",
                tier=EvidenceTier.HARD_CONTRADICTION,
                evidence=("numeric_cells_near_header",),
                contradictions=("independent_item_anchor",),
            ),
        ), diagnostics

    ordinal_values = [value for _cell, value in numeric_cells]
    offset_counts = Counter(
        value - cell.ref.column_index
        for cell, value in numeric_cells
    )
    offset_support_count = max(offset_counts.values(), default=0)
    strongest_offsets = tuple(
        offset
        for offset, support in offset_counts.items()
        if support == offset_support_count
    )
    offset_conflict = (
        offset_support_count < 2
        or len(strongest_offsets) != 1
    )
    ordinal_offset = (
        strongest_offsets[0]
        if not offset_conflict
        else None
    )
    expected = []
    matches = []
    mismatches = []
    if ordinal_offset is not None:
        for cell, observed in numeric_cells:
            expected_value = cell.ref.column_index + ordinal_offset
            expected.append({"column_index": cell.ref.column_index, "value": expected_value})
            entry = {
                "column_index": cell.ref.column_index,
                "observed": observed,
                "expected": expected_value,
            }
            if observed == expected_value:
                matches.append(entry)
            else:
                mismatches.append(entry)
    diagnostics.update(
        {
            "ordinal_offset": ordinal_offset,
            "offset_support_count": offset_support_count,
            "offset_conflict": offset_conflict,
            "ordinal_values": ordinal_values,
            "expected_ordinals": expected,
            "ordinal_matches": matches,
            "ordinal_mismatches": mismatches,
        }
    )

    non_numeric = [value for value in values if not _INTEGER_RE.fullmatch(value)]
    if non_numeric:
        diagnostics["non_numeric_tokens"] = non_numeric[:20]
        return (
            _candidate(
                RowRole.SERVICE,
                qualifier="NUMBERING_BAND",
                tier=EvidenceTier.SUPPORTING,
                evidence=("numeric_cells_near_header", "sparse_ordinal_shape"),
                contradictions=("non_numeric_ocr_token",),
                reasons=("numbering_band_ambiguous",),
            ),
        ), diagnostics

    duplicates = sorted(value for value, count in Counter(ordinal_values).items() if count > 1)
    non_monotonic = any(left >= right for left, right in zip(ordinal_values, ordinal_values[1:]))
    outliers = [
        {
            "column_index": mismatch["column_index"],
            "observed": mismatch["observed"],
            "expected": mismatch["expected"],
        }
        for mismatch in mismatches
        if mismatch["observed"] >= 10 and mismatch["expected"] < 10
    ]
    diagnostics.update(
        {
            "ordinal_duplicates": duplicates,
            "ordinal_non_monotonic": non_monotonic,
            "ocr_outliers": outliers,
        }
    )
    contradictions = []
    reasons = []
    if duplicates:
        contradictions.append("duplicate_ordinal")
        reasons.append("numbering_band_ambiguous")
    if non_monotonic:
        contradictions.append("non_monotonic_ordinal")
        reasons.append("numbering_band_ambiguous")
    if offset_conflict:
        contradictions.append("offset_conflict")
        reasons.append("ordinal_offset_unresolved")
    if mismatches:
        contradictions.append("numbering_band_ocr_anomaly")
        reasons.append("numbering_band_ocr_anomaly")
    if outliers:
        diagnostics["numbering_band_ocr_anomaly"] = True
    strong_shape = (
        len(numeric_cells) >= 3
        and not duplicates
        and not non_monotonic
        and not offset_conflict
    )
    tier = EvidenceTier.STRONG if strong_shape else EvidenceTier.SUPPORTING
    return (
        _candidate(
            RowRole.SERVICE,
            qualifier="NUMBERING_BAND",
            tier=tier,
            evidence=(
                "numeric_cells_near_header",
                "physical_column_order",
                "compact_row_geometry",
                "absence_of_independent_item_anchor",
            ),
            contradictions=contradictions,
            reasons=reasons,
        ),
    ), diagnostics


def _item_evidence(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
) -> tuple[RoleCandidate, ...]:
    field_text = _field_texts(row, mapping)
    identity_fields = {
        field
        for field in ("name", "type_mark", "code", "manufacturer")
        if any(_has_letters(value) for value in field_text.get(field, ()))
    }
    numeric_fields = {
        field
        for field in ("quantity", "mass")
        if any(_is_numeric(value) for value in field_text.get(field, ()))
    }
    unit_fields = {
        field for field in ("unit",) if field_text.get(field)
    }
    position_fields = {"position"} if field_text.get("position") else set()
    if not identity_fields and not numeric_fields and not unit_fields and not position_fields:
        return ()
    independent_groups = int(bool(identity_fields)) + int(bool(numeric_fields or unit_fields))
    strong = bool(
        identity_fields
        and (numeric_fields or unit_fields or position_fields)
    ) or len(identity_fields) >= 2
    if strong:
        tier = EvidenceTier.STRONG
        evidence = ("independent_item_anchor", "mapped_physical_columns")
    elif independent_groups >= 1 and (identity_fields or numeric_fields):
        tier = EvidenceTier.SUPPORTING
        evidence = ("partial_item_anchor", "mapped_physical_columns")
    else:
        tier = EvidenceTier.WEAK
        evidence = ("single_weak_item_field",)
    contradictions = ()
    if not identity_fields and position_fields and not numeric_fields and not unit_fields:
        contradictions = ("position_only_is_not_item_proof",)
    return (
        _candidate(
            RowRole.ITEM_ROOT,
            qualifier=None,
            tier=tier,
            evidence=evidence,
            contradictions=contradictions,
        ),
    )


def _context_component_note_evidence(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    *,
    allow_alphabetic_system: bool = False,
) -> tuple[RoleCandidate, ...]:
    occupied = _row_occupied(row)
    values = [_text(cell.raw_text) for cell in occupied]
    combined = " ".join(value for value in values if value)
    if not combined:
        return ()
    item_fields = _field_texts(row, mapping)
    has_item_anchor = any(
        any(_has_letters(value) for value in item_fields.get(field, ()))
        for field in ("name", "type_mark", "code", "manufacturer")
    )
    candidates: list[RoleCandidate] = []
    system_shape = bool(
        len(values) == 1
        and (
            _SYSTEM_ALNUM_RE.fullmatch(combined)
            or allow_alphabetic_system and _SYSTEM_ALPHA_RE.fullmatch(combined)
        )
    )
    if system_shape:
        candidates.append(
            _candidate(
                RowRole.CONTEXT,
                qualifier="SYSTEM",
                tier=EvidenceTier.STRONG,
                evidence=("explicit_system_code_shape", "single_context_cell"),
            )
        )
    elif (
        len(values) == 1
        and _has_letters(combined)
        and not has_item_anchor
        and next(
            (char for char in combined if char.isalpha()), ""
        ).isupper()
    ):
        candidates.append(
            _candidate(
                RowRole.CONTEXT,
                qualifier="SECTION",
                tier=EvidenceTier.SUPPORTING,
                evidence=("single_text_context_row", "no_independent_item_anchor"),
            )
        )
    if combined.startswith(("-", "–", "—", "•", "*")) or any(value.startswith(("-", "–", "—", "•")) for value in values):
        candidates.append(
            _candidate(
                RowRole.COMPONENT,
                qualifier="BULLET",
                tier=EvidenceTier.SUPPORTING,
                evidence=("bullet_component_marker", "bounded_row_text"),
            )
        )
    if _NOTE_RE.search(combined):
        candidates.append(
            _candidate(
                RowRole.NOTE,
                qualifier=None,
                tier=EvidenceTier.SUPPORTING,
                evidence=("explicit_note_marker", "bounded_row_text"),
            )
        )
    return tuple(candidates)


def _is_standalone_system_row(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    *,
    allow_alphabetic: bool = False,
) -> bool:
    occupied = _row_occupied(row)
    if len(occupied) != 1:
        return False
    field_text = _field_texts(row, mapping)
    if set(field_text) - {"name", "position"}:
        return False
    combined = " ".join(
        _text(cell.raw_text) for cell in occupied if _text(cell.raw_text)
    )
    return bool(
        combined
        and (
            _SYSTEM_ALNUM_RE.fullmatch(combined)
            or allow_alphabetic and _SYSTEM_ALPHA_RE.fullmatch(combined)
        )
    )


def _section_before_system_candidate(
    row: PhysicalRowIR,
    next_row: PhysicalRowIR | None,
    mapping: Mapping[int, tuple[str, ...]],
) -> RoleCandidate | None:
    """Use adjacent heading/system topology as provider-neutral evidence."""

    # A section candidate must not be manufactured from the same weak
    # alphabetic token that it would then enable as a SYSTEM row.  Only an
    # independently strong alphanumeric system shape may prove this sparse
    # preceding row to be a section.  Alphabetic-only systems are admitted by
    # the normal topology path only after a section has already been confirmed
    # by its own evidence.
    if next_row is None or not _is_standalone_system_row(next_row, mapping):
        return None
    occupied = _row_occupied(row)
    if not occupied or len(occupied) > 2:
        return None
    field_text = _field_texts(row, mapping)
    if set(field_text) - {"name", "position"}:
        return None
    if any(field_text.get(field) for field in ("unit", "quantity", "mass")):
        return None
    if any(
        any(_has_letters(value) for value in field_text.get(field, ()))
        for field in ("type_mark", "code", "manufacturer")
    ):
        return None
    combined = " ".join(
        _text(cell.raw_text) for cell in occupied if _text(cell.raw_text)
    )
    if not combined or not _has_letters(combined):
        return None
    return _candidate(
        RowRole.CONTEXT,
        qualifier="SECTION",
        tier=EvidenceTier.STRONG,
        evidence=(
            "section_precedes_standalone_system",
            "sparse_heading_topology",
            "no_independent_product_anchor",
        ),
    )


def _is_package_host(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
    assessment: RowRoleAssessment,
) -> bool:
    if (
        assessment.selected_role != RowRole.ITEM_ROOT
        or assessment.state != RowRoleState.CONFIRMED
    ):
        return False
    field_text = _field_texts(row, mapping)
    return any(
        bool(re.fullmatch(r"\s*комп(?:л(?:\.|ект)?|лект)?\s*", value, re.IGNORECASE))
        for value in field_text.get("unit", ())
    )


def _package_component_candidate(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
) -> RoleCandidate | None:
    field_text = _field_texts(row, mapping)
    present = {
        field for field, values in field_text.items()
        if any(_text(value) for value in values)
    }
    if not present or "position" in present or "mass" in present:
        return None
    identity = {
        field for field in ("name", "type_mark", "code", "manufacturer")
        if any(_has_letters(value) for value in field_text.get(field, ()))
    }
    if not identity:
        return None
    combined = " ".join(
        _text(value)
        for values in field_text.values()
        for value in values
        if _text(value)
    )
    bullet = combined.lstrip().startswith(("-", "–", "—", "•", "*"))
    # A component can have its own quantity/unit. A non-bulleted row with a
    # unit is treated as a new item boundary; this prevents swallowing the
    # unrelated item following a package block.
    if "unit" in present and not bullet:
        return None
    if not bullet and "quantity" not in present:
        return None
    evidence = ["package_parent_context", "included_component_identity"]
    if bullet:
        evidence.append("bullet_component_marker")
    if "quantity" in present:
        evidence.append("explicit_component_quantity")
    return _candidate(
        RowRole.COMPONENT,
        qualifier="INCLUDED",
        tier=EvidenceTier.STRONG,
        evidence=evidence,
    )


def _section_heading_evidence(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
) -> tuple[RoleCandidate, ...]:
    """Recognize generic heading topology without engineering vocabulary.

    A heading candidate is intentionally structural: it requires sparse text
    in the name/position area and a generic heading shape.  Product anchors
    in mapped product/critical columns win, so model text such as ``IV.
    ABC-400`` with unit/quantity remains an item.
    """

    occupied = _row_occupied(row)
    if not occupied or len(occupied) > 2:
        return ()
    field_text = _field_texts(row, mapping)
    mapped_fields = set(field_text)
    if mapped_fields - {"name", "position"}:
        return ()
    if any(field_text.get(field) for field in ("unit", "quantity", "mass")):
        return ()
    if any(
        any(_has_letters(value) for value in field_text.get(field, ()))
        for field in ("type_mark", "code", "manufacturer")
    ):
        return ()
    combined = " ".join(_text(cell.raw_text) for cell in occupied).strip()
    if not combined:
        return ()
    heading_shape = bool(_SECTION_HEADING_RE.match(combined))
    sparse_colon = len(occupied) <= 2 and combined.rstrip().endswith(":")
    first_alpha = next((char for char in combined if char.isalpha()), "")
    if sparse_colon and not heading_shape and first_alpha.islower():
        # A lower-case terminal fragment is generic continuation evidence,
        # not a new section heading (for example, a wrapped product phrase).
        return ()
    if not heading_shape and not sparse_colon:
        return ()
    return (
        _candidate(
            RowRole.CONTEXT,
            qualifier="SECTION",
            tier=EvidenceTier.STRONG,
            evidence=(
                "generic_section_heading_syntax"
                if heading_shape
                else "generic_section_terminal_colon",
                "sparse_heading_topology",
                "no_independent_product_anchor",
            ),
        ),
    )


def _assessment(
    row: PhysicalRowIR,
    candidates: Iterable[RoleCandidate],
) -> RowRoleAssessment:
    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        candidate_tuple = (
            _candidate(
                RowRole.UNKNOWN,
                qualifier=None,
                tier=EvidenceTier.WEAK,
                evidence=("no_sufficient_role_evidence",),
                reasons=("role_unresolved",),
            ),
        )
    positive_candidates = tuple(
        candidate
        for candidate in candidate_tuple
        if candidate.evidence_strength != EvidenceTier.HARD_CONTRADICTION.value
    )
    if positive_candidates:
        best_rank = max(candidate.evidence_score for candidate in positive_candidates)
        best = tuple(candidate for candidate in positive_candidates if candidate.evidence_score == best_rank)
    else:
        # HARD_CONTRADICTION is retained in the graph for auditability, but it
        # is never allowed to select a role or act as role support.
        best_rank = 0
        best = ()
    identities = {(candidate.role, candidate.qualifier) for candidate in best}
    selected = best[0] if len(identities) == 1 else None
    structural_section = next(
        (
            candidate
            for candidate in best
            if candidate.role == RowRole.CONTEXT
            and candidate.qualifier == "SECTION"
            and (
                "generic_section_heading_syntax" in candidate.evidence
                or "section_precedes_standalone_system" in candidate.evidence
            )
        ),
        None,
    )
    if structural_section is not None:
        # A generic, independently evidenced heading wins a tie against the
        # weak identity/position item hypothesis. Product anchors in critical
        # columns prevent the section candidate from being emitted earlier.
        selected = structural_section
    # Weak evidence is deliberately not a role decision.  In particular, a
    # lone position/number must remain unresolved rather than becoming an
    # item or a numbering row by proximity alone.
    if selected is not None and selected.evidence_strength == EvidenceTier.WEAK.value:
        selected = None
    state = RowRoleState.UNRESOLVED
    if selected is not None and selected.evidence_strength == EvidenceTier.STRONG.value:
        state = (
            RowRoleState.AMBIGUOUS
            if selected.contradictions
            else RowRoleState.CONFIRMED
        )
    elif selected is not None and selected.evidence_strength == EvidenceTier.SUPPORTING.value:
        state = RowRoleState.AMBIGUOUS
    second_rank = max(
        (candidate.evidence_score for candidate in positive_candidates if candidate.evidence_score < best_rank),
        default=0,
    )
    decision_margin = float(best_rank - second_rank) if selected is not None else 0.0
    contradictions = tuple(
        reason
        for candidate in candidate_tuple
        for reason in candidate.contradictions
    )
    reasons = tuple(
        reason
        for candidate in candidate_tuple
        for reason in candidate.reasons
    )
    return RowRoleAssessment(
        physical_row_ref=row.ref,
        candidates=candidate_tuple,
        selected_role=selected.role if selected else None,
        selected_qualifier=selected.qualifier if selected else None,
        state=state,
        evidence_score=float(best_rank),
        evidence_strength=selected.evidence_strength if selected else "ambiguous",
        decision_margin=decision_margin,
        evidence=tuple(
            evidence
            for candidate in best
            for evidence in candidate.evidence
        ),
        contradictions=contradictions,
        reasons=reasons,
        provenance={"source": "TableFunctionalAnalyzer"},
    )


@dataclass(frozen=True, slots=True)
class FunctionalEvidenceGraph:
    table_ref: PhysicalTableRef
    row_role_assessments: tuple[RowRoleAssessment, ...]
    row_relation_assessments: tuple[RowRelationAssessment, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_role_assessments", tuple(self.row_role_assessments))
        object.__setattr__(self, "row_relation_assessments", tuple(self.row_relation_assessments))
        object.__setattr__(self, "diagnostics", freeze_mapping(self.diagnostics))

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_ref": self.table_ref.as_dict(),
            "row_role_assessments": [assessment.as_dict() for assessment in self.row_role_assessments],
            "row_relation_assessments": [relation.as_dict() for relation in self.row_relation_assessments],
            "diagnostics": thaw_value(self.diagnostics),
        }


class TableFunctionalAnalyzer:
    """Produce functional row evidence without semantic resolution."""

    def analyze(
        self,
        physical_table: PhysicalTableIR | TableAnalysisContext,
        context: TableAnalysisContext | None = None,
    ) -> FunctionalEvidenceGraph:
        if isinstance(physical_table, TableAnalysisContext):
            context = physical_table
            physical_table = context.physical_table
        if context is None:
            context = TableAnalysisContext(physical_table=physical_table)
        mapping = _mapping_from_context(context)
        header_rows = _header_rows_from_context(context)
        assessments: list[RowRoleAssessment] = []
        numbering_rows: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        physical_rows = tuple(
            sorted(physical_table.rows, key=lambda value: value.ref.row_index)
        )
        for row_index, row in enumerate(physical_rows):
            previous_assessment = assessments[-1] if assessments else None
            previous_section_confirmed = bool(
                previous_assessment is not None
                and previous_assessment.selected_role == RowRole.CONTEXT
                and previous_assessment.selected_qualifier == "SECTION"
                and previous_assessment.state == RowRoleState.CONFIRMED
            )
            candidates = list(_header_adjacency_evidence(row, header_rows))
            if row.ref.row_index not in header_rows:
                numbering_candidates, numbering_diagnostics = _numbering_evidence(
                    row,
                    header_rows=header_rows,
                    mapping=mapping,
                )
                candidates.extend(numbering_candidates)
                if (
                    numbering_candidates
                    and numbering_candidates[0].qualifier == "NUMBERING_BAND"
                    and numbering_candidates[0].evidence_strength != EvidenceTier.HARD_CONTRADICTION.value
                ):
                    numbering_rows.append({"row_ref": row.ref.as_dict(), **numbering_diagnostics})
                    if numbering_diagnostics.get("numbering_band_ocr_anomaly"):
                        anomalies.append({"row_ref": row.ref.as_dict(), **numbering_diagnostics})
                candidates.extend(_item_evidence(row, mapping))
                candidates.extend(_section_heading_evidence(row, mapping))
                candidates.extend(
                    _context_component_note_evidence(
                        row,
                        mapping,
                        allow_alphabetic_system=previous_section_confirmed,
                    )
                )
            next_row = (
                physical_rows[row_index + 1]
                if row_index + 1 < len(physical_rows)
                else None
            )
            section_before_system = _section_before_system_candidate(
                row, next_row, mapping
            )
            if section_before_system is not None:
                candidates = [
                    candidate
                    for candidate in candidates
                    if not (
                        candidate.role == RowRole.CONTEXT
                        and candidate.qualifier == "SECTION"
                    )
                ]
                candidates.append(section_before_system)
            assessments.append(_assessment(row, candidates))

        # Package rows introduce a bounded component block. This is based on
        # the structured unit column and physical adjacency, not on product
        # names or document-specific vocabulary.
        normalized_assessments: list[RowRoleAssessment] = []
        package_block_active = False
        for row, assessment in zip(physical_rows, assessments):
            if _is_package_host(row, mapping, assessment):
                package_block_active = True
                normalized_assessments.append(assessment)
                continue
            if package_block_active:
                component = _package_component_candidate(row, mapping)
                if component is not None:
                    normalized_assessments.append(_assessment(row, [component]))
                    continue
                if (
                    assessment.selected_role in {
                        RowRole.CONTEXT,
                        RowRole.HEADER,
                        RowRole.SERVICE,
                    }
                    or assessment.state == RowRoleState.CONFIRMED
                    and assessment.selected_role == RowRole.ITEM_ROOT
                ):
                    package_block_active = False
                elif any(
                    _text(value)
                    for cell in row.cells
                    for value in (_text(cell.raw_text),)
                ):
                    # A non-component physical row ends the package block;
                    # it is still assessed by the normal evidence path.
                    package_block_active = False
            normalized_assessments.append(assessment)
        assessments = normalized_assessments

        selected_roles = Counter(
            assessment.selected_role.value
            for assessment in assessments
            if assessment.selected_role is not None
        )
        diagnostics = {
            "physical_row_count": len(physical_table.rows),
            "header_row_refs": [
                row.ref.as_dict()
                for row in physical_table.rows
                if row.ref.row_index in header_rows
            ],
            "numbering_rows": numbering_rows,
            "numbering_band_ocr_anomalies": anomalies,
            "role_coverage": dict(sorted(selected_roles.items())),
            "relations_inferred": False,
            "canonical_projection": False,
            "provider_provenance_ignored": True,
        }
        return FunctionalEvidenceGraph(
            table_ref=physical_table.ref,
            row_role_assessments=tuple(assessments),
            row_relation_assessments=(),
            diagnostics=diagnostics,
        )


def _cell_key(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d+)\s*[:/,]\s*(-?\d+)\s*", value)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def physical_table_ir_from_snapshot(
    snapshot: PhysicalEvidenceSnapshot,
    *,
    table_ref: PhysicalTableRef | None = None,
    page_size: tuple[float, float] | None = None,
) -> PhysicalTableIR:
    """Adapt the existing diagnostics snapshot into the Stage A physical IR."""

    grid_data = snapshot.grid if isinstance(snapshot.grid, Mapping) else {}
    x_boundaries = tuple(float(value) for value in grid_data.get("x_boundaries", ()))
    y_boundaries = tuple(float(value) for value in grid_data.get("y_boundaries", ()))
    width, height = page_size or (0.0, 0.0)
    if (width <= 0 or height <= 0) and x_boundaries and y_boundaries:
        width, height = 1.0, 1.0
    if width > 1.0 and x_boundaries and max(x_boundaries) <= 1.0:
        x_boundaries = tuple(value * width for value in x_boundaries)
    if height > 1.0 and y_boundaries and max(y_boundaries) <= 1.0:
        y_boundaries = tuple(value * height for value in y_boundaries)
    if table_ref is None:
        table_ref = PhysicalTableRef(
            page_number=1,
            table_index=0,
            detector_source="snapshot-runtime-local",
        )

    source_words = tuple(snapshot.spatial_words or ())
    words = tuple(
        PhysicalWordIR(
            ref=PhysicalWordRef(table_ref, index),
            text=_text(word.get("text")) if isinstance(word, Mapping) else _text(word),
            bbox=(word.get("vertices") or word.get("bbox")) if isinstance(word, Mapping) else None,
            provenance={"source": "PhysicalEvidenceSnapshot"},
        )
        for index, word in enumerate(source_words)
    )
    assigned_by_cell: dict[tuple[int, int], tuple[PhysicalWordRef, ...]] = {}
    assigned_named: dict[str, tuple[PhysicalWordRef, ...]] = {}
    for raw_key, raw_refs in (snapshot.assigned_word_refs or {}).items():
        refs = tuple(PhysicalWordRef(table_ref, int(index)) for index in raw_refs)
        assigned_named[str(raw_key)] = refs
        parsed = _cell_key(raw_key)
        if parsed is not None:
            assigned_by_cell[parsed] = refs

    rows: list[PhysicalRowIR] = []
    for raw_row in snapshot.physical_rows or ():
        row_index = int(raw_row.get("source_row_index", raw_row.get("row_index", 0)))
        row_ref = PhysicalRowRef(table_ref, row_index)
        cells: list[PhysicalCellIR] = []
        for raw_cell in raw_row.get("cells") or ():
            column_index = int(raw_cell.get("column_index", 0))
            cell_ref = PhysicalCellRef(
                row=row_ref,
                column_index=column_index,
                source_cell_index=int(raw_cell.get("source_cell_index", -1)),
            )
            cells.append(
                PhysicalCellIR(
                    ref=cell_ref,
                    bbox=raw_cell.get("bbox"),
                    raw_text=_text(raw_cell.get("raw_text", raw_cell.get("text", ""))),
                    row_span=int(raw_cell.get("row_span", 1)),
                    column_span=int(raw_cell.get("column_span", 1)),
                    word_refs=assigned_by_cell.get((row_index, column_index), ()),
                    provider_cell_refs=(str(raw_cell.get("provider_cell_ref")),)
                    if raw_cell.get("provider_cell_ref") is not None
                    else (),
                    raster_evidence=raw_cell.get("raster_evidence", {}),
                )
            )
        rows.append(
            PhysicalRowIR(
                ref=row_ref,
                bbox=raw_row.get("bbox"),
                cells=tuple(cells),
                raster_nonempty=bool(
                    raw_row.get("raster_nonempty")
                    or any(cell.raw_text.strip() or cell.word_refs for cell in cells)
                ),
                raster_metrics=raw_row.get("raster_metrics", {}),
            )
        )

    bounds = None
    if x_boundaries and y_boundaries:
        bounds = (x_boundaries[0], y_boundaries[0], x_boundaries[-1], y_boundaries[-1])
    if bounds is None:
        bounds = _bbox(snapshot.selected_table_bounds)
    return PhysicalTableIR(
        ref=table_ref,
        bounds=bounds,
        x_boundaries=x_boundaries,
        y_boundaries=y_boundaries,
        rows=tuple(rows),
        words=words,
        assigned_word_refs=assigned_named,
        grid_source=str(grid_data.get("source", "snapshot")),
        grid_evidence=thaw_value(grid_data),
        structural_evidence=thaw_value(snapshot.structural_evidence or {}),
        raster_witness=thaw_value(snapshot.raster_witness or {}),
        provenance={"source": "PhysicalEvidenceSnapshot", "provider_neutral": True},
    )


__all__ = [
    "EvidenceTier",
    "FunctionalEvidenceGraph",
    "TableFunctionalAnalyzer",
    "physical_table_ir_from_snapshot",
]
