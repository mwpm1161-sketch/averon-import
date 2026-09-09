"""Sanitized Stage 7.1.2D.3 semantic safety policy regressions."""

from __future__ import annotations

from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.reconstruction import _schema_gate_structural_evidence
from averon_import.services.ocr.semantics.functional_analyzer import TableFunctionalAnalyzer
from averon_import.services.ocr.semantics.row_evidence import (
    RowRole,
    RowRoleState,
    SemanticReviewImpact,
)
from averon_import.services.ocr.semantics.row_relation_analyzer import RowRelationAnalyzer
from averon_import.services.ocr.semantics.row_semantics_resolver import GlobalRowSemanticsResolver
from averon_import.services.ocr.semantics.schema_gate import DEFAULT_SCHEMA_GATE
from averon_import.services.ocr.semantics.family_classifier import TableFamilyClassifier
from averon_import.services.ocr.semantics.context_evidence import BoundedFamilyContext
from averon_import.services.review_policy import critical_blockers_for_row
from averon_import.services.row_assembler import SpecificationRowAssembler
from tests.test_stage702_safety import _evidence, _grid, _trusted_schema_mapping
from tests.test_stage712c_row_relations import _run


def _semantic(body: dict[int, dict[int, str]]):
    table, context, graph, relations, semantic = _run(body)
    return table, context, graph, relations, semantic


def test_non_output_unresolved_rows_do_not_block_page_by_themselves():
    status = page_status_from_diagnostics(
        1,
        {
            "selected_mode": "geometry_first",
            "geometry_grid": {"high_confidence": True},
            "schema": {"status": "supported"},
            "semantic_authoritative": True,
            "unresolved_physical_row_count": 1,
            "semantic_output_critical_unresolved_count": 0,
            "semantic_non_output_unresolved_count": 1,
            "semantic_safety_special_count": 0,
        },
        row_count=1,
    )
    assert status.output_status == "USABLE"
    assert "physical_row_semantics_unresolved" not in status.blockers


def test_output_critical_unresolved_rows_block_page():
    status = page_status_from_diagnostics(
        1,
        {
            "selected_mode": "geometry_first",
            "geometry_grid": {"high_confidence": True},
            "schema": {"status": "supported"},
            "semantic_authoritative": True,
            "unresolved_physical_row_count": 1,
            "semantic_output_critical_unresolved_count": 1,
            "semantic_non_output_unresolved_count": 0,
            "semantic_safety_special_count": 0,
        },
        row_count=1,
    )
    assert status.output_status == "REVIEW_REQUIRED"
    assert "physical_row_semantics_unresolved" in status.blockers


def test_generic_section_heading_is_non_item_and_has_no_output_impact():
    _table, _context, graph, _relations, semantic = _semantic({1: {1: "IV. Раздел"}})
    assessment = graph.row_role_assessments[-1]
    disposition = semantic.dispositions[-1]
    assert assessment.selected_role == RowRole.CONTEXT
    assert assessment.selected_qualifier == "SECTION"
    assert assessment.state == RowRoleState.CONFIRMED
    assert not semantic.logical_items
    assert disposition.review_impact == SemanticReviewImpact.NONE


def test_confirmed_context_section_projects_as_section_not_review_evidence():
    raw = OcrRow(
        1,
        {"name": "IV. Раздел"},
        {},
        {},
        {},
        {
            "provider": "yandex_vision",
            "semantic_authoritative": True,
            "semantic_resolved": True,
            "semantic_role": "CONTEXT",
            "semantic_qualifier": "SECTION",
            "semantic_state": "VERIFIED",
        },
    )
    row = SpecificationRowAssembler().build_semantic_row(1, raw)
    assert row["row_type"] == "section"
    assert row["semantic_review"] is False


def test_ordinary_name_only_row_remains_output_critical():
    _table, _context, graph, _relations, semantic = _semantic({1: {1: "Насос"}})
    assessment = graph.row_role_assessments[-1]
    disposition = semantic.dispositions[-1]
    assert assessment.selected_role == RowRole.ITEM_ROOT
    assert assessment.state == RowRoleState.AMBIGUOUS
    assert disposition.review_impact == SemanticReviewImpact.OUTPUT_CRITICAL
    assert not semantic.logical_items


def test_model_text_with_critical_product_fields_is_not_section():
    _table, _context, graph, _relations, semantic = _semantic(
        {1: {1: "IV. ABC-400", 5: "шт.", 6: "2"}}
    )
    assessment = graph.row_role_assessments[-1]
    assert assessment.selected_role == RowRole.ITEM_ROOT
    assert assessment.state == RowRoleState.CONFIRMED
    assert semantic.logical_items


def test_explicit_system_code_is_non_output():
    _table, _context, graph, _relations, semantic = _semantic({1: {0: "К1"}})
    assessment = graph.row_role_assessments[-1]
    disposition = semantic.dispositions[-1]
    assert assessment.selected_role == RowRole.CONTEXT
    assert assessment.selected_qualifier == "SYSTEM"
    assert assessment.state == RowRoleState.CONFIRMED
    assert disposition.review_impact == SemanticReviewImpact.NONE


def test_numbering_anomaly_is_safety_special_not_item_output():
    _table, _context, graph, _relations, semantic = _semantic(
        {
            1: {2: "3", 4: "5", 5: "6", 6: "17"},
            2: {0: "1", 1: "Реальный насос", 5: "шт", 6: "1"},
        }
    )
    disposition = semantic.dispositions[1]
    assert graph.row_role_assessments[1].selected_qualifier == "NUMBERING_BAND"
    assert disposition.review_impact == SemanticReviewImpact.SAFETY_SPECIAL
    assert semantic.diagnostics["metrics"]["semantic_safety_special_count"] == 1
    assert not any(
        item.logical_id == "item:1" for item in semantic.logical_items
    )


def test_extra_provider_edge_in_optional_column_is_informational():
    grid = _grid()
    edges = sorted([
        *grid.x_boundaries,
        (grid.x_boundaries[8] + grid.x_boundaries[9]) / 2,
    ])
    evidence = _evidence(edges)
    assert evidence["extra_provider_internal_boundaries"]
    assert evidence["critical_boundary_conflicts"] == []
    assert evidence["material_column_disagreement"] is False
    assert evidence["material_disagreement"] is False
    gate_evidence = _schema_gate_structural_evidence(evidence)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=9,
        mapping=_trusted_schema_mapping(),
        family=TableFamilyClassifier().assess(BoundedFamilyContext()),
        structural=gate_evidence,
    )
    assert assessment.status == "supported"


def test_informational_structural_reason_does_not_block_complete_item():
    row = {
        "row_type": "item",
        "name": "Насос",
        "unit": "шт.",
        "quantity": "2",
        "mass": "1",
        "review_reasons": ["structural_ambiguity", "structural_disagreement"],
        "ocr_metadata": {
            "provider": "yandex_vision",
            "structured_table": True,
            "semantic_structural_impact": "INFORMATIONAL",
        },
    }
    blockers = critical_blockers_for_row(row)
    assert "structural_ambiguity" not in blockers
    assert "structural_disagreement" not in blockers
