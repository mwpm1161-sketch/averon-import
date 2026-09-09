"""Shadow-only Stage 7.1.2B functional row evidence tests."""

from __future__ import annotations

import json

import pytest

from averon_import.services.ocr.physical_evidence import PhysicalEvidenceSnapshot
from averon_import.services.ocr.semantics.functional_analyzer import (
    TableFunctionalAnalyzer,
    physical_table_ir_from_snapshot,
)
from averon_import.services.ocr.semantics.row_evidence import RowRole, RowRoleState
from averon_import.services.ocr.semantics.semantic_table import TableAnalysisContext
from averon_import.services.ocr.table_ir import PhysicalTableRef


def _snapshot() -> PhysicalEvidenceSnapshot:
    rows = (
        {
            "source_row_index": 0,
            "bbox": {"vertices": [{"x": 0, "y": 0}, {"x": 200, "y": 20}]},
            "cells": [
                {"source_cell_index": 0, "column_index": 0, "raw_text": "Позиция", "bbox": {"x": 0, "y": 0, "width": 40, "height": 20}},
                {"source_cell_index": 1, "column_index": 1, "raw_text": "Наименование", "bbox": {"x": 40, "y": 0, "width": 80, "height": 20}},
                {"source_cell_index": 2, "column_index": 2, "raw_text": "Ед.", "bbox": {"x": 80, "y": 0, "width": 40, "height": 20}},
                {"source_cell_index": 3, "column_index": 3, "raw_text": "Количество", "bbox": {"x": 120, "y": 0, "width": 40, "height": 20}},
            ],
        },
        {
            "source_row_index": 1,
            "bbox": {"vertices": [{"x": 0, "y": 20}, {"x": 200, "y": 40}]},
            "cells": [
                {"source_cell_index": 4, "column_index": 0, "raw_text": "1", "bbox": {"x": 0, "y": 20, "width": 40, "height": 20}},
                {"source_cell_index": 5, "column_index": 1, "raw_text": "Насос", "bbox": {"x": 40, "y": 20, "width": 80, "height": 20}},
                {"source_cell_index": 6, "column_index": 2, "raw_text": "шт", "bbox": {"x": 120, "y": 20, "width": 40, "height": 20}},
                {"source_cell_index": 7, "column_index": 3, "raw_text": "2", "bbox": {"x": 160, "y": 20, "width": 40, "height": 20}},
            ],
        },
        {
            "source_row_index": 2,
            "bbox": {"vertices": [{"x": 0, "y": 40}, {"x": 200, "y": 60}]},
            "cells": [
                {"source_cell_index": 8, "column_index": 2, "raw_text": "3", "bbox": {"x": 80, "y": 40, "width": 40, "height": 20}},
                {"source_cell_index": 9, "column_index": 4, "raw_text": "5", "bbox": {"x": 160, "y": 40, "width": 40, "height": 20}},
                {"source_cell_index": 10, "column_index": 5, "raw_text": "6", "bbox": {"x": 200, "y": 40, "width": 40, "height": 20}},
                {"source_cell_index": 11, "column_index": 6, "raw_text": "17", "bbox": {"x": 240, "y": 40, "width": 40, "height": 20}},
            ],
        },
    )
    return PhysicalEvidenceSnapshot(
        grid={"source": "offline-raster", "x_boundaries": [0, 40, 80, 120, 160, 200, 240, 280], "y_boundaries": [0, 20, 40, 60]},
        selected_table_bounds={"x": 0, "y": 0, "width": 280, "height": 60},
        physical_rows=rows,
        spatial_words=(),
        assigned_word_refs={},
        structural_evidence={"provider": "offline"},
    )


def _graph():
    snapshot = _snapshot()
    table = physical_table_ir_from_snapshot(snapshot)
    context = TableAnalysisContext(
        physical_table=table,
        header_mapping={
            "mapping_status": "trusted",
            "header_candidate_rows": [0],
            "selected_mapping": {"0": ["position"], "1": ["name"], "2": ["unit"], "3": ["quantity"]},
            "fullText": "протокол испытаний кабельный журнал",
        },
        schema_assessment={"status": "supported"},
    )
    return TableFunctionalAnalyzer().analyze(table, context)


def test_snapshot_adapter_and_analyzer_are_wired_without_full_text_authority():
    graph = _graph()
    assessments = {item.physical_row_ref.row_index: item for item in graph.row_role_assessments}
    assert assessments[0].selected_role == RowRole.HEADER
    assert assessments[0].state == RowRoleState.CONFIRMED
    assert assessments[1].selected_role == RowRole.ITEM_ROOT
    assert assessments[2].selected_role == RowRole.SERVICE
    assert assessments[2].selected_qualifier == "NUMBERING_BAND"
    assert assessments[2].state == RowRoleState.AMBIGUOUS
    assert graph.diagnostics["relations_inferred"] is False
    assert graph.diagnostics["canonical_projection"] is False


def test_p12_like_numbering_band_records_outlier_without_repairing_it():
    graph = _graph()
    numbering = graph.as_dict()["diagnostics"]["numbering_rows"]
    assert len(numbering) == 1
    evidence = numbering[0]
    assert evidence["ordinal_values"] == [3, 5, 6, 17]
    assert evidence["ocr_outliers"] == [{"column_index": 6, "observed": 17, "expected": 7}]
    assert evidence["ordinal_offset"] == 1
    assert evidence["offset_support_count"] == 3
    assert evidence["offset_conflict"] is False
    assert evidence["ordinal_matches"] == [
        {"column_index": 2, "observed": 3, "expected": 3},
        {"column_index": 4, "observed": 5, "expected": 5},
        {"column_index": 5, "observed": 6, "expected": 6},
    ]
    assert evidence["ordinal_mismatches"] == [
        {"column_index": 6, "observed": 17, "expected": 7},
    ]
    assert evidence["numbering_band_ocr_anomaly"] is True
    assessments = {
        item.physical_row_ref.row_index: item
        for item in graph.row_role_assessments
    }
    assert assessments[2].state == RowRoleState.AMBIGUOUS
    assert evidence["expected_ordinals"]
    assert all(item["observed"] != 7 for item in evidence["ordinal_mismatches"])
    assert not any(
        assessment.selected_role == RowRole.ITEM_ROOT
        for assessment in graph.row_role_assessments
        if assessment.physical_row_ref.row_index == 2
    )


def test_same_physical_evidence_is_provider_neutral():
    snapshot = _snapshot()
    first = physical_table_ir_from_snapshot(snapshot)
    second = physical_table_ir_from_snapshot(
        PhysicalEvidenceSnapshot(
            grid=snapshot.grid,
            selected_table_bounds=snapshot.selected_table_bounds,
            physical_rows=snapshot.physical_rows,
            spatial_words=snapshot.spatial_words,
            assigned_word_refs=snapshot.assigned_word_refs,
            structural_evidence={"provider": "another-provider"},
        )
    )
    mapping = {
        "mapping_status": "trusted",
        "header_candidate_rows": [0],
        "selected_mapping": {"0": ["position"], "1": ["name"], "2": ["unit"], "3": ["quantity"]},
    }
    first_graph = TableFunctionalAnalyzer().analyze(first, TableAnalysisContext(first, mapping))
    second_graph = TableFunctionalAnalyzer().analyze(second, TableAnalysisContext(second, mapping))
    assert [item.as_dict() for item in first_graph.row_role_assessments] == [
        item.as_dict() for item in second_graph.row_role_assessments
    ]


def test_graph_diagnostics_are_deterministically_serializable():
    payload = _graph().as_dict()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == encoded
    assert payload["row_relation_assessments"] == []
    assert payload["diagnostics"]["provider_provenance_ignored"] is True


def test_numeric_only_row_far_from_header_is_not_numbering():
    snapshot = _snapshot()
    extra = dict(snapshot.physical_rows[2])
    extra["source_row_index"] = 4
    extra["bbox"] = {"vertices": [{"x": 0, "y": 80}, {"x": 200, "y": 100}]}
    far_snapshot = PhysicalEvidenceSnapshot(
        grid={"source": "offline-raster", "x_boundaries": [0, 40, 120, 160, 200], "y_boundaries": [0, 20, 40, 60, 80, 100]},
        selected_table_bounds=snapshot.selected_table_bounds,
        physical_rows=snapshot.physical_rows + (extra,),
    )
    table = physical_table_ir_from_snapshot(far_snapshot)
    context = TableAnalysisContext(
        table,
        {"header_candidate_rows": [0], "selected_mapping": {"0": ["position"], "1": ["name"], "2": ["unit"], "3": ["quantity"]}},
    )
    assessment = TableFunctionalAnalyzer().analyze(table, context).row_role_assessments[-1]
    assert assessment.selected_role != RowRole.SERVICE


def _numbering_graph(rows, *, header_rows=(0,)):
    """Build a generic, sanitized physical table for N1-N12 cases."""
    max_columns = max(4, max((len(values) for _row_index, values in rows), default=4))
    x_boundaries = [index * 60 for index in range(max_columns + 1)]
    y_boundaries = [index * 20 for index in range(6)]
    table_rows = [
        {
            "source_row_index": 0,
            "bbox": {"x": 0, "y": 0, "width": max_columns * 60, "height": 20},
            "cells": [
                {"source_cell_index": index, "column_index": index, "raw_text": text,
                 "bbox": {"x": index * 60, "y": 0, "width": 60, "height": 20}}
                for index, text in enumerate(
                    ("Позиция", "Наименование", "Ед.", "Количество")
                    + ("",) * (max_columns - 4)
                )
            ],
        }
    ]
    for row_index, values in rows:
        table_rows.append(
            {
                "source_row_index": row_index,
                "bbox": {"x": 0, "y": row_index * 20, "width": max_columns * 60, "height": 20},
                "cells": [
                    {"source_cell_index": row_index * 10 + column_index,
                     "column_index": column_index, "raw_text": text,
                     "bbox": {"x": column_index * 60, "y": row_index * 20,
                              "width": 60, "height": 20}}
                    for column_index, text in enumerate(values)
                    if text
                ],
            }
        )
    snapshot = PhysicalEvidenceSnapshot(
        grid={"source": "offline-raster", "x_boundaries": x_boundaries,
              "y_boundaries": y_boundaries},
        selected_table_bounds={"x": 0, "y": 0, "width": max_columns * 60, "height": 100},
        physical_rows=tuple(table_rows),
    )
    table = physical_table_ir_from_snapshot(snapshot)
    context = TableAnalysisContext(
        table,
        {
            "mapping_status": "trusted",
            "header_candidate_rows": list(header_rows),
            "selected_mapping": {"0": ["position"], "1": ["name"],
                                 "2": ["unit"], "3": ["quantity"]},
        },
    )
    return TableFunctionalAnalyzer().analyze(table, context)


@pytest.mark.parametrize(
    ("case", "rows", "expected_role", "expected_state"),
    [
        ("N1 clean contiguous", [(1, ("1", "2", "3"))], RowRole.SERVICE, RowRoleState.CONFIRMED),
        ("N2 sparse ordinals", [(1, ("1", "", "3", "4"))], RowRole.SERVICE, RowRoleState.CONFIRMED),
        ("N3 multi-digit OCR outlier", [(1, ("", "", "3", "", "5", "6", "17"))], RowRole.SERVICE, RowRoleState.AMBIGUOUS),
        ("N4 missing digits", [(1, ("1", "", "", "", "4"))], RowRole.SERVICE, RowRoleState.AMBIGUOUS),
        ("N5 duplicate", [(1, ("1", "2", "2"))], RowRole.SERVICE, RowRoleState.AMBIGUOUS),
        ("N6 non-monotonic", [(1, ("1", "3", "2"))], RowRole.SERVICE, RowRoleState.AMBIGUOUS),
        ("N7 product row with ordinal", [(1, ("1", "Насос", "2"))], RowRole.ITEM_ROOT, RowRoleState.CONFIRMED),
        ("N8 product row with identity", [(1, ("2", "Насос", "N2", "1"))], RowRole.ITEM_ROOT, RowRoleState.CONFIRMED),
        ("N9 numeric row far from header", [(4, ("1", "2", "3"))], None, RowRoleState.UNRESOLVED),
        ("N10 numeric plus garbage", [(1, ("1", "2", "x"))], RowRole.SERVICE, RowRoleState.AMBIGUOUS),
        ("N11 numbering then item", [(1, ("1", "2", "3")), (2, ("2", "Насос", "шт", "1"))], RowRole.ITEM_ROOT, RowRoleState.CONFIRMED),
        ("N12 repeated header then numbering", [(1, ("Ед.", "Количество")), (2, ("1", "2", "3"))], RowRole.SERVICE, RowRoleState.CONFIRMED),
    ],
)
def test_sanitized_numbering_cases_remain_shadow_only(case, rows, expected_role, expected_state):
    graph = _numbering_graph(rows, header_rows=(0, 1) if case.startswith("N12") else (0,))
    assessment = graph.row_role_assessments[-1]
    assert assessment.selected_role == expected_role, case
    assert assessment.state == expected_state, case
    assert graph.diagnostics["canonical_projection"] is False
    if case.startswith("N3"):
        diagnostics = graph.as_dict()["diagnostics"]["numbering_rows"][-1]
        assert diagnostics["ordinal_offset"] == 1
        assert diagnostics["offset_support_count"] == 3
        assert diagnostics["offset_conflict"] is False
        assert len(diagnostics["ordinal_matches"]) == 3
        assert diagnostics["ordinal_mismatches"] == [
            {"column_index": 6, "observed": 17, "expected": 7},
        ]
        assert graph.as_dict()["diagnostics"]["numbering_band_ocr_anomalies"]


@pytest.mark.parametrize(
    ("values", "offset", "support", "match_count", "mismatch_count"),
    [
        (("1", "2", "3"), 1, 3, 3, 0),
        (("1", "", "3", "4"), 1, 3, 3, 0),
        (("", "", "3", "", "5", "6", "7"), 1, 4, 4, 0),
    ],
)
def test_ordinal_diagnostics_follow_physical_columns(values, offset, support, match_count, mismatch_count):
    diagnostics = _numbering_graph([(1, values)]).as_dict()["diagnostics"]["numbering_rows"][-1]
    assert diagnostics["ordinal_offset"] == offset
    assert diagnostics["offset_support_count"] == support
    assert diagnostics["offset_conflict"] is False
    assert len(diagnostics["ordinal_matches"]) == match_count
    assert len(diagnostics["ordinal_mismatches"]) == mismatch_count


def test_conflicting_offsets_remain_ambiguous_without_guessing():
    graph = _numbering_graph([(1, ("1", "3", "5", "4", "6", "8"))])
    diagnostics = graph.as_dict()["diagnostics"]["numbering_rows"][-1]
    assessment = graph.row_role_assessments[-1]
    assert diagnostics["ordinal_offset"] is None
    assert diagnostics["offset_support_count"] == 2
    assert diagnostics["offset_conflict"] is True
    assert diagnostics["ordinal_matches"] == []
    assert diagnostics["ordinal_mismatches"] == []
    assert assessment.selected_role is None
    assert assessment.state == RowRoleState.UNRESOLVED
    assert "offset_conflict" in assessment.contradictions
