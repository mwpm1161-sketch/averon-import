"""Safety gate separating family evidence from semantic schema evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS as AMBIGUOUS_FAMILY,
    OTHER_TABLE,
    TableFamilyAssessment,
)
from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult


SUPPORTED = "supported"
AMBIGUOUS = "ambiguous"
UNSUPPORTED = "unsupported"

CORE_FIELDS = frozenset({"name", "unit", "quantity"})
OPTIONAL_FIELDS = frozenset({
    "position", "type_mark", "code", "manufacturer", "mass", "note",
})


@dataclass(frozen=True, slots=True)
class SchemaGateEvidence:
    header_body_conflict: bool = False
    safe_relevant_structure: bool = True
    illegal_critical_combination: bool = False
    structural_reasons: tuple[str, ...] = ()
    critical_boundary_conflicts: tuple[Any, ...] = ()
    material_column_conflict: bool = False
    unsafe_physical_column_anchoring: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "header_body_conflict": self.header_body_conflict,
            "safe_relevant_structure": self.safe_relevant_structure,
            "illegal_critical_combination": self.illegal_critical_combination,
            "structural_reasons": list(self.structural_reasons),
            "critical_boundary_conflicts": list(self.critical_boundary_conflicts),
            "material_column_conflict": self.material_column_conflict,
            "unsafe_physical_column_anchoring": self.unsafe_physical_column_anchoring,
        }


@dataclass(frozen=True, slots=True)
class SchemaAssessment:
    status: str
    coverage_score: float
    uniqueness_score: float
    header_consistency: float
    mapped_fields: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    family_assessment: dict[str, Any] | None = None
    mapping_status: str = "unavailable"
    supported_variant: str | None = None
    decision_reasons: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status == SUPPORTED

    def as_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "coverage_score": round(self.coverage_score, 4),
            "uniqueness_score": round(self.uniqueness_score, 4),
            "header_consistency": round(self.header_consistency, 4),
            "mapped_fields": list(self.mapped_fields),
            "reasons": list(self.reasons),
            "mapping_status": self.mapping_status,
            "supported_variant": self.supported_variant,
            "decision_reasons": list(self.decision_reasons),
        }
        if self.family_assessment is not None:
            result["family"] = dict(self.family_assessment)
        return result


def _fields(mapping: HeaderMappingResult) -> set[str]:
    return {
        field
        for values in mapping.mapping.values()
        for field in values
        if field in CORE_FIELDS or field in OPTIONAL_FIELDS
    }


class SchemaGate:
    """Decide whether an already-mapped table is safe specification schema."""

    def assess(
        self,
        *,
        column_count: int,
        mapping: HeaderMappingResult,
        family: TableFamilyAssessment,
        structural: SchemaGateEvidence | None = None,
    ) -> SchemaAssessment:
        evidence = structural or SchemaGateEvidence()
        mapped_fields = _fields(mapping)
        reasons: list[str] = list(mapping.reasons)
        decision: list[str] = []
        structural_blockers: list[str] = []
        if evidence.critical_boundary_conflicts:
            structural_blockers.append("critical_boundary_conflicts")
        if evidence.material_column_conflict:
            structural_blockers.append("material_column_conflict")
        if evidence.unsafe_physical_column_anchoring:
            structural_blockers.append("unsafe_physical_column_anchoring")
        illegal = bool(evidence.illegal_critical_combination)
        multi_field_columns = [
            column for column, values in mapping.mapping.items()
            if len(values) != 1 and set(values) != {"mass", "note"}
        ]
        inverse: dict[str, list[int]] = {}
        for column, values in mapping.mapping.items():
            for field in values:
                inverse.setdefault(field, []).append(column)
        duplicate_fields = {
            field for field, columns in inverse.items() if len(set(columns)) > 1
        }
        if multi_field_columns:
            illegal = True
            reasons.append("multiple_semantic_fields_in_one_column")
        if duplicate_fields:
            illegal = True
            reasons.append("semantic_field_has_multiple_columns")
        coverage = min(1.0, len(mapping.mapping) / max(1, int(column_count)))
        uniqueness = 0.0 if illegal else 1.0
        header_consistency = 1.0 if mapping.header_rows else 0.0
        missing_core = sorted(CORE_FIELDS - mapped_fields)
        if missing_core:
            reasons.append("missing_core_fields:" + ",".join(missing_core))
        if len(mapped_fields & OPTIONAL_FIELDS) < 1:
            reasons.append("insufficient_optional_specification_evidence")
        if coverage < 0.45:
            reasons.append("semantic_coverage_too_low")
        if evidence.header_body_conflict or "header_body_conflict" in mapping.reasons:
            reasons.append("header_body_conflict")
        reasons.extend(evidence.structural_reasons)
        reasons.extend(structural_blockers)
        if family.negative_evidence:
            reasons.append(
                "negative_document_family:" + ",".join(
                    sorted({item.code for item in family.negative_evidence})
                )
            )
        elif family.family == AMBIGUOUS_FAMILY:
            reasons.append("family_evidence_missing")
        elif family.positive_evidence:
            reasons.append(
                "positive_document_family:" + ",".join(
                    sorted({item.code for item in family.positive_evidence})
                )
            )
        reasons = list(dict.fromkeys(reasons))

        family_data = family.as_dict()
        base = dict(
            coverage_score=coverage,
            uniqueness_score=uniqueness,
            header_consistency=header_consistency,
            mapped_fields=tuple(sorted(mapped_fields)),
            family_assessment=family_data,
            mapping_status=mapping.status,
        )
        if column_count <= 0:
            return SchemaAssessment(UNSUPPORTED, 0.0, 0.0, 0.0, (), ("invalid_column_count",), **{
                key: value for key, value in base.items() if key not in {"coverage_score", "uniqueness_score", "header_consistency", "mapped_fields"}
            })
        if family.family == OTHER_TABLE:
            reasons.append("confirmed_other_table")
            return SchemaAssessment(UNSUPPORTED, **base, reasons=tuple(dict.fromkeys(reasons)), decision_reasons=("confirmed_other_table",))
        if mapping.status != "trusted":
            decision.append("mapping_not_trusted")
            # A supported-family signal does not turn an unavailable/ambiguous
            # mapper into an unsupported table.  The physical evidence remains
            # reviewable, but no semantic rows are accepted.
            status = AMBIGUOUS
        elif missing_core or illegal or evidence.header_body_conflict or not evidence.safe_relevant_structure or structural_blockers:
            status = AMBIGUOUS
            decision.append("critical_schema_evidence_incomplete")
        elif family.family == "SUPPORTED_SPECIFICATION":
            if "semantic_coverage_too_low" in reasons:
                status = UNSUPPORTED
                decision.append("semantic_coverage_too_low")
            else:
                status = SUPPORTED
                decision.append("positive_family_evidence")
        elif family.family == AMBIGUOUS_FAMILY:
            canonical_ready = (
                family.reasons == ("family_context_missing",)
                and not family.negative_evidence
                and len(mapped_fields) >= 6
                and not duplicate_fields
                and not multi_field_columns
                and mapping.status == "trusted"
                and not missing_core
                and "semantic_coverage_too_low" not in reasons
            )
            if canonical_ready:
                status = SUPPORTED
                decision.append("canonical_header_without_family_context")
            else:
                status = UNSUPPORTED if "semantic_coverage_too_low" in reasons else AMBIGUOUS
                decision.append("family_context_missing")
        else:
            status = AMBIGUOUS
            decision.append("family_not_confirmed")
        return SchemaAssessment(
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            decision_reasons=tuple(dict.fromkeys(decision)),
            supported_variant=("canonical_header" if "canonical_header_without_family_context" in decision else "family_confirmed" if status == SUPPORTED else None),
            **base,
        )

    evaluate = assess


DEFAULT_SCHEMA_GATE = SchemaGate()
