"""Compatibility facade for the Stage 7.1 family/schema split.

Production reconstruction uses :mod:`semantics.family_classifier` and
:mod:`semantics.schema_gate` directly.  This facade remains for older callers
and tests that supplied explicit bounded ``header_text``/``context_text``.
It does not inspect page-wide text and it does not own family keywords.
"""

from __future__ import annotations

from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
)
from averon_import.services.ocr.semantics.family_classifier import (
    DEFAULT_TABLE_FAMILY_CLASSIFIER,
)
from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult
from averon_import.services.ocr.semantics.schema_gate import (
    AMBIGUOUS,
    CORE_FIELDS,
    DEFAULT_SCHEMA_GATE,
    OPTIONAL_FIELDS,
    SUPPORTED,
    UNSUPPORTED,
    SchemaAssessment,
)


def _compatibility_mapping_result(
    column_count: int,
    mapping: dict[int, tuple[str, ...]],
    header_rows: set[int],
) -> HeaderMappingResult:
    normalized = {
        int(column): tuple(str(field) for field in fields)
        for column, fields in mapping.items()
    }
    fields = {field for values in normalized.values() for field in values}
    inverse: dict[str, list[int]] = {}
    for column, values in normalized.items():
        for field in values:
            inverse.setdefault(field, []).append(column)
    illegal = any(
        len(values) != 1 and set(values) != {"mass", "note"}
        for values in normalized.values()
    ) or any(len(set(columns)) > 1 for columns in inverse.values())
    missing = tuple(sorted(CORE_FIELDS - fields))
    status = "ambiguous" if illegal else "trusted" if normalized and not missing else "unavailable"
    reasons: list[str] = []
    if illegal:
        reasons.append("multiple_semantic_fields_in_one_column")
    if missing:
        reasons.append("missing_core_fields:" + ",".join(missing))
    return HeaderMappingResult(
        status=status,
        header_rows=tuple(sorted(header_rows)),
        mapping=normalized,
        candidates_by_column={},
        best_score=1.0 if normalized else 0.0,
        second_best_score=0.0,
        assignment_margin=1.0 if normalized else 0.0,
        unmapped_columns=tuple(column for column in range(max(0, column_count)) if column not in normalized),
        missing_core_fields=missing,
        reasons=tuple(reasons),
    )


class SupportedSpecificationSchemaRecognizer:
    """Backward-compatible entry point delegating to the separated layers."""

    def assess(
        self,
        *,
        column_count: int,
        mapping: dict[int, tuple[str, ...]],
        header_rows: set[int],
        header_text: str = "",
        context_text: str = "",
    ) -> SchemaAssessment:
        result = _compatibility_mapping_result(column_count, mapping, header_rows)
        regions = []
        if header_text.strip():
            regions.append(
                ContextRegionEvidence(
                    kind="header",
                    bounds=(0.0, 0.0, 1.0, 1.0),
                    text=header_text.strip()[:1000],
                    source="explicit_bounded_context",
                )
            )
        if context_text.strip():
            regions.append(
                ContextRegionEvidence(
                    kind="compatibility_context",
                    bounds=(0.0, 0.0, 1.0, 1.0),
                    text=context_text.strip()[:500],
                    source="explicit_bounded_context",
                )
            )
        family = DEFAULT_TABLE_FAMILY_CLASSIFIER.assess(BoundedFamilyContext(tuple(regions)))
        return DEFAULT_SCHEMA_GATE.assess(
            column_count=column_count,
            mapping=result,
            family=family,
        )


DEFAULT_SCHEMA_RECOGNIZER = SupportedSpecificationSchemaRecognizer()

__all__ = [
    "AMBIGUOUS",
    "CORE_FIELDS",
    "DEFAULT_SCHEMA_RECOGNIZER",
    "OPTIONAL_FIELDS",
    "SUPPORTED",
    "UNSUPPORTED",
    "SchemaAssessment",
    "SupportedSpecificationSchemaRecognizer",
]
