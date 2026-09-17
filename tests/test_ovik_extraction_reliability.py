from __future__ import annotations

from pathlib import Path

from averon_import.core.normalizers import numeric_cell_metadata
from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.critical_verification import (
    attach_exact_cell_candidate,
    promote_exact_cell_candidate,
    resolve_numeric_shape_with_exact_cell_evidence,
)
from averon_import.services.ocr.semantics.semantic_projection import (
    StructuredReconstructionResult,
    project_semantic_table,
)
from averon_import.services.ocr.semantics.row_evidence import (
    RowRole,
    RowRoleState,
)
from averon_import.services.ocr.semantics.functional_analyzer import (
    TableFunctionalAnalyzer,
)
from averon_import.services.row_assembler import SpecificationRowAssembler
from averon_import.services.review_policy import (
    critical_blockers_for_row,
    critical_field_count,
    refresh_review_state,
)

from tests.test_stage712c_row_relations import _run


def _semantic_row(values: dict[str, str], *, review_reasons: list[str] | None = None) -> OcrRow:
    return OcrRow(
        source_row=1,
        values=values,
        confidences={},
        sources={key: "yandex_vision" for key in values},
        bbox={},
        metadata={
            "provider": "yandex_vision",
            "structured_table": True,
            "provider_has_explicit_rows": True,
            "semantic_authoritative": True,
            "semantic_resolved": True,
            "semantic_role": "ITEM_ROOT",
            "semantic_state": "REVIEW" if review_reasons else "VERIFIED",
            "semantic_review": bool(review_reasons),
            "semantic_required_critical_fields": ["quantity", "unit"],
            "review_reasons": list(review_reasons or []),
            "normalization": {},
        },
    )


def test_integer_like_decimal_quantity_is_review_only_but_real_fraction_survives():
    assert numeric_cell_metadata("40")["integer_like_decimal"] is False
    assert numeric_cell_metadata("4.0")["integer_like_decimal"] is True
    assert numeric_cell_metadata("2.5")["integer_like_decimal"] is False

    row = SpecificationRowAssembler().build_semantic_row(
        27,
        _semantic_row(
            {"name": "Трубы", "unit": "м", "quantity": "4.0"},
            review_reasons=["numeric_shape_suspect"],
        ),
    )
    row["ocr_metadata"]["normalization"]["quantity"] = numeric_cell_metadata("4.0")
    refresh_review_state(row)
    assert row["quantity"] == "4.0"
    assert row["status"] == "review"
    assert "numeric_shape_suspect" in critical_blockers_for_row(row)
    assert critical_field_count(row) == 1


def test_exact_cell_quantity_is_promoted_only_with_local_structural_proof():
    row = _semantic_row({"name": "Насос", "unit": "шт."})
    row.metadata.update({
        "target_cell_structural_safety": {
            "quantity": {"safe": True, "reasons": []},
        },
        "cell_bboxes": {"quantity": {"x": 0.7, "y": 0.2, "width": 0.05, "height": 0.02}},
    })
    assert attach_exact_cell_candidate(
        row, "quantity", "1", bbox={"x": 0.7, "y": 0.2, "width": 0.05, "height": 0.02}
    )
    assert row.values.get("quantity", "") == ""
    assert promote_exact_cell_candidate(row, "quantity")
    assert row.values["quantity"] == "1"
    assert row.metadata["value_candidates"]["quantity"]["auto_trusted"] is True
    assert row.metadata["semantic_review"] is False


def test_exact_cell_does_not_promote_package_quantity_or_suspicious_decimal():
    for unit, raw in (("компл.", "1"), ("м", "4.0")):
        row = _semantic_row({"name": "Комплект", "unit": unit})
        row.metadata["target_cell_structural_safety"] = {
            "quantity": {"safe": True, "reasons": []},
        }
        assert attach_exact_cell_candidate(row, "quantity", raw, bbox={})
        assert not promote_exact_cell_candidate(row, "quantity")
        assert "quantity" not in row.values


def test_blank_component_rejects_exact_candidate_without_raster_glyph_proof():
    row = _semantic_row({"name": "- Компонент", "unit": "шт."})
    row.metadata["semantic_role"] = "COMPONENT"
    row.metadata["semantic_required_critical_fields"] = []
    row.metadata["target_cell_structural_safety"] = {
        "quantity": {"safe": True, "reasons": []},
    }

    assert not attach_exact_cell_candidate(row, "quantity", "1", bbox={})
    assert row.values.get("quantity", "") == ""
    assert row.metadata.get("semantic_required_critical_fields") == []
    assert (row.metadata.get("value_candidates") or {}) == {}
    presented = SpecificationRowAssembler().build_semantic_row(1, row)
    assert "critical_value_missing" not in critical_blockers_for_row(presented)


def test_blank_component_keeps_glyph_proven_exact_candidate_for_review():
    row = _semantic_row({"name": "- Компонент", "unit": "шт."})
    row.metadata["semantic_role"] = "COMPONENT"
    row.metadata["semantic_required_critical_fields"] = []
    row.metadata["semantic_field_evidence"] = {
        "quantity": {"raster_glyph": True},
    }
    row.metadata["target_cell_structural_safety"] = {
        "quantity": {"safe": True, "reasons": []},
    }

    assert attach_exact_cell_candidate(row, "quantity", "2", bbox={})
    assert row.values.get("quantity", "") == ""
    assert row.metadata["semantic_required_critical_fields"] == ["quantity"]
    assert row.metadata["value_candidates"]["quantity"]["value_candidate"] == "2"
    assert not promote_exact_cell_candidate(row, "quantity")
    presented = SpecificationRowAssembler().build_semantic_row(1, row)
    assert presented["value_candidates"]["quantity"]["value_candidate"] == "2"
    assert "critical_value_missing" in critical_blockers_for_row(presented)


def test_legitimate_integer_like_decimal_requires_and_accepts_exact_agreement():
    row = _semantic_row(
        {"name": "Насос", "unit": "шт.", "quantity": "4.0"},
        review_reasons=["numeric_shape_suspect"],
    )
    row.metadata["target_cell_structural_safety"] = {
        "quantity": {"safe": True, "reasons": []},
    }
    assert attach_exact_cell_candidate(row, "quantity", "4.0", bbox={})
    assert resolve_numeric_shape_with_exact_cell_evidence(row, "quantity")
    assert row.values["quantity"] == "4.0"
    assert row.metadata["semantic_review"] is False
    assert "numeric_shape_suspect" not in row.metadata["review_reasons"]
    presented = SpecificationRowAssembler().build_semantic_row(1, row)
    assert presented["status"] == "recognized"
    assert critical_blockers_for_row(presented) == []


def test_integer_like_decimal_conflict_keeps_primary_and_exposes_candidate():
    row = _semantic_row(
        {"name": "Трубы", "unit": "м", "quantity": "4.0"},
        review_reasons=["numeric_shape_suspect"],
    )
    row.metadata["target_cell_structural_safety"] = {
        "quantity": {"safe": True, "reasons": []},
    }
    assert attach_exact_cell_candidate(row, "quantity", "40", bbox={})
    assert row.values["quantity"] == "4.0"
    candidate = row.metadata["value_candidates"]["quantity"]
    assert candidate["value_candidate"] == "40"
    assert candidate["review_reason"] == "numeric_shape_conflict"
    assert "secondary_conflict" in row.metadata["review_reasons"]
    assert not resolve_numeric_shape_with_exact_cell_evidence(row, "quantity")
    presented = SpecificationRowAssembler().build_semantic_row(1, row)
    assert presented["status"] == "review"
    assert "40" == presented["value_candidates"]["quantity"]["value_candidate"]
    assert "numeric_shape_conflict" in critical_blockers_for_row(presented)


def test_numeric_shape_trigger_does_not_reject_other_quantity_shapes():
    for value in ("2.5", "3.4", "2.2", "8.2", "4.9", "3.46", "4.33", "40", ""):
        assert numeric_cell_metadata(value)["integer_like_decimal"] is False


def test_alphabetic_system_requires_section_topology():
    for label in ("ГОСТ", "ООО", "КЖ", "ПС"):
        _table, _context, graph, _relations, _semantic = _run({
            1: {1: label},
            2: {1: "Точка", 5: "шт", 6: "1"},
        })
        selected = graph.row_role_assessments[0]
        assert not (
            selected.selected_role == RowRole.CONTEXT
            and selected.selected_qualifier == "SYSTEM"
        )


def test_alphabetic_adjacency_cannot_create_circular_section_system_context():
    for first in ("Поставщик", "КЖ", "ПС", "ГОСТ"):
        _table, _context, graph, _relations, _semantic = _run({
            1: {1: first},
            2: {1: "ООО"},
            3: {1: "Насос", 5: "шт", 6: "1"},
        })
        selected = {
            assessment.physical_row_ref.row_index: assessment
            for assessment in graph.row_role_assessments
        }
        assert not (
            selected[1].selected_role == RowRole.CONTEXT
            and selected[1].selected_qualifier == "SECTION"
            and selected[1].state == RowRoleState.CONFIRMED
        )
        assert not (
            selected[2].selected_role == RowRole.CONTEXT
            and selected[2].selected_qualifier == "SYSTEM"
            and selected[2].state == RowRoleState.CONFIRMED
        )


def test_package_components_keep_explicit_and_blank_quantity_without_swallowing_next_item():
    _table, context, graph, _relations, semantic = _run({
        1: {1: "Установка", 5: "компл."},
        2: {1: "- Компонент", 6: "2"},
        3: {1: "- Компонент без количества"},
        4: {1: "Отдельный насос", 5: "шт", 6: "1"},
    })
    roles = {
        assessment.physical_row_ref.row_index: assessment
        for assessment in graph.row_role_assessments
    }
    assert roles[2].selected_role == RowRole.COMPONENT
    assert roles[2].state == RowRoleState.CONFIRMED
    assert roles[3].selected_role == RowRole.COMPONENT
    assert roles[4].selected_role == RowRole.ITEM_ROOT

    projected = project_semantic_table(
        StructuredReconstructionResult(
            rows=[],
            physical_table=_table,
            semantic_table=semantic,
            page_size=(900.0, 400.0),
        )
    )
    components = [
        row for row in projected
        if row.metadata.get("semantic_role") == "COMPONENT"
    ]
    assert len(components) == 2
    assert components[0].values["quantity"] == "2"
    assert components[1].values.get("quantity", "") == ""
    assert all(not row.metadata.get("semantic_review") for row in components)
    assert not attach_exact_cell_candidate(
        components[1], "quantity", "1", bbox={}
    )
    assert components[1].metadata.get("semantic_required_critical_fields") == []
    assert components[1].metadata.get("value_candidates") == {}
    presented_components = [
        SpecificationRowAssembler().build_semantic_row(1, row)
        for row in components
    ]
    assert presented_components[0]["quantity"] == "2"
    assert presented_components[1].get("quantity") is None
    assert all(critical_blockers_for_row(row) == [] for row in presented_components)
    assert len(semantic.logical_items) == 2
    assert all(
        all(ref.row_index not in {2, 3} for ref in item.physical_row_refs)
        for item in semantic.logical_items
    )


def test_section_system_topology_and_system_lists_are_generic():
    _table, _context, graph, _relations, semantic = _run({
        1: {1: "II Дымоудаление"},
        2: {1: "ДВЕ"},
        3: {1: "Точка", 5: "шт", 6: "1"},
        4: {1: "III Кондиционирование"},
        5: {1: "K1,K2,K3"},
        6: {1: "Точка 2", 5: "шт", 6: "1"},
        7: {1: "IV _ Отопление"},
        8: {1: "K4, K4*"},
        9: {1: "Точка 3", 5: "шт", 6: "1"},
    })
    selected = {
        assessment.physical_row_ref.row_index: assessment
        for assessment in graph.row_role_assessments
    }
    for row_index in (1, 4, 7):
        assert selected[row_index].selected_role == RowRole.CONTEXT
        assert selected[row_index].selected_qualifier == "SECTION"
        assert selected[row_index].state == RowRoleState.CONFIRMED
    for row_index in (2, 5, 8):
        assert selected[row_index].selected_role == RowRole.CONTEXT
        assert selected[row_index].selected_qualifier == "SYSTEM"
        assert selected[row_index].state == RowRoleState.CONFIRMED
    assert [item.physical_row_refs[0].row_index for item in semantic.logical_items] == [3, 6, 9]


def test_lowercase_terminal_fragment_is_not_a_new_section():
    _table, _context, graph, _relations, _semantic = _run({
        1: {1: "Насос", 5: "шт", 6: "1"},
        2: {1: "сетка-SG60M) в компл.:"},
    })
    selected = graph.row_role_assessments[2]
    assert not (
        selected.selected_role == RowRole.CONTEXT
        and selected.selected_qualifier == "SECTION"
    )


def test_semantic_rows_without_provider_confidence_expose_none():
    row = SpecificationRowAssembler().build_semantic_row(
        1,
        _semantic_row({"name": "Насос", "unit": "шт.", "quantity": "1"}),
    )
    assert row["confidence"] is None
    assert row["status"] == "recognized"


def test_static_ui_keeps_unavailable_confidence_distinct_from_zero():
    app_js = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    assert "row.confidence === null" in app_js
    assert "numericShapeAgreed" in app_js
    assert "—" in app_js
