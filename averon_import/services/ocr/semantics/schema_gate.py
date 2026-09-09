"""Fail-closed schema gate driven by observed evidence and profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS as AMBIGUOUS_FAMILY,
    OTHER_TABLE,
    TableFamilyAssessment,
)
from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult
from averon_import.services.ocr.semantics.observed_schema import ObservedSchema
from averon_import.services.ocr.semantics.schema_profiles import (
    AMBIGUOUS as AMBIGUOUS_PROFILE,
    DEFAULT_SCHEMA_PROFILE_MATCHER,
    MATCHED,
    NON_SPEC,
    UNKNOWN_SPEC_SCHEMA,
    ProfileMatchResult,
    SchemaProfileMatcher,
    SpecificationSchemaProfile,
)


SUPPORTED = "supported"
AMBIGUOUS = "ambiguous"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown_spec_schema"


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
    observed_schema: dict[str, Any] | None = None
    profile_match: dict[str, Any] | None = None
    critical_field_policy: dict[str, str] | None = None
    production_authoritative: bool = False

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
            "observed_schema": dict(self.observed_schema) if self.observed_schema else None,
            "profile_match": dict(self.profile_match) if self.profile_match else None,
            "critical_field_policy": dict(self.critical_field_policy or {}),
            "production_authoritative": self.production_authoritative,
        }
        if self.family_assessment is not None:
            result["family"] = dict(self.family_assessment)
        return result


def _family_value(family: Any, key: str, default: Any = None) -> Any:
    if family is None:
        return default
    if isinstance(family, Mapping):
        return family.get(key, default)
    return getattr(family, key, default)


def _mapping_fields(mapping: HeaderMappingResult) -> set[str]:
    return {str(field) for values in mapping.mapping.values() for field in values}


class SchemaGate:
    """Decide applicability using the selected profile, never global core fields."""

    def __init__(self, profile_matcher: SchemaProfileMatcher | None = None) -> None:
        self.profile_matcher = profile_matcher or DEFAULT_SCHEMA_PROFILE_MATCHER

    def assess(
        self,
        *,
        column_count: int,
        mapping: HeaderMappingResult,
        family: TableFamilyAssessment,
        structural: SchemaGateEvidence | None = None,
        observed_schema: ObservedSchema | None = None,
        profile_match: ProfileMatchResult | None = None,
    ) -> SchemaAssessment:
        evidence = structural or SchemaGateEvidence()
        observed = observed_schema or ObservedSchema.from_mapping_result(mapping, column_count)
        match = profile_match or self.profile_matcher.match(
            observed,
            family=family,
            structural=evidence.as_dict(),
        )
        mapped_fields = set(observed.mapped_concepts) or _mapping_fields(mapping)
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
                inverse.setdefault(str(field), []).append(column)
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
        if not mapped_fields:
            reasons.append("no_observed_semantic_concepts")
        if coverage < 0.45:
            reasons.append("semantic_coverage_too_low")
        if evidence.header_body_conflict or "header_body_conflict" in mapping.reasons:
            reasons.append("header_body_conflict")
        reasons.extend(evidence.structural_reasons)
        reasons.extend(structural_blockers)
        negative_evidence = _family_value(family, "negative_evidence", ()) or ()
        if negative_evidence:
            codes = {
                str(item.code if hasattr(item, "code") else item.get("code"))
                for item in negative_evidence
            }
            reasons.append("negative_document_family:" + ",".join(sorted(codes)))
        reasons = list(dict.fromkeys(reasons))

        family_data = family.as_dict() if hasattr(family, "as_dict") else dict(family or {})
        base = dict(
            coverage_score=coverage,
            uniqueness_score=uniqueness,
            header_consistency=header_consistency,
            mapped_fields=tuple(sorted(mapped_fields)),
            family_assessment=family_data,
            mapping_status=mapping.status,
            observed_schema=observed.as_dict(),
            profile_match=match.as_dict(),
        )

        if column_count <= 0:
            return SchemaAssessment(
                UNSUPPORTED,
                0.0,
                0.0,
                0.0,
                (),
                ("invalid_column_count",),
                **{key: value for key, value in base.items() if key not in {"coverage_score", "uniqueness_score", "header_consistency", "mapped_fields"}},
            )
        if _family_value(family, "family", "") == OTHER_TABLE or match.status == NON_SPEC:
            reasons.append("confirmed_other_table")
            return SchemaAssessment(
                UNSUPPORTED,
                **base,
                reasons=tuple(dict.fromkeys(reasons)),
                decision_reasons=("confirmed_other_table",),
            )
        schema_conflict = bool(
            illegal
            or evidence.header_body_conflict
            or "header_body_conflict" in mapping.reasons
            or not evidence.safe_relevant_structure
            or structural_blockers
        )
        if match.status == UNKNOWN_SPEC_SCHEMA and mapping.trusted:
            reasons.append("unknown_specification_profile")
            if schema_conflict:
                return SchemaAssessment(
                    AMBIGUOUS,
                    **base,
                    reasons=tuple(dict.fromkeys(reasons)),
                    decision_reasons=("critical_schema_evidence_incomplete",),
                )
            return SchemaAssessment(
                UNKNOWN,
                **base,
                reasons=tuple(dict.fromkeys(reasons)),
                decision_reasons=("unknown_specification_profile",),
            )
        if mapping.status != "trusted":
            return SchemaAssessment(
                AMBIGUOUS,
                **base,
                reasons=tuple(dict.fromkeys(reasons)),
                decision_reasons=("mapping_not_trusted",),
            )
        if match.status == AMBIGUOUS_PROFILE:
            return SchemaAssessment(
                AMBIGUOUS,
                **base,
                reasons=tuple(dict.fromkeys(reasons + list(match.reasons))),
                decision_reasons=("profile_match_ambiguous",),
            )
        profile: SpecificationSchemaProfile | None = match.selected_profile
        if match.status != MATCHED or profile is None:
            status = UNSUPPORTED if "semantic_coverage_too_low" in reasons else AMBIGUOUS
            return SchemaAssessment(
                status,
                **base,
                reasons=tuple(dict.fromkeys(reasons + list(match.reasons))),
                decision_reasons=("profile_not_matched",),
            )

        missing_profile = sorted(profile.required_concepts - mapped_fields)
        forbidden_profile = [
            "+".join(item)
            for item in profile.forbidden_combinations
            if set(item).issubset(mapped_fields)
        ]
        if missing_profile:
            reasons.append("missing_profile_concepts:" + ",".join(missing_profile))
        if forbidden_profile:
            reasons.append("forbidden_profile_combination:" + ",".join(forbidden_profile))
        if coverage < 0.45:
            return SchemaAssessment(
                UNSUPPORTED,
                **base,
                reasons=tuple(dict.fromkeys(reasons)),
                decision_reasons=("semantic_coverage_too_low",),
            )
        if missing_profile or forbidden_profile or illegal or evidence.header_body_conflict or not evidence.safe_relevant_structure or structural_blockers:
            return SchemaAssessment(
                AMBIGUOUS,
                **base,
                reasons=tuple(dict.fromkeys(reasons)),
                decision_reasons=("critical_schema_evidence_incomplete",),
                supported_variant=profile.profile_id,
                critical_field_policy={
                    str(key): str(value.value if hasattr(value, "value") else value)
                    for key, value in profile.critical_field_policy.items()
                },
                production_authoritative=profile.production_authoritative,
            )

        if _family_value(family, "family", "") == AMBIGUOUS_FAMILY:
            # Missing family context is not enough to promote a short generic
            # equipment list.  The deliberately narrow canonical-header path
            # belongs here, after a unique profile match, and requires a rich
            # observed header rather than a duplicated mapper threshold.
            canonical_ready = (
                len(mapped_fields) >= 6
                and profile.required_concepts.issubset(mapped_fields)
                and "semantic_coverage_too_low" not in reasons
                and not schema_conflict
                and not negative_evidence
            )
            if not canonical_ready:
                reasons.append("family_evidence_missing")
                return SchemaAssessment(
                    AMBIGUOUS,
                    **base,
                    reasons=tuple(dict.fromkeys(reasons)),
                    decision_reasons=("family_context_missing",),
                    supported_variant=profile.profile_id,
                    critical_field_policy={
                        str(key): str(value.value if hasattr(value, "value") else value)
                        for key, value in profile.critical_field_policy.items()
                    },
                    production_authoritative=profile.production_authoritative,
                )
            decision = ("canonical_header_without_family_context",)
            variant = "canonical_header"
        elif _family_value(family, "family", "") == AMBIGUOUS_FAMILY:
            decision = ("profile_matched_without_family_context",)
            variant = profile.profile_id
        else:
            decision = ("profile_matched",)
            variant = profile.profile_id
        return SchemaAssessment(
            SUPPORTED,
            **base,
            reasons=tuple(dict.fromkeys(reasons)),
            decision_reasons=decision,
            supported_variant=variant,
            critical_field_policy={
                str(key): str(value.value if hasattr(value, "value") else value)
                for key, value in profile.critical_field_policy.items()
            },
            production_authoritative=profile.production_authoritative,
        )

    evaluate = assess


DEFAULT_SCHEMA_GATE = SchemaGate()


__all__ = [
    "AMBIGUOUS",
    "DEFAULT_SCHEMA_GATE",
    "SchemaAssessment",
    "SchemaGate",
    "SchemaGateEvidence",
    "SUPPORTED",
    "UNKNOWN",
    "UNSUPPORTED",
]
