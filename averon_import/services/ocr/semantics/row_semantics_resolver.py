"""Deterministic shadow resolver for functional row relationships."""

from __future__ import annotations

from collections import defaultdict
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
from averon_import.services.ocr.semantics.semantic_table import (
    DispositionValidation,
    FieldOrigin,
    LogicalFieldValue,
    LogicalSpecificationItem,
    ResolvedPhysicalDisposition,
    SemanticTableIR,
    SourceFieldFragment,
    TableAnalysisContext,
    evaluate_semantic_conservation,
)
from averon_import.services.ocr.table_ir import (
    PhysicalTableIR,
)


_CRITICAL_FIELDS = frozenset({"position", "unit", "quantity", "mass"})


def _mapped_present_fields(
    row: Any,
    mapping: Mapping[int, tuple[str, ...]],
) -> set[str]:
    fields: set[str] = set()
    for cell in row.cells:
        if cell.raw_text.strip() or cell.word_refs:
            fields.update(mapping.get(cell.ref.column_index, ()))
    return fields


def _field_fragments(
    row: PhysicalRowIR,
    mapping: Mapping[int, tuple[str, ...]],
) -> tuple[SourceFieldFragment, ...]:
    fragments: list[SourceFieldFragment] = []
    for cell in sorted(row.cells, key=lambda item: item.ref.column_index):
        text = cell.raw_text.strip()
        if not text:
            continue
        for field_name in mapping.get(cell.ref.column_index, ()):
            fragments.append(
                SourceFieldFragment(
                    field=field_name,
                    text=text,
                    raw_text=cell.raw_text,
                    physical_row_ref=row.ref,
                    physical_cell_ref=cell.ref,
                    word_refs=cell.word_refs,
                    bbox=cell.bbox,
                    origin=FieldOrigin.OCR,
                    provider_refs=cell.provider_cell_refs,
                    provenance={
                        "source": "GlobalRowSemanticsResolver",
                        "physical_column_index": cell.ref.column_index,
                    },
                )
            )
    return tuple(fragments)


def _join_fragments(fragments: Iterable[SourceFieldFragment]) -> str:
    ordered = tuple(fragments)
    if not ordered:
        return ""
    result = ordered[0].text.strip()
    for fragment in ordered[1:]:
        text = fragment.text.strip()
        if not text:
            continue
        if not result:
            result = text
        elif result.endswith("-"):
            result += text
        else:
            result += " " + text
    return result


def _union_bbox(rows: Iterable[PhysicalRowIR]) -> tuple[float, float, float, float]:
    bboxes = [row.bbox for row in rows]
    if not bboxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _relation_is_plausible(relation: RowRelationAssessment) -> bool:
    if relation.state not in {RowRelationState.AMBIGUOUS, RowRelationState.UNRESOLVED}:
        return False
    if not relation.candidate_target_refs:
        return False
    if "parent_not_confirmed_item_root" in relation.contradictions:
        return False
    return relation.evidence_strength in {
        EvidenceTier.STRONG.value,
        EvidenceTier.SUPPORTING.value,
    }


class GlobalRowSemanticsResolver:
    """Resolve row roles and continuation chains into shadow SemanticTableIR."""

    def resolve(
        self,
        physical_table: PhysicalTableIR | TableAnalysisContext,
        context: TableAnalysisContext | None = None,
        functional_graph: Any | None = None,
        relations: Iterable[RowRelationAssessment] | None = None,
    ) -> SemanticTableIR:
        if isinstance(physical_table, TableAnalysisContext):
            context = physical_table
            physical_table = context.physical_table
        if context is None:
            context = TableAnalysisContext(physical_table=physical_table)
        if functional_graph is None:
            from averon_import.services.ocr.semantics.functional_analyzer import TableFunctionalAnalyzer

            functional_graph = TableFunctionalAnalyzer().analyze(physical_table, context)
        if relations is None:
            from averon_import.services.ocr.semantics.row_relation_analyzer import RowRelationAnalyzer

            relations = RowRelationAnalyzer().analyze(physical_table, context, functional_graph)
        relation_tuple = tuple(relations)
        rows = tuple(sorted(physical_table.rows, key=lambda row: row.ref.row_index))
        row_by_key = {row.ref.key: row for row in rows}
        mapping = _mapping_from_context(context)
        role_by_key = {
            assessment.physical_row_ref.key: assessment
            for assessment in functional_graph.row_role_assessments
        }
        relation_by_source: dict[str, list[RowRelationAssessment]] = defaultdict(list)
        relation_conflicts = 0
        invalid_relation_keys: set[str] = set()

        for relation in relation_tuple:
            source_key = relation.source_row_ref.key
            target_key = relation.target_row_ref.key if relation.target_row_ref else None
            valid_source = source_key in row_by_key and relation.source_row_ref == row_by_key[source_key].ref
            valid_target = target_key is None or (
                target_key in row_by_key and relation.target_row_ref == row_by_key[target_key].ref
            )
            adjacent = bool(
                relation.target_row_ref
                and relation.target_row_ref.table == relation.source_row_ref.table
                and relation.source_row_ref.row_index == relation.target_row_ref.row_index + 1
            )
            if (
                not valid_source
                or not valid_target
                or relation.relation_type != RowRelationType.CONTINUATION_OF
                or (relation.state == RowRelationState.CONFIRMED and not adjacent)
            ):
                invalid_relation_keys.add(source_key)
                relation_conflicts += 1
            if (
                relation.state == RowRelationState.CONFIRMED
                and valid_source
                and _mapped_present_fields(row_by_key[source_key], mapping).intersection(_CRITICAL_FIELDS)
            ):
                # A confirmed continuation cannot compose an amount, unit or
                # position from a second physical row.  Keep the relation as
                # diagnostic evidence, but refuse to project it into an item.
                invalid_relation_keys.add(source_key)
                relation_conflicts += 1
            relation_by_source[source_key].append(relation)

        for source_key, candidates in relation_by_source.items():
            if len(candidates) > 1:
                relation_conflicts += 1

        confirmed_relation_by_source: dict[str, RowRelationAssessment] = {}
        for source_key, candidates in relation_by_source.items():
            confirmed = [
                relation
                for relation in candidates
                if relation.state == RowRelationState.CONFIRMED
                and relation.target_row_ref is not None
                and source_key not in invalid_relation_keys
            ]
            if len(confirmed) == 1 and len(candidates) == 1:
                confirmed_relation_by_source[source_key] = confirmed[0]
            elif confirmed:
                relation_conflicts += 1

        confirmed_roots: set[str] = set()
        plausible_relations: set[str] = set()
        for row in rows:
            assessment = role_by_key.get(row.ref.key)
            candidates = relation_by_source.get(row.ref.key, ())
            if any(_relation_is_plausible(relation) for relation in candidates):
                plausible_relations.add(row.ref.key)
            if (
                assessment
                and assessment.selected_role == RowRole.ITEM_ROOT
                and assessment.state == RowRoleState.CONFIRMED
                and row.ref.key not in confirmed_relation_by_source
                and row.ref.key not in plausible_relations
            ):
                confirmed_roots.add(row.ref.key)

        parent_by_source = {
            source_key: relation.target_row_ref.key
            for source_key, relation in confirmed_relation_by_source.items()
            if relation.target_row_ref is not None
        }
        cycle_keys: set[str] = set()

        def root_for(key: str, path: tuple[str, ...] = ()) -> str | None:
            if key in path:
                cycle_keys.update(path[path.index(key):])
                return None
            if key in confirmed_roots:
                return key
            parent = parent_by_source.get(key)
            if parent is None:
                return None
            return root_for(parent, (*path, key))

        resolved_root_by_key = {
            row.ref.key: root_for(row.ref.key)
            for row in rows
            if row.ref.key in parent_by_source
        }
        confirmed_roots.difference_update(cycle_keys)
        resolved_root_by_key = {
            key: root
            for key, root in resolved_root_by_key.items()
            if root is not None and root not in cycle_keys
        }

        dispositions: list[ResolvedPhysicalDisposition] = []
        for row in rows:
            key = row.ref.key
            assessment = role_by_key.get(key)
            confirmed_relation = confirmed_relation_by_source.get(key)
            root_key = resolved_root_by_key.get(key)
            common_kwargs = {
                "physical_row_ref": row.ref,
                "evidence_score": assessment.evidence_score if assessment else 0.0,
                "evidence_strength": assessment.evidence_strength if assessment else "none",
                "evidence": assessment.evidence if assessment else ("physical_row_present",),
                "contradictions": assessment.contradictions if assessment else (),
                "provenance": {"source": "GlobalRowSemanticsResolver"},
            }
            if confirmed_relation is not None and root_key is not None and key not in cycle_keys:
                dispositions.append(
                    ResolvedPhysicalDisposition(
                        **common_kwargs,
                        role=RowRole.CONTINUATION,
                        qualifier=None,
                        validation_state=DispositionValidation.VALIDATED,
                        role_state=RowRoleState.CONFIRMED,
                        logical_item_id=f"item:{root_key}",
                        relation=confirmed_relation,
                        reasons=("confirmed_continuation_relation",),
                    )
                )
                continue
            if key in confirmed_roots:
                dispositions.append(
                    ResolvedPhysicalDisposition(
                        **common_kwargs,
                        role=RowRole.ITEM_ROOT,
                        qualifier=None,
                        validation_state=DispositionValidation.VALIDATED,
                        role_state=RowRoleState.CONFIRMED,
                        logical_item_id=f"item:{key}",
                        reasons=("confirmed_independent_item_root",),
                    )
                )
                continue

            selected_role = assessment.selected_role if assessment else None
            selected_qualifier = assessment.selected_qualifier if assessment else None
            state = assessment.state if assessment else RowRoleState.UNRESOLVED
            reasons = list(assessment.reasons if assessment else ())
            relation_candidates = relation_by_source.get(key, ())
            if any(_relation_is_plausible(relation) for relation in relation_candidates):
                state = RowRoleState.UNRESOLVED
                reasons.append("continuation_relation_unresolved")
            if key in cycle_keys:
                state = RowRoleState.UNRESOLVED
                reasons.append("relation_cycle")
            disposition_role = selected_role or RowRole.UNKNOWN
            validation = (
                DispositionValidation.VALIDATED
                if selected_role is not None
                and state == RowRoleState.CONFIRMED
                and selected_role != RowRole.ITEM_ROOT
                else DispositionValidation.UNVALIDATED
            )
            dispositions.append(
                ResolvedPhysicalDisposition(
                    **common_kwargs,
                    role=disposition_role,
                    qualifier=selected_qualifier,
                    validation_state=validation,
                    role_state=state,
                    logical_item_id=None,
                    reasons=tuple(reasons) or ("semantic_role_unresolved",),
                )
            )

        physical_by_key = {row.ref.key: row for row in rows}
        logical_items: list[LogicalSpecificationItem] = []
        for root_key in sorted(confirmed_roots):
            member_keys = [
                key
                for key, member_root in resolved_root_by_key.items()
                if member_root == root_key
            ]
            member_keys = [root_key, *sorted(member_keys, key=lambda key: physical_by_key[key].ref.row_index)]
            unique_keys = tuple(dict.fromkeys(member_keys))
            member_rows = tuple(physical_by_key[key] for key in unique_keys if key in physical_by_key)
            fragments_by_field: dict[str, list[SourceFieldFragment]] = defaultdict(list)
            for member_row in member_rows:
                for fragment in _field_fragments(member_row, mapping):
                    fragments_by_field[fragment.field].append(fragment)
            fields: dict[str, LogicalFieldValue] = {}
            for field_name, fragments in fragments_by_field.items():
                ordered = tuple(fragments)
                canonical_text = (
                    ordered[0].text.strip()
                    if field_name in _CRITICAL_FIELDS and len(ordered) > 1
                    else _join_fragments(ordered)
                )
                review_reasons = (
                    ("multiple_critical_source_fragments",)
                    if field_name in _CRITICAL_FIELDS and len(ordered) > 1
                    else ()
                )
                fields[field_name] = LogicalFieldValue(
                    field=field_name,
                    canonical_text=canonical_text,
                    source_fragments=ordered,
                    origin=FieldOrigin.OCR,
                    review_reasons=review_reasons,
                    provenance={"source": "physical_source_fragments"},
                )
            logical_items.append(
                LogicalSpecificationItem(
                    logical_id=f"item:{root_key}",
                    physical_row_refs=tuple(row.ref for row in member_rows),
                    fields=fields,
                    bbox=_union_bbox(member_rows),
                    provenance={
                        "source": "GlobalRowSemanticsResolver",
                        "root_physical_row": root_key,
                    },
                )
            )

        conservation = evaluate_semantic_conservation(
            rows,
            dispositions,
            logical_items=logical_items,
            relations=relation_tuple,
        )
        item_candidate_rows = {
            assessment.physical_row_ref.key
            for assessment in functional_graph.row_role_assessments
            if assessment.selected_role == RowRole.ITEM_ROOT
        }
        resolved_item_rows = {
            disposition.physical_row_ref.key
            for disposition in dispositions
            if disposition.validated
            and disposition.role in {RowRole.ITEM_ROOT, RowRole.CONTINUATION}
        }
        metrics = {
            "physical_nonempty_rows": sum(row.nonempty for row in rows),
            "resolved_physical_rows": conservation.resolved_physical_rows,
            "unresolved_physical_rows": len(conservation.unresolved_rows),
            "confirmed_header_rows": sum(
                disposition.validated and disposition.role == RowRole.HEADER
                for disposition in dispositions
            ),
            "confirmed_service_rows": sum(
                disposition.validated and disposition.role == RowRole.SERVICE
                for disposition in dispositions
            ),
            "confirmed_item_root_rows": sum(
                disposition.validated and disposition.role == RowRole.ITEM_ROOT
                for disposition in dispositions
            ),
            "confirmed_continuation_rows": sum(
                disposition.validated and disposition.role == RowRole.CONTINUATION
                for disposition in dispositions
            ),
            "confirmed_context_rows": sum(
                disposition.validated and disposition.role == RowRole.CONTEXT
                for disposition in dispositions
            ),
            "confirmed_component_rows": sum(
                disposition.validated and disposition.role == RowRole.COMPONENT
                for disposition in dispositions
            ),
            "confirmed_note_rows": sum(
                disposition.validated and disposition.role == RowRole.NOTE
                for disposition in dispositions
            ),
            "logical_item_count": len(logical_items),
            "relation_candidate_count": len(relation_tuple),
            "confirmed_relation_count": sum(
                relation.state == RowRelationState.CONFIRMED
                for relation in relation_tuple
            ),
            "ambiguous_relation_count": sum(
                relation.state in {RowRelationState.AMBIGUOUS, RowRelationState.UNRESOLVED}
                for relation in relation_tuple
            ),
            "semantic_conservation_rate": conservation.semantic_conservation_rate,
            "semantic_conservation_pass": conservation.semantic_conservation_pass,
            "auto_resolved_row_rate": (
                conservation.resolved_physical_rows / conservation.total_physical_rows
                if conservation.total_physical_rows
                else 1.0
            ),
            "item_auto_resolution_rate": (
                len(resolved_item_rows) / len(item_candidate_rows)
                if item_candidate_rows
                else 1.0
            ),
            "role_conflict_count": sum(
                assessment.state != RowRoleState.CONFIRMED
                for assessment in functional_graph.row_role_assessments
            ),
            "relation_conflict_count": relation_conflicts + len(cycle_keys),
        }
        diagnostics = {
            "resolver": "GlobalRowSemanticsResolver",
            "shadow_only": True,
            "metrics": metrics,
            "unresolved_physical_row_refs": [
                ref.as_dict() for ref in conservation.unresolved_rows
            ],
            "relation_conflict_sources": sorted(invalid_relation_keys),
            "cycle_row_keys": sorted(cycle_keys),
            "critical_values_projected_from_source_only": True,
            "continuation_numeric_composition": False,
        }
        return SemanticTableIR(
            analysis_context=context,
            row_roles=functional_graph.row_role_assessments,
            relations=relation_tuple,
            dispositions=tuple(dispositions),
            logical_items=tuple(logical_items),
            diagnostics=diagnostics,
            conservation=conservation,
        )


__all__ = ["GlobalRowSemanticsResolver"]
