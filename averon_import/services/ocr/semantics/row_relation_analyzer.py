"""Provider-neutral continuation relation evidence.

Stage 7.1.2C deliberately keeps relation analysis separate from both header
mapping and the legacy row assembler.  The analyzer only compares adjacent
physical rows in one trusted table and returns evidence DTOs; it never merges
rows or changes production output.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from averon_import.services.ocr.semantics.functional_analyzer import (
    EvidenceTier,
    _mapping_from_context,
)
from averon_import.services.ocr.semantics.row_evidence import (
    RowRelationAssessment,
    RowRelationState,
    RowRelationType,
    RowRole,
    RowRoleAssessment,
    RowRoleState,
)
from averon_import.services.ocr.semantics.semantic_table import TableAnalysisContext
from averon_import.services.ocr.table_ir import (
    PhysicalCellIR,
    PhysicalRowIR,
    PhysicalTableIR,
)


_IDENTITY_FIELDS = frozenset({"name", "type_mark", "code", "manufacturer"})
_CRITICAL_FIELDS = frozenset({"unit", "quantity", "mass"})
_CONTINUATION_MARKERS = frozenset(
    {"с", "со", "из", "в", "во", "для", "по", "к", "ко", "на", "при", "без", "и", "или", "а"}
)


def _candidate_text(value: Any) -> str:
    return str(value or "").strip()


def _row_fields(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
) -> dict[str, tuple[tuple[PhysicalCellIR, str], ...]]:
    fields: dict[str, list[tuple[PhysicalCellIR, str]]] = {}
    for cell in sorted(row.cells, key=lambda item: item.ref.column_index):
        text = _candidate_text(cell.raw_text)
        if not text and not cell.word_refs:
            continue
        for field_name in mapping.get(cell.ref.column_index, ()):
            fields.setdefault(field_name, []).append((cell, text))
    return {field: tuple(values) for field, values in fields.items()}


def _texts(fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]], field: str) -> tuple[str, ...]:
    return tuple(text for _cell, text in fields.get(field, ()) if text)


def _present_fields(fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]]) -> set[str]:
    return {field for field, values in fields.items() if any(text for _cell, text in values)}


def _role_for(
    row_ref: str,
    assessments: Mapping[str, RowRoleAssessment],
) -> RowRoleAssessment | None:
    return assessments.get(row_ref)


def _structural_state(context: TableAnalysisContext, table: PhysicalTableIR) -> tuple[bool, tuple[str, ...]]:
    raw = context.structural_evidence or table.structural_evidence or {}
    critical = tuple(raw.get("critical_boundary_conflicts") or ())
    material = bool(
        critical
        or raw.get("material_column_disagreement")
        or raw.get("material_column_conflict")
        or raw.get("relevant_material_column_conflict")
    )
    reasons = []
    if critical:
        reasons.append("critical_boundary_conflict")
    if raw.get("material_column_disagreement") or raw.get("material_column_conflict") or raw.get("relevant_material_column_conflict"):
        reasons.append("material_column_disagreement")
    return material, tuple(reasons)


def _lowercase_continuity(texts: Iterable[str]) -> bool:
    for text in texts:
        first = next((char for char in text.strip() if char.isalpha()), "")
        if first and first.islower():
            return True
    return False


def _field_has_lowercase(
    fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]],
    field: str,
) -> bool:
    return _lowercase_continuity(_texts(fields, field))


def _field_has_hyphen(
    fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]],
    field: str,
) -> bool:
    return any(text.rstrip().endswith("-") for text in _texts(fields, field))


def _field_has_quote_or_bracket_open(
    fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]],
    field: str,
) -> bool:
    joined = " ".join(text.strip() for text in _texts(fields, field) if text.strip())
    return bool(joined) and joined.endswith(("(", "«", '"', "["))


def _starts_with_continuation_marker(text: str) -> bool:
    """Return generic Russian grammar evidence, never domain evidence."""

    normalized = text.strip().lstrip("([{\"«—–- ").casefold()
    if not normalized:
        return False
    words = normalized.split()
    if len(words) >= 2 and words[0] == "а" and words[1] == "также":
        return True
    return words[0].rstrip(".,;:") in _CONTINUATION_MARKERS


def _field_has_grammar_marker(
    fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]],
    field: str,
) -> bool:
    return any(_starts_with_continuation_marker(text) for text in _texts(fields, field))


def _field_has_open_grammar_tail(
    fields: Mapping[str, tuple[tuple[PhysicalCellIR, str], ...]],
    field: str,
) -> bool:
    """Recognize an unfinished generic phrase at the previous row's tail."""

    for text in _texts(fields, field):
        words = text.strip().rstrip(".,;:").casefold().split()
        if words and words[-1] in _CONTINUATION_MARKERS - {"а", "и", "или"}:
            return True
    return False


def _relation(
    source: PhysicalRowIR,
    previous: PhysicalRowIR,
    *,
    state: RowRelationState,
    tier: EvidenceTier,
    evidence: Iterable[str] = (),
    contradictions: Iterable[str] = (),
    reasons: Iterable[str] = (),
) -> RowRelationAssessment:
    return RowRelationAssessment(
        source_row_ref=source.ref,
        target_row_ref=previous.ref if state == RowRelationState.CONFIRMED else None,
        candidate_target_refs=(previous.ref,),
        relation_type=RowRelationType.CONTINUATION_OF,
        state=state,
        evidence_score=tier.rank,
        evidence_strength=tier.value,
        evidence=tuple(evidence),
        contradictions=tuple(contradictions),
        reasons=tuple(reasons),
        provenance={"source": "RowRelationAnalyzer"},
    )


class RowRelationAnalyzer:
    """Generate only adjacent-row CONTINUATION_OF assessments."""

    def analyze(
        self,
        physical_table: PhysicalTableIR | TableAnalysisContext,
        context: TableAnalysisContext | None = None,
        functional_graph: Any | None = None,
    ) -> tuple[RowRelationAssessment, ...]:
        if isinstance(physical_table, TableAnalysisContext):
            context = physical_table
            physical_table = context.physical_table
        if context is None:
            context = TableAnalysisContext(physical_table=physical_table)
        if functional_graph is None:
            from averon_import.services.ocr.semantics.functional_analyzer import TableFunctionalAnalyzer

            functional_graph = TableFunctionalAnalyzer().analyze(physical_table, context)

        mapping = _mapping_from_context(context)
        role_assessments = {
            assessment.physical_row_ref.key: assessment
            for assessment in functional_graph.row_role_assessments
        }
        table_rows = {row.ref.row_index: row for row in physical_table.rows}
        material_structure, structural_reasons = _structural_state(context, physical_table)
        mapping_status = context.header_mapping.get(
            "mapping_status",
            context.header_mapping.get("status"),
        )
        schema_status = (
            context.schema_assessment.get("status")
            if context.schema_assessment is not None
            else None
        )
        trusted_mapping = (
            mapping_status is None
            or str(mapping_status).lower() == "trusted"
        ) and (
            schema_status is None
            or str(schema_status).lower() == "supported"
        ) and (
            bool(mapping)
        )
        if not trusted_mapping:
            # Stage C is deliberately scoped to a trusted structured table.
            # The functional graph remains useful diagnostics, but relation
            # inference must not bridge an untrusted semantic schema.
            return ()
        mapped_fields = {
            field
            for fields in mapping.values()
            for field in fields
        }
        relations: list[RowRelationAssessment] = []

        for source in sorted(physical_table.rows, key=lambda row: row.ref.row_index):
            if not source.nonempty:
                continue
            previous = table_rows.get(source.ref.row_index - 1)
            if previous is None or not previous.nonempty:
                continue

            source_fields = _row_fields(source, mapping)
            previous_fields = _row_fields(previous, mapping)
            source_present = _present_fields(source_fields)
            previous_present = _present_fields(previous_fields)
            source_role = _role_for(source.ref.key, role_assessments)
            previous_role = _role_for(previous.ref.key, role_assessments)

            common_fields = source_present.intersection(previous_present)
            source_identity = source_present.intersection(_IDENTITY_FIELDS)
            source_critical = source_present.intersection(_CRITICAL_FIELDS)
            has_position = "position" in source_present
            has_identity = bool(source_identity)
            independent_root = bool(source_critical or (has_position and has_identity))
            root_reasons: list[str] = []
            if "quantity" in source_critical:
                root_reasons.append("new_independent_quantity")
            if "unit" in source_critical:
                root_reasons.append("new_independent_unit")
            if "mass" in source_critical:
                root_reasons.append("new_independent_mass")
            if has_position and has_identity:
                root_reasons.append("position_with_item_identity")

            evidence = [
                "same_physical_table",
                "immediately_adjacent_physical_rows",
            ]
            if trusted_mapping:
                evidence.append("same_trusted_mapped_schema")
            if source_present and source_present.issubset(mapped_fields):
                evidence.append("mapped_source_columns")
            if common_fields:
                evidence.append("compatible_semantic_fields")

            if source_role and source_role.selected_role in {
                RowRole.HEADER,
                RowRole.SERVICE,
                RowRole.CONTEXT,
                RowRole.COMPONENT,
                RowRole.NOTE,
            } and source_role.state in {
                RowRoleState.CONFIRMED,
                RowRoleState.AMBIGUOUS,
            }:
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=RowRelationState.REJECTED,
                        tier=EvidenceTier.HARD_CONTRADICTION,
                        evidence=("confirmed_functional_role",),
                        contradictions=("functional_role_not_continuation",),
                        reasons=("functional_role_locked",),
                    )
                )
                continue

            if independent_root:
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=RowRelationState.REJECTED,
                        tier=EvidenceTier.HARD_CONTRADICTION,
                        evidence=tuple(evidence),
                        contradictions=("independent_item_root_anchor",),
                        reasons=tuple(root_reasons),
                    )
                )
                continue

            previous_identity_only = bool(
                previous_present.intersection(_IDENTITY_FIELDS)
                and not previous_present.intersection(_CRITICAL_FIELDS)
                and "position" not in previous_present
            )
            parent_is_candidate = bool(
                previous_role
                and previous_role.selected_role == RowRole.ITEM_ROOT
            ) or previous_identity_only
            if not parent_is_candidate:
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=RowRelationState.UNRESOLVED,
                        tier=EvidenceTier.SUPPORTING if common_fields else EvidenceTier.WEAK,
                        evidence=tuple(evidence),
                        contradictions=("parent_not_confirmed_item_root",),
                        reasons=("continuation_parent_unavailable",),
                    )
                )
                continue

            source_subset = bool(source_present) and source_present.issubset(previous_present)
            same_identity_fragments = len(source_identity) >= 2 and source_subset
            aligned_identity_fields = source_identity.intersection(previous_present)
            lower_fields = {
                field
                for field in source_identity
                if _field_has_lowercase(source_fields, field)
            }
            hyphen_fields = {
                field
                for field in aligned_identity_fields
                if _field_has_hyphen(previous_fields, field)
            }
            quote_open_fields = {
                field
                for field in aligned_identity_fields
                if _field_has_quote_or_bracket_open(previous_fields, field)
            }
            grammar_marker_fields = {
                field
                for field in aligned_identity_fields
                if _field_has_grammar_marker(source_fields, field)
            }
            open_grammar_tail_fields = {
                field
                for field in aligned_identity_fields
                if _field_has_open_grammar_tail(previous_fields, field)
            }
            parent_strong_anchor = bool(
                previous_role
                and previous_role.selected_role == RowRole.ITEM_ROOT
                and previous_role.state == RowRoleState.CONFIRMED
                and (
                    previous_present.intersection(_CRITICAL_FIELDS)
                    or "position" in previous_present
                    or (
                        len(previous_present.intersection(_IDENTITY_FIELDS)) >= 2
                        and previous_present.intersection(_CRITICAL_FIELDS)
                    )
                )
            )
            textual_support = []
            if lower_fields:
                textual_support.append("lowercase_text_continuity")
            if quote_open_fields or hyphen_fields:
                textual_support.append("open_previous_text_fragment")
            if hyphen_fields:
                evidence.append("hyphenated_field_continuity")
            if quote_open_fields:
                evidence.append("open_quote_or_bracket_continuity")
            if grammar_marker_fields:
                evidence.append("grammar_marker_continuity")
            if open_grammar_tail_fields:
                evidence.append("open_grammar_tail_continuity")
            if textual_support:
                evidence.extend(textual_support)

            # A lowercase marker or an opening shape in one field is not
            # enough to destroy a possible adjacent item.  An aligned hyphen
            # is a strong generic continuation signal.  Otherwise a
            # lowercase source field must be paired with an opening shape in
            # a different aligned identity field.
            cross_field_textual_support = bool(
                lower_fields
                and (quote_open_fields or hyphen_fields)
                and bool(lower_fields.difference(quote_open_fields | hyphen_fields))
            )
            anchored_grammar_continuity = bool(
                grammar_marker_fields
                and parent_strong_anchor
                and source_subset
                and not source_critical
                and not has_position
            )
            open_grammar_continuity = bool(
                open_grammar_tail_fields
                and lower_fields.intersection(open_grammar_tail_fields)
                and source_subset
                and not source_critical
                and not has_position
            )
            strong_textual_continuity = bool(
                hyphen_fields or anchored_grammar_continuity or open_grammar_continuity
            )
            multiple_textual_signals = cross_field_textual_support

            if material_structure:
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=RowRelationState.AMBIGUOUS,
                        tier=EvidenceTier.STRONG if source_subset else EvidenceTier.SUPPORTING,
                        evidence=tuple(evidence),
                        contradictions=("material_structural_ambiguity", *structural_reasons),
                        reasons=("relation_blocked_by_material_structure",),
                    )
                )
                continue

            if source_subset and common_fields and (
                strong_textual_continuity or multiple_textual_signals
            ):
                # Pairwise evidence may be strong before global ancestry is
                # known.  Only an independently confirmed parent is allowed
                # to become a locally confirmed edge; the resolver may use a
                # strong adjacent edge as bounded lookahead evidence.
                edge_state = (
                    RowRelationState.CONFIRMED
                    if parent_strong_anchor or (hyphen_fields and parent_is_candidate and previous_role and previous_role.state == RowRoleState.CONFIRMED)
                    else RowRelationState.AMBIGUOUS
                )
                edge_reasons = (
                    ()
                    if edge_state == RowRelationState.CONFIRMED
                    else ("continuation_parent_requires_global_confirmation",)
                )
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=edge_state,
                        tier=EvidenceTier.STRONG,
                        evidence=(
                            *evidence,
                            "source_fields_subset_of_parent",
                            "identity_fragment_complements_parent",
                            *textual_support,
                        ),
                        reasons=edge_reasons,
                    )
                )
                continue

            if source_subset and common_fields and not source_critical and not has_position:
                relation_reasons = ["continuation_evidence_not_sufficient"]
                if same_identity_fragments:
                    relation_reasons.append("identity_subset_only")
                relations.append(
                    _relation(
                        source,
                        previous,
                        state=RowRelationState.AMBIGUOUS,
                        tier=EvidenceTier.SUPPORTING,
                        evidence=(*evidence, "source_fields_subset_of_parent"),
                        reasons=tuple(relation_reasons),
                    )
                )
                continue

            relations.append(
                _relation(
                    source,
                    previous,
                    state=RowRelationState.REJECTED,
                    tier=EvidenceTier.WEAK,
                    evidence=tuple(evidence),
                    reasons=("no_continuation_evidence",),
                )
            )

        return tuple(relations)


__all__ = ["RowRelationAnalyzer"]
