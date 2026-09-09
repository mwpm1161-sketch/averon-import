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
_SYSTEM_RE = re.compile(r"^[A-ZА-ЯЁ]\s*\d{1,3}$", re.IGNORECASE)
_NOTE_RE = re.compile(
    r"^(?:примеч|см\.?\s|согласно\b|по месту\b|в соответствии\b|для\s+справ|\*|•|—\s*прим)",
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
    if len(values) == 1 and _SYSTEM_RE.fullmatch(combined):
        candidates.append(
            _candidate(
                RowRole.CONTEXT,
                qualifier="SYSTEM",
                tier=EvidenceTier.STRONG,
                evidence=("explicit_system_code_shape", "single_context_cell"),
            )
        )
    elif len(values) == 1 and _has_letters(combined) and not has_item_anchor:
        candidates.append(
            _candidate(
                RowRole.CONTEXT,
                qualifier="SECTION",
                tier=EvidenceTier.SUPPORTING,
                evidence=("single_text_context_row", "no_independent_item_anchor"),
            )
        )
    if combined.startswith(("-", "—", "•", "*")) or any(value.startswith(("-", "—", "•")) for value in values):
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
        for row in physical_table.rows:
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
                candidates.extend(_context_component_note_evidence(row, mapping))
            assessments.append(_assessment(row, candidates))

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
