from __future__ import annotations

import inspect

from averon_import.services.ocr.reconstruction import rows_from_tables
from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
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
    assert diagnostics["schema"]["status"] == UNSUPPORTED
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
