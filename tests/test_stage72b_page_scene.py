from __future__ import annotations

from pathlib import Path

import cv2
import fitz
import numpy as np

from averon_import.services.ocr.page_scene import (
    PROVIDER,
    RASTER,
    REJECTED,
    REVIEW,
    TRUSTED,
    LocalGridHypothesis,
    RasterRegionProposalSource,
    TableRegionCandidate,
    VectorTableRegionDetector,
    compose_page_scene,
    provider_region_proposals,
)
from averon_import.services.ocr.physical_grid import PhysicalGrid
from averon_import.services.ocr.raster_grid import RasterRuledTableGridDetector


def _ruled_image(boxes: tuple[tuple[int, int, int, int], ...]) -> np.ndarray:
    image = np.full((900, 1400), 255, dtype=np.uint8)
    for left, top, right, bottom in boxes:
        for column in range(4):
            x = round(left + (right - left) * column / 3)
            cv2.line(image, (x, top), (x, bottom), 0, 3)
        for row in range(4):
            y = round(top + (bottom - top) * row / 3)
            cv2.line(image, (left, y), (right, y), 0, 3)
    return image


def _candidate(
    ref: str,
    bounds: tuple[float, float, float, float],
    *,
    source: str = RASTER,
    status: str = TRUSTED,
    schema_status: str | None = None,
) -> TableRegionCandidate:
    grid = PhysicalGrid(
        source=source,
        x_boundaries=(bounds[0], bounds[2]),
        y_boundaries=(bounds[1], bounds[3]),
        cells=(),
        confidence=1.0,
        high_confidence=status == TRUSTED,
    )
    hypothesis = LocalGridHypothesis(
        source=source,
        bounds=bounds,
        grid=grid,
        confidence=1.0,
        status=status,
    )
    return TableRegionCandidate(
        ref=ref,
        page_bounds=bounds,
        proposal_sources=(source,),
        local_grid_hypotheses=(hypothesis,),
        physical_status=status,
        schema_status=schema_status,
        provenance=({"source": source},),
    )


def _provider(bounds: tuple[float, float, float, float]) -> TableRegionCandidate:
    return TableRegionCandidate(
        ref="provider:0",
        page_bounds=bounds,
        proposal_sources=(PROVIDER,),
        provider_proposal_evidence={"table_index": 0},
        physical_status=REVIEW,
        rejection_reasons=("provider_only_no_physical_corroboration",),
        provider_refs=("provider:0",),
        provider_association="unassociated",
    )


def test_r1_two_spatially_separate_raster_tables_survive_as_two_regions():
    proposals = RasterRegionProposalSource(RasterRuledTableGridDetector()).propose(
        _ruled_image(((50, 60, 520, 430), (800, 120, 1320, 780)))
    )
    assert len(proposals) == 2
    scene = compose_page_scene("page:1", raster_proposals=proposals)
    assert scene.diagnostics["table_region_candidate_count"] == 2
    assert scene.diagnostics["multi_table_page"] is True


def test_r2_large_drawing_and_smaller_table_both_survive_generation():
    proposals = RasterRegionProposalSource(RasterRuledTableGridDetector()).propose(
        _ruled_image(((40, 40, 1050, 760), (1120, 120, 1360, 340)))
    )
    assert len(proposals) == 2
    assert {item.physical_status for item in proposals} <= {TRUSTED, REVIEW, REJECTED}


def test_r3_same_region_raster_and_vector_proposals_fuse_with_provenance():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(_candidate("raster:0", (0.1, 0.1, 0.5, 0.5)),),
        vector_proposals=(_candidate("vector:0", (0.105, 0.105, 0.495, 0.495), source="vector"),),
    )
    assert len(scene.region_candidates) == 1
    candidate = scene.region_candidates[0]
    assert set(candidate.proposal_sources) == {RASTER, "vector"}
    assert candidate.local_grid_hypotheses[0].source == RASTER


def test_r4_nearby_non_overlapping_regions_are_not_fused():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(
            _candidate("raster:0", (0.1, 0.1, 0.3, 0.3)),
            _candidate("raster:1", (0.31, 0.1, 0.5, 0.3)),
        ),
    )
    assert len(scene.region_candidates) == 2


def test_r5_provider_bbox_alone_remains_review():
    scene = compose_page_scene("page:1", provider_proposals=(_provider((0.1, 0.1, 0.5, 0.5)),))
    assert len(scene.region_candidates) == 1
    assert scene.region_candidates[0].physical_status == REVIEW
    assert PROVIDER in scene.region_candidates[0].proposal_sources


def test_r6_provider_bbox_plus_raster_corroboration_can_be_trusted():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(_candidate("raster:0", (0.1, 0.1, 0.5, 0.5)),),
        provider_proposals=(_provider((0.11, 0.11, 0.49, 0.49)),),
    )
    assert len(scene.region_candidates) == 1
    assert scene.region_candidates[0].physical_status == TRUSTED
    assert scene.region_candidates[0].provider_association == "unique"


def test_r7_provider_bbox_plus_vector_corroboration_can_be_trusted():
    scene = compose_page_scene(
        "page:1",
        vector_proposals=(_candidate("vector:0", (0.1, 0.1, 0.5, 0.5), source="vector"),),
        provider_proposals=(_provider((0.1, 0.1, 0.5, 0.5)),),
    )
    assert scene.region_candidates[0].physical_status == TRUSTED
    assert scene.region_candidates[0].provider_association == "unique"


def test_r8_vector_only_valid_table_is_a_trusted_physical_region(tmp_path: Path):
    path = tmp_path / "vector-table.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    for x in (100, 200, 300, 400):
        page.draw_line(fitz.Point(x, 100), fitz.Point(x, 400))
    for y in (100, 200, 300, 400):
        page.draw_line(fitz.Point(100, y), fitz.Point(400, y))
    document.save(path)
    document.close()

    proposals = VectorTableRegionDetector().propose_page(path, 1)
    assert len(proposals) == 1
    assert proposals[0].physical_status == TRUSTED
    assert proposals[0].local_grid_hypotheses[0].grid is not None


def test_r9_raster_only_scan_table_is_supported_physically():
    proposals = RasterRegionProposalSource(RasterRuledTableGridDetector()).propose(
        _ruled_image(((100, 100, 1000, 700),))
    )
    assert len(proposals) == 1
    assert proposals[0].proposal_sources == (RASTER,)
    assert proposals[0].local_grid_hypotheses[0].grid is not None


def test_r10_weak_drawing_graph_never_becomes_trusted(tmp_path: Path):
    path = tmp_path / "weak-drawing-scene.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    for x in (100, 200, 300, 400):
        page.draw_line(fitz.Point(x, 100), fitz.Point(x, 250))
    page.draw_line(fitz.Point(100, 100), fitz.Point(400, 100))
    page.draw_line(fitz.Point(100, 250), fitz.Point(250, 250))
    document.save(path)
    document.close()
    proposals = VectorTableRegionDetector().propose_page(path, 1)
    assert all(item.physical_status != TRUSTED for item in proposals)


def test_r11_physical_title_block_can_be_trusted_but_schema_non_spec():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(_candidate("raster:title", (0.7, 0.7, 0.98, 0.98), schema_status="unsupported"),),
    )
    assert scene.region_candidates[0].physical_status == TRUSTED
    assert scene.diagnostics["non_spec_region_count"] == 1


def test_r12_two_specification_regions_remain_independent():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(
            _candidate("raster:0", (0.05, 0.05, 0.45, 0.3), schema_status="supported"),
            _candidate("raster:1", (0.55, 0.55, 0.95, 0.85), schema_status="supported"),
        ),
    )
    assert len(scene.region_candidates) == 2
    assert scene.diagnostics["supported_schema_region_count"] == 2


def test_r13_ambiguous_provider_association_is_review():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(
            _candidate("raster:0", (0.1, 0.1, 0.4, 0.4)),
            _candidate("raster:1", (0.35, 0.1, 0.65, 0.4)),
        ),
        provider_proposals=(_provider((0.25, 0.1, 0.5, 0.4)),),
    )
    provider_candidates = [item for item in scene.region_candidates if PROVIDER in item.proposal_sources]
    assert len(provider_candidates) == 1
    assert provider_candidates[0].physical_status == REVIEW
    assert provider_candidates[0].provider_association == "ambiguous"


def test_r14_partial_merged_lines_remain_candidate_review_not_invented_geometry():
    base = _candidate("vector:merged", (0.1, 0.1, 0.6, 0.5), source="vector", status=REVIEW)
    candidate = TableRegionCandidate(
        ref=base.ref,
        page_bounds=base.page_bounds,
        proposal_sources=base.proposal_sources,
        local_grid_hypotheses=base.local_grid_hypotheses,
        physical_status=base.physical_status,
        rejection_reasons=("merged_cell_span_ambiguity",),
        provenance=base.provenance,
    )
    assert candidate.physical_status == REVIEW
    assert "merged_cell_span_ambiguity" in candidate.rejection_reasons


def test_r15_rejected_candidate_retains_diagnostics_and_provenance():
    candidate = _candidate("raster:rejected", (0.1, 0.1, 0.2, 0.2), status=REJECTED)
    candidate = TableRegionCandidate(
        ref=candidate.ref,
        page_bounds=candidate.page_bounds,
        proposal_sources=candidate.proposal_sources,
        local_grid_hypotheses=candidate.local_grid_hypotheses,
        physical_status=REJECTED,
        rejection_reasons=("poor_closed_cell_topology",),
        provenance=({"source": "raster", "candidate_index": 3},),
    )
    scene = compose_page_scene("page:1", raster_proposals=(candidate,))
    assert scene.region_candidates[0].physical_status == REJECTED
    assert scene.region_candidates[0].provenance[0]["candidate_index"] == 3


def test_provider_table_bbox_can_be_derived_from_cells_without_text_semantics():
    payload = {
        "page": {"width": 1000, "height": 1000},
        "textAnnotation": {
            "tables": [{
                "rowCount": 2,
                "columnCount": 2,
                "cells": [{
                    "boundingBox": {"vertices": [
                        {"x": 100, "y": 100}, {"x": 300, "y": 100},
                        {"x": 300, "y": 200}, {"x": 100, "y": 200},
                    ]}
                }],
            }]
        },
    }
    proposal = provider_region_proposals(payload)[0]
    assert proposal.physical_status == REVIEW
    assert proposal.provider_proposal_evidence["row_count"] == 2


def test_provider_association_prefers_local_physical_iou_over_page_frame():
    scene = compose_page_scene(
        "page:1",
        raster_proposals=(
            _candidate("raster:frame", (0.01, 0.01, 0.99, 0.99), status=REVIEW),
            _candidate("raster:table", (0.67, 0.04, 0.99, 0.33)),
        ),
        provider_proposals=(_provider((0.68, 0.05, 0.98, 0.32)),),
    )
    associated = [item for item in scene.region_candidates if PROVIDER in item.proposal_sources]
    assert len(associated) == 1
    assert associated[0].ref == "raster:table"
    assert associated[0].provider_association == "unique"
