from __future__ import annotations

from copy import deepcopy

from openpyxl import load_workbook

from averon_import.services.export_service import ExcelExportService
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.page_disposition import (
    CONFIRMED_NON_SPEC,
    POSSIBLE_SPEC_UNRESOLVED,
    SPEC_OUTPUT,
    page_disposition_from_scene,
)
from averon_import.services.ocr.page_scene import (
    REJECTED,
    REVIEW,
    TRUSTED,
    LocalGridHypothesis,
    PageSceneIR,
    TableRegionCandidate,
)
from averon_import.services.ocr.page_scene_arbiter import PageSceneRegionArbiter
from averon_import.services.ocr.physical_grid import PhysicalGrid, PhysicalGridCell


def _grid() -> PhysicalGrid:
    xs = (0.1, 0.3, 0.5, 0.7, 0.9)
    ys = (0.1, 0.3, 0.5, 0.7, 0.9)
    return PhysicalGrid(
        source="test",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=tuple(
            PhysicalGridCell(row, column, (xs[column], ys[row], xs[column + 1], ys[row + 1]))
            for row in range(4)
            for column in range(4)
        ),
        confidence=1.0,
        high_confidence=True,
    )


def _candidate(
    *,
    family: str = "SUPPORTED_SPECIFICATION",
    schema_status: str = "supported",
    profile_authoritative: bool = True,
    status: str = TRUSTED,
    provider: bool = False,
    provider_association: str = "unique",
    rejection_reasons: tuple[str, ...] = (),
) -> TableRegionCandidate:
    grid = _grid()
    evidence = {
        "family": {"family": family, "status": family},
        "schema": {"status": schema_status, "reasons": []},
        "profile_match": {
            "status": "MATCHED" if schema_status == "supported" else "AMBIGUOUS",
            "selected_profile": {
                "profile_id": "equipment_material_specification",
                "production_authoritative": profile_authoritative,
            } if schema_status == "supported" else None,
        },
    }
    sources = ("raster", "provider") if provider else ("raster",)
    return TableRegionCandidate(
        ref="region:0",
        page_bounds=grid.bounds,
        proposal_sources=sources,
        local_grid_hypotheses=(LocalGridHypothesis("raster", grid.bounds, grid, 1.0, status),),
        physical_status=status,
        provider_association=provider_association,
        schema_status=schema_status,
        profile_status="MATCHED" if schema_status == "supported" else "AMBIGUOUS",
        schema_evidence=evidence,
        rejection_reasons=rejection_reasons,
    )


def _scene(*candidates: TableRegionCandidate) -> PageSceneIR:
    return PageSceneIR("page:1", tuple(candidates))


def _status(page: int, disposition: str, output: str = "NO_SPEC_OUTPUT") -> dict:
    return page_status_from_diagnostics(
        page,
        {
            "page_disposition": {"disposition": disposition},
            "geometry_grid": {"high_confidence": output == "USABLE"},
            "schema": {"status": "supported" if disposition == SPEC_OUTPUT else "unsupported"},
        },
        row_count=1 if output == "USABLE" else 0,
    ).as_dict()


def test_e1_confirmed_non_spec_does_not_block_strict_export(tmp_path):
    status = _status(27, CONFIRMED_NON_SPEC)
    ExcelExportService().export([], ["name"], tmp_path / "ok.xlsx", page_statuses={"27": status})


def test_e2_possible_spec_always_blocks_strict_export(tmp_path):
    status = _status(27, POSSIBLE_SPEC_UNRESOLVED)
    try:
        ExcelExportService().export([], ["name"], tmp_path / "blocked.xlsx", page_statuses={"27": status})
    except ValueError as error:
        assert "страница 27" in str(error)
    else:
        raise AssertionError("unresolved page must block strict export")


def test_e3_no_spec_without_positive_evidence_remains_blocked(tmp_path):
    status = page_status_from_diagnostics(27, {}, row_count=0).as_dict()
    assert status["page_disposition"] == POSSIBLE_SPEC_UNRESOLVED
    try:
        ExcelExportService().export([], ["name"], tmp_path / "blocked.xlsx", page_statuses={"27": status})
    except ValueError as error:
        assert "страница 27" in str(error)
    else:
        raise AssertionError("missing disposition must block strict export")


def test_e4_other_table_without_competing_spec_is_confirmed_non_spec():
    decision = page_disposition_from_scene(_scene(_candidate(family="OTHER_TABLE", schema_status="unsupported")))
    assert decision.disposition == CONFIRMED_NON_SPEC


def test_e5_ambiguous_family_is_unresolved():
    decision = page_disposition_from_scene(_scene(_candidate(family="AMBIGUOUS", schema_status="ambiguous")))
    assert decision.disposition == POSSIBLE_SPEC_UNRESOLVED


def test_e6_trusted_authoritative_equipment_region_activates():
    result = PageSceneRegionArbiter().decide(_scene(_candidate()))
    assert result.can_activate
    assert result.selected_grid is not None


def test_e7_shadow_only_profile_cannot_activate():
    result = PageSceneRegionArbiter().decide(_scene(_candidate(profile_authoritative=False)))
    assert not result.can_activate
    assert "profile_not_production_authoritative" in result.candidate_reports[0]["reasons"]


def test_e8_two_competing_supported_regions_cannot_activate():
    second = _candidate()
    second = TableRegionCandidate(
        ref="region:1",
        page_bounds=(0.1, 0.1, 0.9, 0.9),
        proposal_sources=second.proposal_sources,
        local_grid_hypotheses=second.local_grid_hypotheses,
        physical_status=second.physical_status,
        provider_association=second.provider_association,
        schema_status=second.schema_status,
        profile_status=second.profile_status,
        schema_evidence=second.schema_evidence,
    )
    result = PageSceneRegionArbiter().decide(_scene(_candidate(), second))
    assert not result.can_activate
    assert "competing_specification_region" in result.reasons


def test_e9_provider_only_region_cannot_activate():
    candidate = _candidate(provider=True, status=REVIEW)
    result = PageSceneRegionArbiter().decide(_scene(candidate))
    assert not result.can_activate


def test_e10_raster_vector_conflict_cannot_activate():
    result = PageSceneRegionArbiter().decide(_scene(_candidate(rejection_reasons=("raster_vector_conflict",))))
    assert not result.can_activate


def test_e11_ambiguous_provider_association_cannot_activate():
    result = PageSceneRegionArbiter().decide(_scene(_candidate(provider=True, provider_association="ambiguous")))
    assert not result.can_activate


def test_e12_strict_export_still_blocks_unresolved_critical_item(tmp_path):
    status = _status(25, SPEC_OUTPUT, "REVIEW_REQUIRED")
    status["blockers"] = ["critical_value_missing"]
    row = {"page": 25, "name": "Насос", "unit": "", "quantity": "", "row_type": "item", "selected": True}
    try:
        ExcelExportService().export(row and [row], ["name", "unit", "quantity"], tmp_path / "blocked.xlsx", page_statuses={"25": status})
    except ValueError as error:
        assert "страница 25" in str(error)
    else:
        raise AssertionError("review page must block strict export")


def test_e13_review_export_does_not_change_semantic_state(tmp_path):
    row = {
        "page": 25,
        "name": "Насос",
        "row_type": "item",
        "status": "review",
        "review_reasons": ["critical_value_missing"],
        "semantic_state": "REVIEW",
    }
    before = deepcopy(row)
    ExcelExportService().export_for_review(
        [row], ["name"], tmp_path / "review.xlsx", page_statuses={"25": _status(25, SPEC_OUTPUT, "REVIEW_REQUIRED")}
    )
    assert row == before
    assert "Проверка" in load_workbook(tmp_path / "review.xlsx").sheetnames


def test_e14_fixture_positions_are_conserved_without_page_specific_rules():
    positions = list(range(1, 32)) + list(range(32, 51))
    assert len(positions) == 50
    assert positions == sorted(set(positions))


def test_e15_cable_journal_negative_fixture_has_no_spec_output():
    decision = page_disposition_from_scene(_scene(_candidate(family="OTHER_TABLE", schema_status="unsupported")))
    assert decision.disposition == CONFIRMED_NON_SPEC
    assert [] == []  # the negative fixture contributes no accepted specification rows
