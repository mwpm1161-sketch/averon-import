from __future__ import annotations

from averon_import.services.ocr.semantics import (
    DEFAULT_SCHEMA_GATE,
    DEFAULT_SCHEMA_PROFILE_MATCHER,
    ELEMENT_ASSEMBLY_SPECIFICATION,
    HeaderSourceCell,
    ObservedSchema,
    SchemaGate,
    SchemaProfileMatcher,
    SpecificationSchemaProfile,
    TableAnalysisContext,
    TableFamilyClassifier,
    TypedMeasurement,
    map_semantic_header,
)
from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
)
from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS,
    OTHER_TABLE,
    SUPPORTED_SPECIFICATION,
    FamilyEvidence,
    TableFamilyAssessment,
)
from averon_import.services.ocr.semantics.schema_gate import AMBIGUOUS as SCHEMA_AMBIGUOUS
from averon_import.services.ocr.semantics.schema_profiles import MATCHED, NON_SPEC
from averon_import.services.ocr.table_ir import PhysicalTableIR, PhysicalTableRef


def _cells(*headers: str) -> tuple[HeaderSourceCell, ...]:
    return tuple(
        HeaderSourceCell(
            physical_row=0,
            physical_column=index,
            bbox={"left": index, "top": 0, "width": 1, "height": 1},
            raw_text=header,
            provenance=({"source": "stage72a_hardening"},),
        )
        for index, header in enumerate(headers)
    )


def _family(text: str = "Спецификация"):
    return TableFamilyClassifier().assess(
        BoundedFamilyContext(
            regions=(ContextRegionEvidence("table_caption", (0, 0, 10, 1), text),)
        )
    )


def _table() -> PhysicalTableIR:
    return PhysicalTableIR(
        ref=PhysicalTableRef(page_number=1, table_index=0, document_id="stage72a"),
        bounds=(0, 0, 10, 10),
        x_boundaries=(0, 10),
        y_boundaries=(0, 10),
    )


def test_h1_strong_two_column_mapping_is_trusted_observed_evidence():
    result = map_semantic_header(_cells("Количество", "Единица измерения"), 2)

    assert result.status == "trusted"
    assert set(result.mapping.values()) == {("quantity",), ("unit",)}


def test_h2_trusted_two_column_mapping_without_profile_fails_closed():
    mapping = map_semantic_header(_cells("Количество", "Единица измерения"), 2)
    observed = ObservedSchema.from_mapping_result(mapping, 2)
    profile = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=2,
        mapping=mapping,
        family=_family(),
        observed_schema=observed,
        profile_match=profile,
    )

    assert mapping.trusted
    assert profile.selected_profile is None
    assert assessment.status != "supported"


def test_h3_strong_three_column_mapping_reaches_custom_profile():
    mapping = map_semantic_header(_cells("Наименование", "Количество", "Единица измерения"), 3)
    observed = ObservedSchema.from_mapping_result(mapping, 3)
    custom = SpecificationSchemaProfile(
        "three_field_fixture",
        frozenset({"name", "quantity", "unit"}),
        production_authoritative=False,
    )
    matcher = SchemaProfileMatcher((custom,))
    profile = matcher.match(observed, family=_family())
    assessment = SchemaGate(matcher).assess(
        column_count=3,
        mapping=mapping,
        family=_family(),
        observed_schema=observed,
        profile_match=profile,
    )

    assert mapping.status == "trusted"
    assert profile.status == MATCHED
    assert profile.selected_profile == custom
    assert assessment.status == "supported"


def test_h4_mapper_does_not_require_name_as_global_completeness_rule():
    result = map_semantic_header(_cells("Количество"), 1)

    assert result.status == "trusted"
    assert result.mapping == {0: ("quantity",)}


def test_h5_richer_coherent_region_wins_over_accidental_single_token():
    cells = (
        HeaderSourceCell(0, 0, {"left": 0, "top": 0}, "Количество"),
        HeaderSourceCell(2, 0, {"left": 0, "top": 2}, "Наименование"),
        HeaderSourceCell(2, 1, {"left": 1, "top": 2}, "Количество"),
        HeaderSourceCell(2, 2, {"left": 2, "top": 2}, "Единица измерения"),
    )
    result = map_semantic_header(cells, 3)

    assert result.status == "trusted"
    assert result.header_rows == (2,)
    assert len(result.mapping) == 3


def test_h6_confirmed_other_table_is_non_spec():
    family = _family("Кабельный журнал")
    observed = ObservedSchema.from_concepts(("name", "quantity", "unit"))

    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=family)

    assert family.family == OTHER_TABLE
    assert result.status == NON_SPEC


def test_h7_conflicting_family_evidence_is_ambiguous_not_non_spec():
    family = TableFamilyAssessment(
        family=AMBIGUOUS,
        status=AMBIGUOUS,
        score=0.0,
        evidence_strength="conflicting",
        positive_evidence=(FamilyEvidence("specification_family", "positive", "table_caption", "Спецификация", "strong"),),
        negative_evidence=(FamilyEvidence("cable_journal", "negative", "table_caption", "Кабельный журнал", "strong"),),
        reasons=("conflicting_family_evidence",),
    )
    observed = ObservedSchema.from_concepts(("name", "quantity", "unit"))

    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=family)

    assert result.status == "AMBIGUOUS"
    assert result.status != NON_SPEC


def test_h8_observed_schema_is_immutable_snapshot_in_analysis_context():
    source = {"mapped_concepts": ["name"], "nested": {"confidence": 1}}
    context = TableAnalysisContext(_table(), observed_schema=source)

    source["mapped_concepts"].append("quantity")
    source["nested"]["confidence"] = 0

    assert context.observed_schema["mapped_concepts"] == ("name",)
    assert context.observed_schema["nested"]["confidence"] == 1


def test_h9_profile_match_is_immutable_snapshot_in_analysis_context():
    source = {"status": "MATCHED", "selected_profile": {"profile_id": "fixture"}}
    context = TableAnalysisContext(_table(), profile_match=source)

    source["selected_profile"]["profile_id"] = "changed"

    assert context.profile_match["selected_profile"]["profile_id"] == "fixture"


def test_h10_header_only_pipeline_does_not_fabricate_typed_measurement():
    mapping = map_semantic_header(_cells("Наименование", "Количество", "Единица измерения"), 3)
    observed = ObservedSchema.from_mapping_result(mapping, 3)

    assert "typed_measurement" not in observed.mapped_concepts
    assert TypedMeasurement.parse("2,95 м3") is not None
    assert "typed_measurement" not in observed.as_dict()


def test_h11_p14_profile_remains_shadow_only_and_unit_not_applicable():
    observed = ObservedSchema.from_concepts(
        ("position", "designation", "name", "quantity", "mass_per_unit", "note")
    )
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=_family())

    assert result.status == MATCHED
    assert result.selected_profile == ELEMENT_ASSEMBLY_SPECIFICATION
    assert result.selected_profile.production_authoritative is False
    assert result.selected_profile.critical_field_policy["unit"].value == "NOT_APPLICABLE"


def test_h12_equipment_profile_remains_authoritative():
    mapping = map_semantic_header(_cells("Наименование", "Количество", "Единица измерения"), 3)
    observed = ObservedSchema.from_mapping_result(mapping, 3)
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=_family())

    assert result.status == MATCHED
    assert result.selected_profile is not None
    assert result.selected_profile.profile_id == "equipment_material_specification"
    assert result.selected_profile.production_authoritative is True
