"""Offline contract tests for the Stage 7.1.2A IR foundation."""

from __future__ import annotations

import json

import pytest

from averon_import.services.ocr.physical_evidence import PhysicalEvidenceSnapshot
from averon_import.services.ocr.semantics.row_evidence import (
    RoleCandidate,
    RowRelationAssessment,
    RowRelationState,
    RowQualifier,
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
    ValueCandidate,
    evaluate_semantic_conservation,
)
from averon_import.services.ocr.table_ir import (
    PhysicalCellIR,
    PhysicalCellRef,
    PhysicalRowFragmentIR,
    PhysicalRowIR,
    PhysicalRowRef,
    PhysicalTableIR,
    PhysicalTableRef,
    PhysicalWordIR,
    PhysicalWordRef,
)


def _table() -> tuple[PhysicalTableIR, tuple[PhysicalRowRef, ...]]:
    table_ref = PhysicalTableRef(page_number=7, table_index=0, document_id="fixture", detector_source="raster")
    rows = []
    refs = []
    words = []
    for row_index, text in enumerate(("root", "continuation", "note")):
        row_ref = PhysicalRowRef(table_ref, row_index)
        cell_ref = PhysicalCellRef(row_ref, 0, row_index)
        word_ref = PhysicalWordRef(table_ref, row_index)
        refs.append(row_ref)
        words.append(PhysicalWordIR(word_ref, text, (10, row_index * 10, 40, row_index * 10 + 8)))
        rows.append(
            PhysicalRowIR(
                row_ref,
                (0, row_index * 10, 100, row_index * 10 + 10),
                cells=(
                    PhysicalCellIR(
                        cell_ref,
                        (0, row_index * 10, 100, row_index * 10 + 10),
                        raw_text=text,
                        word_refs=(word_ref,),
                    ),
                ),
                raster_nonempty=True,
            )
        )
    return (
        PhysicalTableIR(
            ref=table_ref,
            bounds=(0, 0, 100, 40),
            x_boundaries=(0, 100),
            y_boundaries=(0, 10, 20, 30, 40),
            rows=tuple(rows),
            words=tuple(words),
            assigned_word_refs={refs[0].key: (words[0].ref,)},
            grid_source="raster",
            grid_evidence={"nested": {"confidence": 0.99}},
            structural_evidence={"critical_boundary_conflicts": []},
            raster_witness={"nonempty_rows": [0, 1, 2]},
            provenance={"source": "offline-test"},
        ),
        tuple(refs),
    )


def _item(row_ref: PhysicalRowRef, logical_id: str = "item-1", role: RowRole = RowRole.ITEM_ROOT):
    return ResolvedPhysicalDisposition(
        physical_row_ref=row_ref,
        role=role,
        validation_state=DispositionValidation.VALIDATED,
        role_state=RowRoleState.CONFIRMED,
        logical_item_id=logical_id if role in {RowRole.ITEM_ROOT, RowRole.CONTINUATION} else None,
        evidence=("explicit offline evidence",),
    )


def test_physical_ir_defensively_freezes_nested_inputs_and_roundtrips_deterministically():
    table, refs = _table()
    assert table.grid_evidence["nested"]["confidence"] == 0.99
    with pytest.raises(TypeError):
        table.grid_evidence["nested"]["confidence"] = 0.1

    encoded = json.dumps(table.as_dict(), ensure_ascii=False, sort_keys=True)
    restored = PhysicalTableIR.from_dict(json.loads(encoded))
    assert restored == table
    assert json.dumps(restored.as_dict(), ensure_ascii=False, sort_keys=True) == encoded
    assert restored.row(1).ref == refs[1]


def test_physical_refs_fragments_and_word_assignments_keep_stable_identity():
    table, refs = _table()
    fragment = PhysicalRowFragmentIR(refs[1], 0, (0, 10, 50, 20), cells=table.rows[1].cells)
    assert fragment.row_ref == refs[1]
    assert fragment.parent_row_ref != PhysicalRowRef(table.ref, 3)
    assert table.assigned_word_refs[refs[0].key][0].source_word_index == 0


def test_compatibility_snapshot_retains_typed_table_and_historical_shape():
    table, _ = _table()
    snapshot = PhysicalEvidenceSnapshot.from_physical_table_ir(table)
    assert snapshot.to_physical_table_ir() == table
    assert snapshot.selected_table_bounds["width"] == 100.0
    assert snapshot.physical_rows[0]["source_row_index"] == 0
    assert snapshot.assigned_word_refs[next(iter(snapshot.assigned_word_refs))] == (0,)
    assert "physical_table_ir" not in snapshot.as_dict()


def test_row_evidence_is_dto_only_and_immutable():
    table, refs = _table()
    assessment = RowRoleAssessment(
        refs[0],
        candidates=(
            RoleCandidate("ITEM_ROOT"),
        ),
        selected_role="ITEM_ROOT",
        state="CONFIRMED",
        evidence=("bounded row evidence",),
    )
    assert assessment.selected_role == RowRole.ITEM_ROOT


def test_ocr_value_candidate_cannot_become_auto_trusted():
    with pytest.raises(ValueError):
        ValueCandidate("1", origin=FieldOrigin.OCR, auto_trusted=True)


def test_conservation_valid_item_continuation_and_explicit_note():
    table, refs = _table()
    relation = RowRelationAssessment(
        source_row_ref=refs[1],
        target_row_ref=refs[0],
        state=RowRelationState.CONFIRMED,
        evidence=("same physical item",),
    )
    dispositions = (
        _item(refs[0]),
        ResolvedPhysicalDisposition(
            refs[1],
            role=RowRole.CONTINUATION,
            validation_state=DispositionValidation.VALIDATED,
            role_state=RowRoleState.CONFIRMED,
            logical_item_id="item-1",
            relation=relation,
            evidence=("confirmed continuation",),
        ),
        ResolvedPhysicalDisposition(
            refs[2],
            role=RowRole.NOTE,
            validation_state=DispositionValidation.VALIDATED,
            role_state=RowRoleState.CONFIRMED,
            evidence=("explicit note evidence",),
        ),
    )
    report = evaluate_semantic_conservation(
        table.rows,
        dispositions,
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
        relations=(relation,),
    )
    assert report.semantic_conservation_pass is True
    assert report.semantic_conservation_rate == 1.0
    assert report.continuation_count == 1


@pytest.mark.parametrize(
    ("disposition_factory", "reason"),
    [
        (lambda refs: (_item(refs[0]), _item(refs[0])), "MULTIPLE_DISPOSITIONS"),
        (lambda refs: (_item(refs[0]),), "UNACCOUNTED"),
        (
            lambda refs: (
                ResolvedPhysicalDisposition(refs[0], role=RowRole.UNKNOWN, evidence=("unknown",)),
                _item(refs[1]),
                _item(refs[2], role=RowRole.NOTE),
            ),
            "UNKNOWN",
        ),
        (
            lambda refs: (
                ResolvedPhysicalDisposition(refs[0], role=RowRole.ITEM_ROOT, evidence=("weak",)),
                _item(refs[1]),
                _item(refs[2], role=RowRole.NOTE),
            ),
            "UNRESOLVED",
        ),
    ],
)
def test_conservation_fail_closed_for_missing_duplicate_unknown_or_unresolved(disposition_factory, reason):
    table, refs = _table()
    report = evaluate_semantic_conservation(
        table.rows,
        disposition_factory(refs),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1], refs[2])),),
    )
    assert report.semantic_conservation_pass is False
    assert reason in report.reasons


def test_continuation_without_parent_or_with_multiple_roots_is_not_validated():
    table, refs = _table()
    relation_a = RowRelationAssessment(refs[1], refs[0], state=RowRelationState.CONFIRMED)
    relation_b = RowRelationAssessment(refs[1], refs[2], state=RowRelationState.CONFIRMED)
    continuation = ResolvedPhysicalDisposition(
        refs[1],
        role=RowRole.CONTINUATION,
        validation_state=DispositionValidation.VALIDATED,
        role_state=RowRoleState.CONFIRMED,
        logical_item_id="item-1",
        relation=relation_a,
        evidence=("ambiguous continuation",),
    )
    report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), continuation, _item(refs[2], role=RowRole.NOTE)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
        relations=(relation_a, relation_b),
    )
    assert report.semantic_conservation_pass is False
    assert "RELATION_CONFLICT" in report.reasons

    no_parent = ResolvedPhysicalDisposition(
        refs[1],
        role=RowRole.CONTINUATION,
        validation_state=DispositionValidation.VALIDATED,
        role_state=RowRoleState.CONFIRMED,
        logical_item_id="item-1",
        evidence=("continuation without relation",),
    )
    report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), no_parent, _item(refs[2], role=RowRole.NOTE)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
    )
    assert report.semantic_conservation_pass is False
    assert "ORPHAN" in report.reasons


def test_semantic_table_roundtrip_preserves_fragments_provenance_and_candidate_only_value():
    table, refs = _table()
    context = TableAnalysisContext.from_assessments(
        table,
        header_mapping={"status": "trusted"},
        family_assessment={"family": "SUPPORTED_SPECIFICATION"},
    )
    fragment = SourceFieldFragment(
        field="quantity",
        text="1",
        raw_text="1О",
        physical_row_ref=refs[0],
        physical_cell_ref=table.rows[0].cells[0].ref,
        word_refs=(table.words[0].ref,),
        bbox=table.rows[0].cells[0].bbox,
        provenance={"pass": "primary"},
    )
    logical_item = LogicalSpecificationItem(
        "item-1",
        physical_row_refs=(refs[0], refs[1]),
        fields={
            "quantity": LogicalFieldValue(
                "quantity",
                canonical_text=None,
                source_fragments=(fragment,),
                candidates=(ValueCandidate("1", review_reason="suspected OCR ambiguity"),),
                review_reasons=("candidate_only",),
            )
        },
        provenance={"source": "shadow"},
    )
    ir = SemanticTableIR.from_parts(
        context,
        row_roles=(
            RowRoleAssessment(
                refs[0],
                candidates=(RoleCandidate(RowRole.ITEM_ROOT),),
                selected_role=RowRole.ITEM_ROOT,
                state=RowRoleState.CONFIRMED,
            ),
        ),
        logical_items=(logical_item,),
        dispositions=(_item(refs[0]),),
        diagnostics={"mode": "shadow"},
    )
    payload = ir.as_dict()
    assert payload["logical_items"][0]["fields"]["quantity"]["candidates"][0]["auto_trusted"] is False
    assert payload["logical_items"][0]["fields"]["quantity"]["canonical_text"] is None
    assert payload["conservation"]["semantic_conservation_pass"] is False
    assert payload["logical_items"][0]["provenance"]["source"] == "shadow"


def test_ref_keys_include_all_table_identity_fields():
    first = PhysicalTableRef(7, 0, "fixture", "raster")
    second_table = PhysicalTableRef(7, 1, "fixture", "raster")
    second_detector = PhysicalTableRef(7, 0, "fixture", "yandex")
    first_word = PhysicalWordRef(first, 3)
    second_word = PhysicalWordRef(second_table, 3)
    third_word = PhysicalWordRef(second_detector, 3)
    assert first.key != second_table.key
    assert first.key != second_detector.key
    assert PhysicalRowRef(first, 0).key != PhysicalRowRef(second_table, 0).key
    assert first_word.key != second_word.key
    assert first_word.key != third_word.key
    assert PhysicalCellRef(PhysicalRowRef(first, 0), 1).key != PhysicalCellRef(
        PhysicalRowRef(second_detector, 0), 1
    ).key


def test_physical_table_ir_rejects_malformed_internal_references():
    table, refs = _table()
    foreign_table = PhysicalTableRef(7, 0, "fixture", "other-detector")
    foreign_row = PhysicalRowIR(PhysicalRowRef(foreign_table, 0), (0, 0, 10, 10))
    with pytest.raises(ValueError, match="row reference"):
        PhysicalTableIR(table.ref, table.bounds, rows=(foreign_row,))

    with pytest.raises(ValueError, match="duplicate physical row"):
        PhysicalTableIR(table.ref, table.bounds, rows=(table.rows[0], table.rows[0]))

    unrelated_cell = PhysicalCellIR(
        PhysicalCellRef(PhysicalRowRef(foreign_table, 0), 0),
        (0, 0, 10, 10),
        raw_text="foreign",
    )
    malformed_row = PhysicalRowIR(refs[0], table.rows[0].bbox, cells=(unrelated_cell,))
    with pytest.raises(ValueError, match="cell reference"):
        PhysicalTableIR(table.ref, table.bounds, rows=(malformed_row,))

    with pytest.raises(ValueError, match="assigned word"):
        PhysicalTableIR(
            table.ref,
            table.bounds,
            assigned_word_refs={"cell": (PhysicalWordRef(table.ref, 999),)},
        )

    with pytest.raises(ValueError, match="strictly increasing"):
        PhysicalTableIR(table.ref, table.bounds, x_boundaries=(0, 10, 10))
    with pytest.raises(ValueError, match="non-negative"):
        PhysicalTableIR(table.ref, (-1, 0, 10, 10))

    foreign_fragment = PhysicalRowFragmentIR(
        PhysicalRowRef(foreign_table, 0),
        0,
        (0, 0, 10, 10),
    )
    with pytest.raises(ValueError, match="fragment parent"):
        PhysicalTableIR(table.ref, table.bounds, row_fragments=(foreign_fragment,))


def _continuation(
    source: PhysicalRowRef,
    target: PhysicalRowRef,
    logical_id: str = "item-1",
) -> tuple[ResolvedPhysicalDisposition, RowRelationAssessment]:
    relation = RowRelationAssessment(
        source_row_ref=source,
        target_row_ref=target,
        state=RowRelationState.CONFIRMED,
        evidence=("explicit parent relation",),
    )
    return (
        ResolvedPhysicalDisposition(
            source,
            role=RowRole.CONTINUATION,
            validation_state=DispositionValidation.VALIDATED,
            role_state=RowRoleState.CONFIRMED,
            logical_item_id=logical_id,
            relation=relation,
            evidence=("explicit continuation",),
        ),
        relation,
    )


def test_conservation_validates_continuation_chain_to_one_root():
    table, refs = _table()
    middle, middle_relation = _continuation(refs[1], refs[0])
    tail, tail_relation = _continuation(refs[2], refs[1])
    report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), middle, tail),
        logical_items=(LogicalSpecificationItem("item-1", refs),),
        relations=(middle_relation, tail_relation),
    )
    assert report.semantic_conservation_pass is True


def test_conservation_rejects_cycle_and_chain_without_root():
    table, refs = _table()
    first, first_relation = _continuation(refs[0], refs[1])
    second, second_relation = _continuation(refs[1], refs[0])
    cycle_report = evaluate_semantic_conservation(
        table.rows,
        (first, second, _item(refs[2], role=RowRole.NOTE)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
        relations=(first_relation, second_relation),
    )
    assert cycle_report.semantic_conservation_pass is False
    assert "RELATION_CYCLE" in cycle_report.reasons

    tail, tail_relation = _continuation(refs[1], refs[2])
    no_root = ResolvedPhysicalDisposition(
        refs[2],
        role=RowRole.CONTINUATION,
        validation_state=DispositionValidation.VALIDATED,
        role_state=RowRoleState.CONFIRMED,
        logical_item_id="item-1",
        evidence=("no root",),
    )
    no_root_report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), tail, no_root),
        logical_items=(LogicalSpecificationItem("item-1", refs),),
        relations=(tail_relation,),
    )
    assert no_root_report.semantic_conservation_pass is False
    assert "ORPHAN" in no_root_report.reasons


def test_conservation_rejects_bad_target_relation_and_disagreement():
    table, refs = _table()
    continuation, attached = _continuation(refs[1], refs[2])
    target_note = _item(refs[2], role=RowRole.NOTE)
    report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), continuation, target_note),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
        relations=(attached,),
    )
    assert report.semantic_conservation_pass is False
    assert "ROOT_CONFLICT" in report.reasons

    wrong_authoritative = RowRelationAssessment(
        source_row_ref=refs[1],
        target_row_ref=refs[0],
        state=RowRelationState.CONFIRMED,
    )
    mismatch = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), continuation, target_note),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1])),),
        relations=(wrong_authoritative,),
    )
    assert mismatch.semantic_conservation_pass is False
    assert "RELATION_CONFLICT" in mismatch.reasons


def test_conservation_reports_foreign_disposition_relation_and_item_refs():
    table, refs = _table()
    foreign = PhysicalRowRef(PhysicalTableRef(7, 0, "fixture", "foreign"), 99)
    disposition_report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), _item(refs[1]), _item(refs[2], role=RowRole.NOTE), _item(foreign)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1], refs[2])),),
    )
    assert disposition_report.semantic_conservation_pass is False
    assert foreign in disposition_report.invalid_physical_rows
    assert "FOREIGN_PHYSICAL_REF" in disposition_report.reasons

    foreign_relation = RowRelationAssessment(refs[1], foreign, state=RowRelationState.CONFIRMED)
    relation_report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), _item(refs[1]), _item(refs[2], role=RowRole.NOTE)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], refs[1], refs[2])),),
        relations=(foreign_relation,),
    )
    assert relation_report.semantic_conservation_pass is False
    assert foreign in relation_report.invalid_physical_rows

    item_report = evaluate_semantic_conservation(
        table.rows,
        (_item(refs[0]), _item(refs[1]), _item(refs[2], role=RowRole.NOTE)),
        logical_items=(LogicalSpecificationItem("item-1", (refs[0], foreign)),),
    )
    assert item_report.semantic_conservation_pass is False
    assert foreign in item_report.invalid_physical_rows


def test_logical_field_canonical_provenance_contract():
    table, refs = _table()
    with pytest.raises(ValueError, match="source fragment"):
        LogicalFieldValue("quantity", canonical_text="1", origin=FieldOrigin.OCR)

    fragment = SourceFieldFragment("quantity", "1", refs[0], table.rows[0].cells[0].ref)
    accepted = LogicalFieldValue("quantity", canonical_text="1", source_fragments=(fragment,))
    assert accepted.canonical_text == "1"

    with pytest.raises(ValueError, match="field"):
        LogicalSpecificationItem("item-1", (refs[0],), fields={"name": accepted})
    with pytest.raises(ValueError, match="source fragment field"):
        LogicalFieldValue(
            "quantity",
            canonical_text="1",
            source_fragments=(SourceFieldFragment("name", "1", refs[0]),),
        )
    with pytest.raises(ValueError, match="belong to the logical item"):
        LogicalSpecificationItem(
            "item-1",
            (refs[0],),
            fields={
                "quantity": LogicalFieldValue(
                    "quantity",
                    canonical_text="1",
                    source_fragments=(SourceFieldFragment("quantity", "1", refs[1]),),
                )
            },
        )

    assert LogicalFieldValue("quantity", canonical_text="1", origin=FieldOrigin.HUMAN).origin == FieldOrigin.HUMAN
    assert LogicalFieldValue(
        "quantity", canonical_text="1", origin=FieldOrigin.TRUSTED_RULE
    ).origin == FieldOrigin.TRUSTED_RULE


def test_row_qualifier_contract_does_not_hide_ambiguous_same_role_candidates():
    assert {
        RowQualifier.COLUMN_HEADER,
        RowQualifier.REPEATED_HEADER,
        RowQualifier.NUMBERING_BAND,
        RowQualifier.TITLE_BLOCK,
        RowQualifier.SECTION,
        RowQualifier.SYSTEM,
    }.issubset(set(RowQualifier))
    _, refs = _table()
    ambiguous = RowRoleAssessment(
        refs[0],
        candidates=(
            RoleCandidate(RowRole.HEADER, qualifier=RowQualifier.COLUMN_HEADER),
            RoleCandidate(RowRole.HEADER, qualifier=RowQualifier.REPEATED_HEADER),
        ),
        selected_role=RowRole.HEADER,
        state=RowRoleState.AMBIGUOUS,
    )
    assert ambiguous.selected is None
    selected = RowRoleAssessment(
        refs[0],
        candidates=ambiguous.candidates,
        selected_role=RowRole.HEADER,
        selected_qualifier=RowQualifier.REPEATED_HEADER,
        state=RowRoleState.CONFIRMED,
    )
    assert selected.selected.qualifier == RowQualifier.REPEATED_HEADER.value
    with pytest.raises(ValueError, match="exactly one candidate"):
        RowRoleAssessment(
            refs[0],
            selected_role=RowRole.HEADER,
            state=RowRoleState.CONFIRMED,
        )
