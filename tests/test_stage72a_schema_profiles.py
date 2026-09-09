from __future__ import annotations

from averon_import.services.ocr.semantics.context_evidence import (
    BoundedFamilyContext,
    ContextRegionEvidence,
    bounded_context_from_words,
)
from averon_import.services.ocr.semantics.family_classifier import (
    AMBIGUOUS as AMBIGUOUS_FAMILY,
    OTHER_TABLE,
    TableFamilyClassifier,
)
from averon_import.services.ocr.semantics.header_evidence import HeaderSourceCell
from averon_import.services.ocr.semantics.observed_schema import (
    ObservedSchema,
    TypedMeasurement,
)
from averon_import.services.ocr.semantics.schema_gate import (
    DEFAULT_SCHEMA_GATE,
    AMBIGUOUS as AMBIGUOUS_SCHEMA,
    SchemaGateEvidence,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
)
from averon_import.services.ocr.semantics.schema_profiles import (
    AMBIGUOUS,
    DEFAULT_SCHEMA_PROFILE_MATCHER,
    ELEMENT_ASSEMBLY_SPECIFICATION,
    MATCHED,
    NON_SPEC,
    UNKNOWN_SPEC_SCHEMA,
    SchemaProfileMatcher,
    SpecificationSchemaProfile,
)
from averon_import.services.ocr.semantics import map_semantic_header


def _observed(
    *concepts: str,
    status: str = "trusted",
    provenance: tuple[dict, ...] = (),
) -> ObservedSchema:
    return ObservedSchema.from_concepts(
        concepts,
        mapping_status=status,
        provenance=provenance,
    )


def _context(text: str, kind: str = "table_caption") -> BoundedFamilyContext:
    return BoundedFamilyContext((ContextRegionEvidence(kind, (0, 0, 100, 20), text),))


def test_sc1_equipment_profile_requires_name_quantity_and_unit():
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(_observed("name", "quantity", "unit"))
    assert result.status == MATCHED
    assert result.selected_profile is not None
    assert result.selected_profile.profile_id == "equipment_material_specification"


def test_sc2_element_profile_matches_without_unit():
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(
        _observed("position", "designation", "name", "quantity", "mass_per_unit", "note")
    )
    assert result.status == MATCHED
    assert result.selected_profile is not None
    assert result.selected_profile.profile_id == "element_assembly_specification"


def test_sc3_element_profile_without_quantity_is_not_supported():
    observed = _observed("position", "designation", "name", "mass_per_unit", "note")
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed)
    assert result.status == UNKNOWN_SPEC_SCHEMA
    family = TableFamilyClassifier().assess(BoundedFamilyContext())
    mapping = _mapping_for_observed(observed)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=5, mapping=mapping, family=family, observed_schema=observed, profile_match=result
    )
    assert assessment.status == UNKNOWN


def test_sc4_material_measure_profile_is_a_typed_measurement_candidate():
    measurement = TypedMeasurement.parse("2,95 м3", role="material")
    assert measurement is not None
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(_observed("name", "typed_measurement"))
    assert result.status == MATCHED
    assert result.selected_profile is not None
    assert result.selected_profile.profile_id == "material_measure_specification"


def test_sc5_multiple_profiles_are_ambiguous_without_numeric_score_tie_breaking():
    first = SpecificationSchemaProfile("first", frozenset({"name", "quantity"}))
    second = SpecificationSchemaProfile("second", frozenset({"name", "quantity"}))
    result = SchemaProfileMatcher((first, second)).match(_observed("name", "quantity"))
    assert result.status == AMBIGUOUS
    assert result.selected_profile is None
    assert result.candidates == ("first", "second")


def test_sc6_specification_like_unknown_schema_is_distinguished_from_no_spec():
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(
        _observed("name", "position", "mass_total", "note")
    )
    assert result.status == UNKNOWN_SPEC_SCHEMA


def test_sc7_other_table_cannot_be_promoted_by_matching_concepts():
    family = TableFamilyClassifier().assess(_context("Кабельный журнал"))
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(
        _observed("name", "quantity", "unit"), family=family
    )
    assert family.family == OTHER_TABLE
    assert result.status == NON_SPEC
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=3,
        mapping=_mapping_for_observed(_observed("name", "quantity", "unit")),
        family=family,
        observed_schema=_observed("name", "quantity", "unit"),
        profile_match=result,
    )
    assert assessment.status == UNSUPPORTED


def test_sc8_element_profile_marks_unit_not_applicable():
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(_observed("designation", "name", "quantity"))
    assert result.status == MATCHED
    assert result.selected_profile is not None
    assert result.selected_profile.critical_field_policy["unit"] == "NOT_APPLICABLE"


def test_sc9_equipment_profile_without_unit_remains_incomplete():
    observed = _observed("name", "quantity", "type_mark")
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed)
    assert result.status in {AMBIGUOUS, UNKNOWN_SPEC_SCHEMA}
    family = TableFamilyClassifier().assess(_context("Спецификация оборудования"))
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=3,
        mapping=_mapping_for_observed(observed),
        family=family,
        observed_schema=observed,
        profile_match=result,
    )
    assert assessment.status != SUPPORTED


def test_sc10_designation_is_not_flattened_to_type_mark():
    source = _header_cells("Поз.", "Обозначение", "Наименование", "Кол.", "Масса, ед., кг", "Примечание")
    mapping = map_semantic_header(source, 6)
    observed = ObservedSchema.from_mapping_result(mapping, 6)
    assert "designation" in observed.mapped_concepts
    assert "type_mark" not in observed.mapped_concepts


def test_sc11_mass_total_and_mass_per_unit_are_distinct():
    assert _observed("name", "mass_per_unit").has("mass_per_unit")
    assert _observed("name", "mass_total").has("mass_total")
    assert not _observed("name", "mass_total").has("mass_per_unit")


def test_sc12_detail_position_is_scoped_and_not_global_position():
    observed = _observed("detail_position", "name", "quantity", "designation")
    assert observed.has("detail_position")
    assert not observed.has("position")
    result = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed)
    assert result.status == MATCHED


def test_sc13_near_table_caption_is_bounded_family_evidence():
    words = [
        {"text": "Спецификация", "vertices": [
            {"x": 10, "y": 100}, {"x": 150, "y": 100},
            {"x": 150, "y": 110}, {"x": 10, "y": 110},
        ]},
    ]
    context = bounded_context_from_words((0, 140, 400, 900), words, page_height=1000)
    assert context.regions
    assert context.regions[-1].kind == "table_caption"
    assert TableFamilyClassifier().assess(context).family != AMBIGUOUS_FAMILY


def test_sc14_whole_page_full_text_is_not_a_family_authority():
    # The family API accepts only BoundedFamilyContext.  Unbounded page text
    # is intentionally not representable in this call contract.
    assessment = TableFamilyClassifier().assess(BoundedFamilyContext())
    assert assessment.family == AMBIGUOUS_FAMILY
    assert "fullText" not in assessment.as_dict()


def test_sc15_provider_provenance_does_not_change_profile_result():
    first = _observed("position", "designation", "name", "quantity", provenance=({"provider": "a"},))
    second = _observed("position", "designation", "name", "quantity", provenance=({"provider": "b"},))
    assert DEFAULT_SCHEMA_PROFILE_MATCHER.match(first).status == MATCHED
    assert DEFAULT_SCHEMA_PROFILE_MATCHER.match(first).selected_profile == DEFAULT_SCHEMA_PROFILE_MATCHER.match(second).selected_profile


def test_p14_header_observation_matches_element_profile_without_inventing_unit():
    mapping = map_semantic_header(
        _header_cells("Поз.", "Обозначение", "Наименование", "Кол.", "Масса, ед., кг", "Примечание"),
        6,
    )
    observed = ObservedSchema.from_mapping_result(mapping, 6)
    family = TableFamilyClassifier().assess(_context("Спецификация элементов"))
    profile = DEFAULT_SCHEMA_PROFILE_MATCHER.match(observed, family=family)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=6,
        mapping=mapping,
        family=family,
        observed_schema=observed,
        profile_match=profile,
    )
    assert observed.mapped_concepts == (
        "designation", "mass_per_unit", "name", "note", "position", "quantity"
    )
    assert profile.status == MATCHED
    assert profile.selected_profile is not None
    assert profile.selected_profile.profile_id == ELEMENT_ASSEMBLY_SPECIFICATION.profile_id
    assert assessment.status == SUPPORTED
    assert assessment.production_authoritative is False
    assert assessment.critical_field_policy["unit"] == "NOT_APPLICABLE"
    assert "unit" not in observed.mapped_concepts


def _header_cells(*headers: str) -> tuple[HeaderSourceCell, ...]:
    return tuple(
        HeaderSourceCell(
            physical_row=0,
            physical_column=column,
            bbox={"left": column, "top": 0, "width": 1, "height": 1},
            raw_text=header,
            provenance=({"source": "stage72a_fixture"},),
        )
        for column, header in enumerate(headers)
    )


def _mapping_for_observed(observed: ObservedSchema):
    from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult

    fields = {index: (concept,) for index, concept in enumerate(observed.mapped_concepts)}
    return HeaderMappingResult(
        status=observed.mapping_status,
        header_rows=(0,),
        mapping=fields,
        candidates_by_column={},
        best_score=1.0,
        second_best_score=0.0,
        assignment_margin=observed.assignment_margin,
        unmapped_columns=(),
        missing_core_fields=(),
        reasons=(),
    )
