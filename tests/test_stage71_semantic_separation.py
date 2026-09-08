from __future__ import annotations

import inspect

from averon_import.services.ocr.reconstruction import rows_from_tables
from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
    bounded_context_from_words,
)
from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS,
    OTHER_TABLE,
    SUPPORTED_SPECIFICATION,
    TableFamilyClassifier,
)
from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult
from averon_import.services.ocr.semantics.schema_gate import (
    DEFAULT_SCHEMA_GATE,
    AMBIGUOUS as AMBIGUOUS_SCHEMA,
    SchemaGateEvidence,
    SUPPORTED,
    UNSUPPORTED,
)


def _mapping_result(fields: dict[int, tuple[str, ...]], status: str = "trusted") -> HeaderMappingResult:
    mapped = {field for values in fields.values() for field in values}
    return HeaderMappingResult(
        status=status,
        header_rows=(0,),
        mapping=fields,
        candidates_by_column={},
        best_score=1.0,
        second_best_score=0.0,
        assignment_margin=1.0,
        unmapped_columns=(),
        missing_core_fields=tuple(sorted({"name", "unit", "quantity"} - mapped)),
        reasons=(),
    )


def _context(text: str, kind: str = "table_caption") -> BoundedFamilyContext:
    return BoundedFamilyContext((ContextRegionEvidence(kind, (0, 0, 1, 1), text),))


def test_family_classifier_has_no_mapper_dependency_or_mapper_tuning_parameters():
    import averon_import.services.ocr.semantics.family_classifier as module

    assert "column_mapper" not in inspect.getsource(module)
    assert list(inspect.signature(TableFamilyClassifier.assess).parameters) == [
        "self", "context"
    ]


def test_family_classifier_is_ambiguous_without_bounded_family_context():
    assessment = TableFamilyClassifier().assess(BoundedFamilyContext())
    assert assessment.family == AMBIGUOUS
    assert "family_context_missing" in assessment.reasons


def test_family_classifier_distinguishes_strong_positive_and_other_context():
    classifier = TableFamilyClassifier()
    assert classifier.assess(_context("Спецификация оборудования")).family == SUPPORTED_SPECIFICATION
    assert classifier.assess(_context("Кабельный журнал")).family == OTHER_TABLE


def test_schema_gate_owns_canonical_header_promotion():
    mapping = _mapping_result({
        0: ("position",), 1: ("name",), 2: ("type_mark",),
        3: ("code",), 4: ("manufacturer",), 5: ("unit",),
        6: ("quantity",), 7: ("mass",), 8: ("note",),
    })
    family = TableFamilyClassifier().assess(BoundedFamilyContext())
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=9, mapping=mapping, family=family
    )
    assert assessment.status == SUPPORTED
    assert assessment.supported_variant == "canonical_header"


def test_confirmed_other_family_can_never_be_promoted():
    mapping = _mapping_result({
        0: ("position",), 1: ("name",), 2: ("type_mark",),
        3: ("code",), 4: ("manufacturer",), 5: ("unit",),
        6: ("quantity",), 7: ("mass",), 8: ("note",),
    })
    family = TableFamilyClassifier().assess(_context("Кабельный журнал"))
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=9, mapping=mapping, family=family
    )
    assert assessment.status == UNSUPPORTED


def test_physical_evidence_is_retained_when_schema_is_rejected():
    table = {
        "rowCount": 2,
        "columnCount": 4,
        "cells": [
            {
                "rowIndex": row,
                "columnIndex": column,
                "text": text,
                "boundingBox": {"vertices": [
                    {"x": column * 100, "y": row * 50},
                    {"x": (column + 1) * 100, "y": row * 50},
                    {"x": (column + 1) * 100, "y": (row + 1) * 50},
                    {"x": column * 100, "y": (row + 1) * 50},
                ]},
            }
            for row, values in enumerate((("Описание", "Размер", "Цена", "Сумма"), ("x", "1", "2", "3")))
            for column, text in enumerate(values)
        ],
    }
    diagnostics: dict = {}
    rows = rows_from_tables([table], 400, 100, "test", diagnostics=diagnostics)
    assert rows is None
    assert diagnostics["schema"]["status"] == AMBIGUOUS_SCHEMA
    assert diagnostics["physical_evidence"]["physical_rows"]
    assert diagnostics["physical_evidence"]["header_mapping"]


def test_untrusted_mapping_does_not_create_semantic_rows_even_with_family_evidence():
    mapping = _mapping_result({1: ("name",)}, status="ambiguous")
    family = TableFamilyClassifier().assess(_context("Спецификация оборудования"))
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=4, mapping=mapping, family=family
    )
    assert assessment.status == "ambiguous"
    assert assessment.trusted is False


def test_generic_negative_words_are_weak_and_do_not_confirm_other_family():
    classifier = TableFamilyClassifier()
    for text in ("стоимость", "сечение", "кабель", "усилие", "начало"):
        assessment = classifier.assess(_context(text))
        assert assessment.family == AMBIGUOUS
        assert not assessment.negative_evidence
        assert assessment.weak_evidence


def test_body_only_family_words_are_not_family_context():
    context = BoundedFamilyContext((
        ContextRegionEvidence("body", (0, 0, 1, 1), "сечение стоимость кабель марка металла"),
    ))
    assessment = TableFamilyClassifier().assess(context)
    assert assessment.family == AMBIGUOUS
    assert not assessment.negative_evidence


def test_explicit_and_profile_negative_evidence_is_confirmed():
    classifier = TableFamilyClassifier()
    assert classifier.assess(_context("Кабельный журнал")).family == OTHER_TABLE
    assert classifier.assess(_context("цена стоимость сумма")).family == OTHER_TABLE
    assert classifier.assess(_context("трасса начало конец кабель")).family == OTHER_TABLE
    assert classifier.assess(_context("марка металла")).family == OTHER_TABLE


def test_far_context_above_tall_table_is_outside_hard_cap():
    words = [
        {"text": "Спецификация", "vertices": [
            {"x": 10, "y": 545}, {"x": 140, "y": 545},
            {"x": 140, "y": 555}, {"x": 10, "y": 555},
        ]},
        {"text": "Сметная документация", "vertices": [
            {"x": 10, "y": 500}, {"x": 230, "y": 500},
            {"x": 230, "y": 510}, {"x": 10, "y": 510},
        ]},
    ]
    context = bounded_context_from_words((0, 600, 500, 1000), words, page_height=1000)
    assert [region.text for region in context.regions] == ["Спецификация"]


def test_nearest_contiguous_caption_cluster_is_preserved():
    words = [
        {"text": "Раздел", "vertices": [
            {"x": 10, "y": 90}, {"x": 70, "y": 90},
            {"x": 70, "y": 100}, {"x": 10, "y": 100},
        ]},
        {"text": "Спецификация", "vertices": [
            {"x": 10, "y": 115}, {"x": 140, "y": 115},
            {"x": 140, "y": 125}, {"x": 10, "y": 125},
        ]},
    ]
    context = bounded_context_from_words((0, 140, 500, 900), words, page_height=1000)
    assert {region.text for region in context.regions} == {"Раздел", "Спецификация"}


def test_supported_family_with_unavailable_or_ambiguous_mapping_is_ambiguous():
    family = TableFamilyClassifier().assess(_context("Спецификация оборудования"))
    unavailable = _mapping_result({}, status="unavailable")
    ambiguous = _mapping_result({1: ("name",)}, status="ambiguous")
    assert DEFAULT_SCHEMA_GATE.assess(column_count=4, mapping=unavailable, family=family).status == AMBIGUOUS_SCHEMA
    assert DEFAULT_SCHEMA_GATE.assess(column_count=4, mapping=ambiguous, family=family).status == AMBIGUOUS_SCHEMA


def test_low_coverage_cannot_canonical_promote():
    mapping = _mapping_result({
        0: ("name",), 1: ("unit",), 2: ("quantity",), 3: ("code",), 4: ("mass",),
    })
    assessment = DEFAULT_SCHEMA_GATE.assess(column_count=30, mapping=mapping, family=TableFamilyClassifier().assess(BoundedFamilyContext()))
    assert assessment.status == UNSUPPORTED
    assert "semantic_coverage_too_low" in assessment.reasons


def test_structural_critical_conflict_is_schema_ambiguous_but_provider_mismatch_is_not():
    mapping = _mapping_result({
        0: ("position",), 1: ("name",), 2: ("type_mark",), 3: ("code",),
        4: ("manufacturer",), 5: ("unit",), 6: ("quantity",), 7: ("mass",), 8: ("note",),
    })
    family = TableFamilyClassifier().assess(BoundedFamilyContext())
    critical = DEFAULT_SCHEMA_GATE.assess(
        column_count=9, mapping=mapping, family=family,
        structural=SchemaGateEvidence(critical_boundary_conflicts=(0.7,)),
    )
    informational = DEFAULT_SCHEMA_GATE.assess(
        column_count=9, mapping=mapping, family=family,
        structural=SchemaGateEvidence(structural_reasons=("provider_column_count_mismatch",)),
    )
    assert critical.status == AMBIGUOUS_SCHEMA
    assert informational.status == SUPPORTED


def test_structural_snapshot_is_present_for_rejected_schema():
    table = {
        "rowCount": 2,
        "columnCount": 4,
        "cells": [
            {"rowIndex": row, "columnIndex": column, "text": text,
             "boundingBox": {"vertices": [
                 {"x": column * 100, "y": row * 50},
                 {"x": (column + 1) * 100, "y": row * 50},
                 {"x": (column + 1) * 100, "y": (row + 1) * 50},
                 {"x": column * 100, "y": (row + 1) * 50},
             ]}}
            for row, values in enumerate((("Описание", "Размер", "Цена", "Сумма"), ("x", "1", "2", "3")))
            for column, text in enumerate(values)
        ],
    }
    diagnostics: dict = {}
    assert rows_from_tables([table], 400, 100, "test", diagnostics=diagnostics) is None
    assert diagnostics["physical_evidence"]["physical_rows"]
    assert diagnostics["physical_evidence"]["header_mapping"]
