"""Provider-neutral specification profiles and deterministic profile matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .observed_schema import ObservedSchema


MATCHED = "MATCHED"
AMBIGUOUS = "AMBIGUOUS"
UNKNOWN_SPEC_SCHEMA = "UNKNOWN_SPEC_SCHEMA"
NON_SPEC = "NON_SPEC"


class Applicability(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONDITIONAL = "CONDITIONAL"


@dataclass(frozen=True, slots=True)
class RowContract:
    row_type: str
    required_concepts: tuple[str, ...] = ()
    optional_concepts: tuple[str, ...] = ()
    critical_field_policy: Mapping[str, Applicability | str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_type": self.row_type,
            "required_concepts": list(self.required_concepts),
            "optional_concepts": list(self.optional_concepts),
            "critical_field_policy": {
                key: Applicability(value).value if not isinstance(value, Applicability) else value.value
                for key, value in self.critical_field_policy.items()
            },
        }


@dataclass(frozen=True, slots=True)
class SpecificationSchemaProfile:
    profile_id: str
    required_concepts: frozenset[str]
    optional_concepts: frozenset[str] = frozenset()
    supporting_concepts: frozenset[str] = frozenset()
    minimum_supporting_concepts: int = 0
    forbidden_combinations: tuple[tuple[str, ...], ...] = ()
    critical_field_policy: Mapping[str, Applicability | str] = field(default_factory=dict)
    row_contracts: tuple[RowContract, ...] = ()
    hierarchy_policy: Mapping[str, Any] = field(default_factory=dict)
    projection_policy: Mapping[str, Any] = field(default_factory=dict)
    profile_evidence: tuple[str, ...] = ()
    production_authoritative: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "required_concepts": sorted(self.required_concepts),
            "optional_concepts": sorted(self.optional_concepts),
            "supporting_concepts": sorted(self.supporting_concepts),
            "minimum_supporting_concepts": self.minimum_supporting_concepts,
            "forbidden_combinations": [list(item) for item in self.forbidden_combinations],
            "critical_field_policy": {
                key: Applicability(value).value if not isinstance(value, Applicability) else value.value
                for key, value in self.critical_field_policy.items()
            },
            "row_contracts": [contract.as_dict() for contract in self.row_contracts],
            "hierarchy_policy": dict(self.hierarchy_policy),
            "projection_policy": dict(self.projection_policy),
            "profile_evidence": list(self.profile_evidence),
            "production_authoritative": self.production_authoritative,
        }


EQUIPMENT_MATERIAL_SPECIFICATION = SpecificationSchemaProfile(
    profile_id="equipment_material_specification",
    required_concepts=frozenset({"name", "quantity", "unit"}),
    optional_concepts=frozenset({
        "position", "detail_position", "type_mark", "product_mark", "designation",
        "code", "manufacturer", "mass_per_unit", "mass_total", "note",
    }),
    critical_field_policy={
        "quantity": Applicability.REQUIRED,
        "unit": Applicability.REQUIRED,
        "mass_per_unit": Applicability.CONDITIONAL,
    },
    row_contracts=(RowContract(
        "ITEM",
        required_concepts=("name", "quantity", "unit"),
        optional_concepts=("position", "type_mark", "designation", "code", "manufacturer", "mass_per_unit", "note"),
        critical_field_policy={"quantity": Applicability.REQUIRED, "unit": Applicability.REQUIRED},
    ),),
    hierarchy_policy={"continuations": True, "group_headers": True},
    projection_policy={"canonical_output": "BASE_COLUMNS"},
    profile_evidence=("name_quantity_unit",),
    production_authoritative=True,
)


ELEMENT_ASSEMBLY_SPECIFICATION = SpecificationSchemaProfile(
    profile_id="element_assembly_specification",
    required_concepts=frozenset({"name", "quantity"}),
    optional_concepts=frozenset({"position", "detail_position", "product_mark", "type_mark", "mass_per_unit", "mass_total", "note"}),
    supporting_concepts=frozenset({"position", "detail_position", "designation", "mass_per_unit", "mass_total", "note"}),
    minimum_supporting_concepts=1,
    forbidden_combinations=(("unit", "quantity"),),
    critical_field_policy={
        "quantity": Applicability.REQUIRED,
        "unit": Applicability.NOT_APPLICABLE,
        "mass_per_unit": Applicability.CONDITIONAL,
        "mass_total": Applicability.CONDITIONAL,
    },
    row_contracts=(
        RowContract("ITEM", required_concepts=("name", "quantity"), optional_concepts=("designation", "position", "mass_per_unit", "note"), critical_field_policy={"quantity": Applicability.REQUIRED, "unit": Applicability.NOT_APPLICABLE}),
        RowContract("ASSEMBLY_PARENT", required_concepts=("name", "quantity"), optional_concepts=("designation", "mass_total"), critical_field_policy={"quantity": Applicability.REQUIRED, "unit": Applicability.NOT_APPLICABLE}),
        RowContract("DETAIL", required_concepts=("name", "quantity"), optional_concepts=("detail_position", "designation", "mass_per_unit", "note"), critical_field_policy={"quantity": Applicability.REQUIRED, "unit": Applicability.NOT_APPLICABLE}),
        RowContract("GROUP_HEADER", required_concepts=("name",), critical_field_policy={"quantity": Applicability.NOT_APPLICABLE, "unit": Applicability.NOT_APPLICABLE}),
        RowContract("NOTE", required_concepts=(), optional_concepts=("note",), critical_field_policy={"quantity": Applicability.NOT_APPLICABLE, "unit": Applicability.NOT_APPLICABLE}),
    ),
    hierarchy_policy={"parent_detail": True, "scoped_positions": True, "group_headers": True},
    projection_policy={"canonical_output": "shadow_until_row_contract_verified"},
    profile_evidence=("designation_name_quantity", "unit_not_required"),
    production_authoritative=False,
)


MATERIAL_MEASURE_SPECIFICATION = SpecificationSchemaProfile(
    profile_id="material_measure_specification",
    required_concepts=frozenset({"name", "typed_measurement"}),
    optional_concepts=frozenset({"position", "designation", "note", "mass_per_unit"}),
    critical_field_policy={
        "typed_measurement": Applicability.REQUIRED,
        "quantity": Applicability.NOT_APPLICABLE,
        "unit": Applicability.NOT_APPLICABLE,
        "mass_per_unit": Applicability.NOT_APPLICABLE,
    },
    row_contracts=(RowContract("MATERIAL", required_concepts=("name", "typed_measurement"), critical_field_policy={"typed_measurement": Applicability.REQUIRED}),),
    hierarchy_policy={"group_headers": True, "typed_measurements": True},
    projection_policy={"canonical_output": "typed_measurement_review"},
    profile_evidence=("name_typed_measurement",),
    production_authoritative=False,
)


DEFAULT_SCHEMA_PROFILES: tuple[SpecificationSchemaProfile, ...] = (
    EQUIPMENT_MATERIAL_SPECIFICATION,
    ELEMENT_ASSEMBLY_SPECIFICATION,
    MATERIAL_MEASURE_SPECIFICATION,
)


@dataclass(frozen=True, slots=True)
class ProfileMatchResult:
    status: str
    selected_profile: SpecificationSchemaProfile | None = None
    candidates: tuple[str, ...] = ()
    positive_evidence: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    strength: str = "missing"
    margin: float = 0.0
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_profile": self.selected_profile.as_dict() if self.selected_profile else None,
            "candidates": list(self.candidates),
            "positive_evidence": list(self.positive_evidence),
            "contradictions": list(self.contradictions),
            "strength": self.strength,
            "margin": round(float(self.margin), 4),
            "reasons": list(self.reasons),
        }


def _family_value(family: Any, key: str, default: Any = None) -> Any:
    if family is None:
        return default
    if isinstance(family, Mapping):
        return family.get(key, default)
    return getattr(family, key, default)


class SchemaProfileMatcher:
    """Match observed concepts using explicit constraints, never score alone."""

    def __init__(self, profiles: tuple[SpecificationSchemaProfile, ...] = DEFAULT_SCHEMA_PROFILES) -> None:
        self.profiles = tuple(profiles)

    @staticmethod
    def _forbidden(profile: SpecificationSchemaProfile, concepts: set[str]) -> tuple[str, ...]:
        return tuple(
            "+".join(combination)
            for combination in profile.forbidden_combinations
            if set(combination).issubset(concepts)
        )

    @staticmethod
    def _looks_specification_like(observed: ObservedSchema) -> bool:
        concepts = set(observed.mapped_concepts)
        if "name" not in concepts:
            return False
        return len(concepts.intersection({
            "position", "detail_position", "designation", "product_mark", "type_mark",
            "quantity", "unit", "mass_per_unit", "mass_total", "note", "typed_measurement",
        })) >= 2

    def match(
        self,
        observed: ObservedSchema,
        *,
        family: Any = None,
        bounded_family_evidence: Any = None,
        structural: Mapping[str, Any] | None = None,
    ) -> ProfileMatchResult:
        family_name = str(_family_value(family, "family", ""))
        negative = _family_value(family, "negative_evidence", ()) or ()
        # A confirmed OTHER_TABLE is a hard negative.  An AMBIGUOUS family
        # with both positive and negative bounded evidence is reviewable, not
        # a reason to discard a potentially legitimate specification.
        if family_name == "OTHER_TABLE":
            return ProfileMatchResult(
                NON_SPEC,
                candidates=(),
                contradictions=("confirmed_other_table",),
                strength="strong",
                reasons=("explicit_other_table",),
            )
        if family_name == "AMBIGUOUS" and negative:
            return ProfileMatchResult(
                AMBIGUOUS,
                candidates=(),
                contradictions=("conflicting_family_evidence",),
                strength="conflicting",
                reasons=("conflicting_family_evidence",),
            )

        concepts = set(observed.mapped_concepts)
        eligible: list[SpecificationSchemaProfile] = []
        contradictions: list[str] = []
        evidence: list[str] = []
        for profile in self.profiles:
            missing = sorted(profile.required_concepts - concepts)
            supporting = concepts.intersection(profile.supporting_concepts)
            missing_support = bool(
                profile.minimum_supporting_concepts
                and len(supporting) < profile.minimum_supporting_concepts
            )
            forbidden = self._forbidden(profile, concepts)
            if forbidden:
                contradictions.extend(f"{profile.profile_id}:{item}" for item in forbidden)
                continue
            if missing or missing_support:
                continue
            eligible.append(profile)
            evidence.extend(f"{profile.profile_id}:{item}" for item in profile.profile_evidence)

        if len(eligible) == 1:
            selected = eligible[0]
            return ProfileMatchResult(
                MATCHED,
                selected_profile=selected,
                candidates=(selected.profile_id,),
                positive_evidence=tuple(dict.fromkeys(evidence)),
                contradictions=tuple(dict.fromkeys(contradictions)),
                strength="strong",
                margin=1.0,
                reasons=("unique_required_concept_match",),
            )
        if len(eligible) > 1:
            return ProfileMatchResult(
                AMBIGUOUS,
                candidates=tuple(profile.profile_id for profile in eligible),
                positive_evidence=tuple(dict.fromkeys(evidence)),
                contradictions=tuple(dict.fromkeys(contradictions)),
                strength="strong",
                margin=0.0,
                reasons=("multiple_materially_plausible_profiles",),
            )
        if self._looks_specification_like(observed):
            return ProfileMatchResult(
                UNKNOWN_SPEC_SCHEMA,
                candidates=(),
                positive_evidence=("specification_like_header_evidence",),
                contradictions=tuple(dict.fromkeys(contradictions)),
                strength="strong",
                reasons=("no_known_profile_matches",),
            )
        return ProfileMatchResult(
            AMBIGUOUS,
            candidates=(),
            contradictions=tuple(dict.fromkeys(contradictions)),
            strength="missing",
            reasons=("insufficient_schema_evidence",),
        )


DEFAULT_SCHEMA_PROFILE_MATCHER = SchemaProfileMatcher()


__all__ = [
    "AMBIGUOUS",
    "Applicability",
    "DEFAULT_SCHEMA_PROFILES",
    "DEFAULT_SCHEMA_PROFILE_MATCHER",
    "ELEMENT_ASSEMBLY_SPECIFICATION",
    "EQUIPMENT_MATERIAL_SPECIFICATION",
    "MATERIAL_MEASURE_SPECIFICATION",
    "MATCHED",
    "NON_SPEC",
    "ProfileMatchResult",
    "RowContract",
    "SchemaProfileMatcher",
    "SpecificationSchemaProfile",
    "UNKNOWN_SPEC_SCHEMA",
]
