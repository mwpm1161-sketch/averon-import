"""Offline, provider-neutral Stage 7.1.2C relation/resolver tests."""

from __future__ import annotations

from averon_import.services.ocr.semantics.functional_analyzer import TableFunctionalAnalyzer
from averon_import.services.ocr.semantics.row_evidence import (
    RowRelationAssessment,
    RowRelationState,
    RowRole,
    RowRoleState,
)
from averon_import.services.ocr.semantics.row_relation_analyzer import RowRelationAnalyzer
from averon_import.services.ocr.semantics.row_semantics_resolver import GlobalRowSemanticsResolver
from averon_import.services.ocr.semantics.semantic_table import TableAnalysisContext
from averon_import.services.ocr.physical_grid import PhysicalGrid, PhysicalGridCell
from averon_import.services.ocr.reconstruction import reconstruct_page_rows
from averon_import.services.ocr.table_ir import (
    PhysicalCellIR,
    PhysicalCellRef,
    PhysicalRowIR,
    PhysicalRowRef,
    PhysicalTableIR,
    PhysicalTableRef,
)


_FIELDS = ("position", "name", "type_mark", "code", "manufacturer", "unit", "quantity", "mass", "note")
_HEADER = {
    0: "Позиция",
    1: "Наименование",
    2: "Тип, марка",
    3: "Код",
    4: "Изготовитель",
    5: "Ед.",
    6: "Количество",
    7: "Масса",
    8: "Примечание",
}
_MAPPING = {str(index): [field] for index, field in enumerate(_FIELDS)}


def _table(
    body: dict[int, dict[int, str]],
    *,
    page_number: int = 1,
    detector_source: str = "raster",
    header_rows: tuple[int, ...] = (0,),
    structural_evidence: dict | None = None,
) -> tuple[PhysicalTableIR, TableAnalysisContext]:
    table_ref = PhysicalTableRef(
        page_number=page_number,
        table_index=0,
        document_id="sanitized-fixture",
        detector_source=detector_source,
    )
    row_indexes = (0, *sorted(body))
    max_row = max(row_indexes, default=0)
    rows: list[PhysicalRowIR] = []
    for row_index in row_indexes:
        values = _HEADER if row_index == 0 else body[row_index]
        row_ref = PhysicalRowRef(table_ref, row_index)
        cells = tuple(
            PhysicalCellIR(
                ref=PhysicalCellRef(row_ref, column_index, row_index * 100 + column_index),
                bbox=(column_index * 100.0, row_index * 20.0,
                      column_index * 100.0 + 100.0, row_index * 20.0 + 20.0),
                raw_text=text,
            )
            for column_index, text in sorted(values.items())
            if text
        )
        rows.append(
            PhysicalRowIR(
                ref=row_ref,
                bbox=(0.0, row_index * 20.0, len(_FIELDS) * 100.0, row_index * 20.0 + 20.0),
                cells=cells,
                raster_nonempty=True,
            )
        )
    table = PhysicalTableIR(
        ref=table_ref,
        bounds=(0.0, 0.0, len(_FIELDS) * 100.0, (max_row + 2) * 20.0),
        x_boundaries=tuple(index * 100.0 for index in range(len(_FIELDS) + 1)),
        y_boundaries=tuple(index * 20.0 for index in range(max_row + 3)),
        rows=tuple(rows),
        grid_source="sanitized-raster",
        structural_evidence=structural_evidence or {},
        raster_witness={"nonempty_rows": list(row_indexes)},
    )
    context = TableAnalysisContext(
        physical_table=table,
        header_mapping={
            "mapping_status": "trusted",
            "header_candidate_rows": list(header_rows),
            "selected_mapping": _MAPPING,
        },
        schema_assessment={"status": "supported", "variant": "sanitized"},
        structural_evidence=structural_evidence or {},
    )
    return table, context


def _run(
    body: dict[int, dict[int, str]],
    **kwargs,
):
    table, context = _table(body, **kwargs)
    graph = TableFunctionalAnalyzer().analyze(table, context)
    relations = RowRelationAnalyzer().analyze(table, context, graph)
    semantic = GlobalRowSemanticsResolver().resolve(table, context, graph, relations)
    return table, context, graph, relations, semantic


def _relation(relations, row_index: int):
    return next(item for item in relations if item.source_row_ref.row_index == row_index)


def _disposition(semantic, row_index: int):
    return next(item for item in semantic.dispositions if item.physical_row_ref.row_index == row_index)


def _grid(row_count: int = 3, column_count: int = 9) -> PhysicalGrid:
    xs = tuple(index / column_count for index in range(column_count + 1))
    ys = tuple(index / row_count for index in range(row_count + 1))
    return PhysicalGrid(
        source="stage712c-test-grid",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=tuple(
            PhysicalGridCell(row, column, (xs[column], ys[row], xs[column + 1], ys[row + 1]))
            for row in range(row_count)
            for column in range(column_count)
        ),
        confidence=1.0,
        high_confidence=True,
    )


def _geometry_payload() -> dict:
    header = tuple(_HEADER[index] for index in range(len(_FIELDS)))
    body = (
        ((1, "Первый насос"), (5, "шт"), (6, "1")),
        ((1, "Второй насос"), (5, "шт"), (6, "2")),
    )
    words = []
    for row, values in enumerate((tuple(enumerate(header)), *body)):
        for column, text in values:
            left = (column + 0.12) * 900.0 / len(_FIELDS)
            top = (row + 0.12) * 900.0 / 3.0
            words.append(
                {
                    "text": text,
                    "boundingBox": {"vertices": [
                        {"x": left, "y": top},
                        {"x": left + 24.0, "y": top},
                        {"x": left + 24.0, "y": top + 18.0},
                        {"x": left, "y": top + 18.0},
                    ]},
                }
            )
    return {
        "page": {"width": 900, "height": 900},
        "textAnnotation": {"blocks": [{"lines": [{"words": words}]}]},
    }


def _parent():
    return {1: {1: "Насос НЦ-50", 2: "НЦ-50", 3: "N2", 4: "ООО «Регент-", 5: "шт", 6: "87"}}


def test_p28_name_and_manufacturer_fragments_resolve_to_one_logical_item():
    body = {
        **_parent(),
        2: {1: "для установки", 4: "Нахимовский»"},
    }
    _table_ir, _context, _graph, relations, semantic = _run(body)
    relation = _relation(relations, 2)
    assert relation.state == RowRelationState.CONFIRMED
    assert relation.target_row_ref.row_index == 1
    assert _disposition(semantic, 2).role == RowRole.CONTINUATION
    assert _disposition(semantic, 2).validated
    assert len(semantic.logical_items) == 1
    item = semantic.logical_items[0]
    assert [ref.row_index for ref in item.physical_row_refs] == [1, 2]
    assert item.fields["name"].canonical_text == "Насос НЦ-50 для установки"
    assert item.fields["manufacturer"].canonical_text == "ООО «Регент-Нахимовский»"
    assert item.fields["unit"].canonical_text == "шт"
    assert item.fields["quantity"].canonical_text == "87"
    assert len(item.fields["name"].source_fragments) == 2
    assert len(item.fields["manufacturer"].source_fragments) == 2
    assert semantic.diagnostics["continuation_numeric_composition"] is False
    assert semantic.conservation.semantic_conservation_pass


def test_geometry_first_production_shadow_wires_relation_and_semantic_namespaces():
    diagnostics: dict = {}
    rows = reconstruct_page_rows(
        _geometry_payload(),
        "yandex_vision",
        physical_grid=_grid(),
        reconstruction_mode="geometry",
        diagnostics=diagnostics,
    )
    assert diagnostics["selected_mode"] == "geometry_first"
    assert diagnostics["schema"]["status"] == "supported"
    assert [row.values["name"] for row in rows] == ["Первый насос", "Второй насос"]
    shadow = diagnostics["functional_semantics_shadow"]
    assert shadow["functional_graph"]["row_role_assessments"]
    assert "relations" in shadow["relation_graph"]
    assert "logical_items" in shadow["semantic_table"]
    assert shadow["metrics"]["logical_item_count"] == 2
    assert shadow["metrics"]["confirmed_relation_count"] == 0


def test_identity_only_name_type_or_code_is_not_auto_continuation():
    for column in (1, 2, 3):
        _table_ir, _context, _graph, relations, semantic = _run(
            {**_parent(), 2: {column: "фрагмент"}}
        )
        relation = _relation(relations, 2)
        assert relation.state == RowRelationState.AMBIGUOUS
        assert _disposition(semantic, 2).logical_item_id is None
        assert "continuation_relation_unresolved" in _disposition(semantic, 2).reasons
        assert not semantic.conservation.semantic_conservation_pass


def test_hyphenated_manufacturer_fragment_is_strong_continuation_evidence():
    _table_ir, _context, _graph, relations, semantic = _run(
        {**_parent(), 2: {4: "Нахимовский»"}}
    )
    assert _relation(relations, 2).state == RowRelationState.CONFIRMED
    assert _disposition(semantic, 2).role == RowRole.CONTINUATION


def test_new_critical_or_position_identity_evidence_blocks_continuation():
    cases = (
        ({2: {6: "2"}}, False),
        ({2: {5: "шт"}}, False),
        ({2: {7: "4"}}, False),
        ({2: {0: "2", 1: "Новый насос"}}, True),
        ({2: {1: "Новый насос", 6: "2"}}, True),
    )
    for addition, independent_root in cases:
        _table_ir, _context, _graph, relations, semantic = _run({**_parent(), **addition})
        relation = _relation(relations, 2)
        assert relation.state == RowRelationState.REJECTED
        assert relation.target_row_ref is None
        assert _disposition(semantic, 2).role != RowRole.CONTINUATION
        # Independent critical evidence may remain a local candidate, but it
        # is never attached to the preceding item.
        if independent_root:
            assert _disposition(semantic, 2).role == RowRole.ITEM_ROOT
            assert _disposition(semantic, 2).logical_item_id is not None
        else:
            assert _disposition(semantic, 2).logical_item_id is None


def test_resolver_rejects_manually_confirmed_continuation_with_critical_source():
    table, context, graph, _relations, _semantic = _run(
        {**_parent(), 2: {1: "другая позиция", 6: "2"}}
    )
    relation = RowRelationAssessment(
        source_row_ref=table.row(2).ref,
        target_row_ref=table.row(1).ref,
        state=RowRelationState.CONFIRMED,
        evidence_strength="STRONG",
        evidence=("invalid manual confirmation",),
    )
    semantic = GlobalRowSemanticsResolver().resolve(table, context, graph, (relation,))
    disposition = _disposition(semantic, 2)
    assert disposition.role != RowRole.CONTINUATION
    assert disposition.role == RowRole.ITEM_ROOT
    assert str(disposition.logical_item_id).endswith("|row:2")
    assert semantic.diagnostics["metrics"]["relation_conflict_count"] > 0


def test_two_sparse_independent_items_are_not_merged():
    body = {
        1: {1: "Первый насос", 5: "шт", 6: "1"},
        2: {1: "Второй насос", 5: "шт", 6: "2"},
    }
    _table_ir, _context, _graph, relations, semantic = _run(body)
    assert _relation(relations, 2).state == RowRelationState.REJECTED
    assert len(semantic.logical_items) == 2
    assert all(item.role != RowRole.CONTINUATION for item in semantic.dispositions)


def test_structural_material_conflict_blocks_relation_but_informational_mismatch_does_not():
    material = {**_parent(), 2: {1: "для установки", 4: "Нахимовский»"}}
    _table_ir, _context, _graph, relations, semantic = _run(
        material,
        structural_evidence={"critical_boundary_conflicts": ["unit_quantity"]},
    )
    assert _relation(relations, 2).state == RowRelationState.AMBIGUOUS
    assert _disposition(semantic, 2).logical_item_id is None
    assert not semantic.conservation.semantic_conservation_pass

    _table_ir, _context, _graph, relations, semantic = _run(
        material,
        structural_evidence={"provider_column_count_mismatch": True, "provider_bbox_edge_jitter": True},
    )
    assert _relation(relations, 2).state == RowRelationState.CONFIRMED
    assert _disposition(semantic, 2).role == RowRole.CONTINUATION


def test_relation_evidence_is_provider_neutral():
    body = {**_parent(), 2: {1: "для установки", 4: "Нахимовский»"}}
    first = _run(body, detector_source="raster")
    second = _run(body, detector_source="vector-prototype")
    first_relation = _relation(first[3], 2)
    second_relation = _relation(second[3], 2)
    assert first_relation.state == second_relation.state == RowRelationState.CONFIRMED
    assert first_relation.evidence == second_relation.evidence
    assert first_relation.reasons == second_relation.reasons
    assert first[4].diagnostics["resolver"] == second[4].diagnostics["resolver"]


def test_untrusted_mapping_disables_relation_inference_but_keeps_functional_analysis():
    table, context, graph, _relations, _semantic = _run(
        {**_parent(), 2: {1: "для установки", 4: "Нахимовский»"}}
    )
    untrusted = TableAnalysisContext(
        table,
        {
            "mapping_status": "ambiguous",
            "header_candidate_rows": [0],
            "selected_mapping": _MAPPING,
        },
    )
    relations = RowRelationAnalyzer().analyze(table, untrusted, graph)
    assert relations == ()


def test_page_start_and_repeated_header_do_not_continue_previous_table():
    _table_ir, _context, _graph, relations, semantic = _run(
        {1: {1: "Старт страницы", 5: "шт", 6: "1"}},
        page_number=2,
    )
    assert _relation(relations, 1).state == RowRelationState.REJECTED
    assert _disposition(semantic, 1).role == RowRole.ITEM_ROOT

    _table_ir, _context, _graph, relations, semantic = _run(
        {1: {1: "Первый насос", 5: "шт", 6: "1"}, 2: _HEADER, 3: {1: "Второй насос", 5: "шт", 6: "2"}},
        header_rows=(0, 2),
    )
    assert _relation(relations, 2).state == RowRelationState.REJECTED
    assert _relation(relations, 3).state == RowRelationState.REJECTED
    assert _disposition(semantic, 2).role == RowRole.HEADER


def test_continuation_chain_resolves_to_one_root_without_cycles():
    body = {
        1: {1: "Насос", 4: "Завод-", 5: "шт", 6: "1"},
        2: {1: "центробежный", 4: "А"},
        3: {1: "исполнение", 4: "Б"},
    }
    _table_ir, _context, _graph, relations, semantic = _run(body)
    assert [_relation(relations, index).state for index in (2, 3)] == [
        RowRelationState.CONFIRMED,
        RowRelationState.CONFIRMED,
    ]
    assert len(semantic.logical_items) == 1
    assert [ref.row_index for ref in semantic.logical_items[0].physical_row_refs] == [1, 2, 3]
    assert list(semantic.diagnostics["cycle_row_keys"]) == []
    assert semantic.conservation.semantic_conservation_pass


def test_competing_parent_interpretation_stays_unresolved():
    table, context, graph, _relations, _semantic = _run(
        {1: {1: "Насос", 5: "шт", 6: "1"}, 2: {1: "фрагмент"}}
    )
    source = table.row(2).ref
    first = table.row(1).ref
    competing = RowRelationAssessment(
        source_row_ref=source,
        candidate_target_refs=(first,),
        state=RowRelationState.AMBIGUOUS,
        evidence_strength="SUPPORTING",
        evidence=("competing parent hypotheses",),
    )
    semantic = GlobalRowSemanticsResolver().resolve(
        table, context, graph, (competing,)
    )
    disposition = _disposition(semantic, 2)
    assert disposition.role_state == RowRoleState.UNRESOLVED
    assert disposition.logical_item_id is None
    assert "continuation_relation_unresolved" in disposition.reasons


def test_non_item_parent_does_not_absorb_a_real_item_root():
    _table_ir, _context, _graph, relations, semantic = _run(
        {1: {8: "Раздел водоснабжения"}, 2: {1: "Насос", 4: "Завод"}}
    )
    relation = _relation(relations, 2)
    assert relation.state == RowRelationState.UNRESOLVED
    assert "parent_not_confirmed_item_root" in relation.contradictions
    assert _disposition(semantic, 2).role == RowRole.ITEM_ROOT
    assert _disposition(semantic, 2).logical_item_id is not None
    assert all(item.physical_row_refs[0].row_index != 1 for item in semantic.logical_items)


def test_context_and_component_rows_are_not_projected_as_logical_items():
    _table_ir, _context, _graph, _relations, semantic = _run(
        {1: {8: "К1"}, 2: {8: "• составная часть"}, 3: {8: "Примечание: проверить"}}
    )
    assert semantic.logical_items == ()
    assert all(disposition.role != RowRole.CONTINUATION for disposition in semantic.dispositions)


def test_p12_numbering_anomaly_remains_shadow_ambiguous_and_is_not_repaired():
    body = {
        1: {2: "3", 4: "5", 5: "6", 6: "17"},
        2: {0: "1", 1: "Реальный насос", 5: "шт", 6: "1"},
    }
    _table_ir, _context, graph, relations, semantic = _run(body)
    numbering = graph.row_role_assessments[1]
    assert numbering.selected_role == RowRole.SERVICE
    assert numbering.state == RowRoleState.AMBIGUOUS
    assert _relation(relations, 1).state == RowRelationState.REJECTED
    assert _disposition(semantic, 1).logical_item_id is None
    assert len(semantic.logical_items) == 1
    assert all(field.canonical_text != "7" for field in semantic.logical_items[0].fields.values())
    assert not semantic.conservation.semantic_conservation_pass


def test_metrics_expose_relation_and_conservation_quality():
    _table_ir, _context, _graph, _relations, semantic = _run(
        {**_parent(), 2: {1: "для установки", 4: "Нахимовский»"}}
    )
    metrics = semantic.diagnostics["metrics"]
    assert metrics["physical_nonempty_rows"] == 3
    assert metrics["confirmed_relation_count"] == 1
    assert metrics["logical_item_count"] == 1
    assert metrics["semantic_conservation_pass"] is True
    assert metrics["auto_resolved_row_rate"] == 1.0
    assert metrics["item_auto_resolution_rate"] == 1.0
