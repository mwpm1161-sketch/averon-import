import base64
import json
from types import SimpleNamespace

import cv2
import numpy as np

from averon_import.services.ocr.base import OcrRow
from averon_import.services.ocr.physical_grid import (
    PhysicalGrid,
    PhysicalGridCell,
    PhysicalGridDetection,
)
from averon_import.services.ocr.page_contract import page_status_from_diagnostics
from averon_import.services.ocr.raster_grid import RasterGridPage, crop_has_isolated_glyph
from averon_import.services.ocr.reconstruction import (
    _primary_structural_evidence,
    _schema_gate_structural_evidence,
    target_cell_structural_safety,
)
from averon_import.services.ocr.semantics.context_evidence import BoundedFamilyContext
from averon_import.services.ocr.semantics.family_classifier import TableFamilyClassifier
from averon_import.services.ocr.semantics.header_evidence import HeaderMappingResult
from averon_import.services.ocr.semantics.schema_gate import DEFAULT_SCHEMA_GATE
from averon_import.services.ocr.yandex_vision import YandexVisionProvider, _HttpResponse
from averon_import.services.review_policy import (
    CRITICAL_FIELDS,
    critical_blockers_for_row,
    refresh_review_state,
)
from averon_import.services.secrets import MemorySecretStore


MAPPING = {
    0: ("position",),
    1: ("name",),
    2: ("type_mark",),
    3: ("code",),
    4: ("manufacturer",),
    5: ("unit",),
    6: ("quantity",),
    7: ("mass",),
    8: ("note",),
}


def _grid(rows: int = 2) -> PhysicalGrid:
    xs = tuple(0.05 + index * 0.1 for index in range(10))
    ys = tuple(index / rows for index in range(rows + 1))
    return PhysicalGrid(
        source="test",
        x_boundaries=xs,
        y_boundaries=ys,
        cells=tuple(
            PhysicalGridCell(row, column, (xs[column], ys[row], xs[column + 1], ys[row + 1]))
            for row in range(rows)
            for column in range(9)
        ),
        confidence=1.0,
        high_confidence=True,
    )


def _cell(left: float, right: float, *, row: int = 0, top: float = 0, bottom: float = 500) -> dict:
    return {
        "text": "",
        "rowIndex": row,
        "columnIndex": 0,
        "boundingBox": {"vertices": [
            {"x": left * 1000, "y": top},
            {"x": right * 1000, "y": top},
            {"x": right * 1000, "y": bottom},
            {"x": left * 1000, "y": bottom},
        ]},
    }


def _payload(edges: list[float], *, column_count: int | None = None, jitter: float | None = None) -> dict:
    cells = [_cell(left, right) for left, right in zip(edges, edges[1:])]
    if jitter is not None:
        shifted = [value + jitter for value in edges]
        cells.extend(
            _cell(left, right, row=1, top=500, bottom=1000)
            for left, right in zip(shifted, shifted[1:])
        )
    return {
        "page": {"width": 1000, "height": 1000},
        "textAnnotation": {
            "width": 1000,
            "height": 1000,
            "tables": [{
                "rowCount": 2 if jitter is not None else 1,
                "columnCount": column_count if column_count is not None else len(edges) - 1,
                "cells": cells,
            }],
        },
    }


def _evidence(edges: list[float], *, column_count: int | None = None, jitter: float | None = None) -> dict:
    return _primary_structural_evidence(
        _payload(edges, column_count=column_count, jitter=jitter),
        _grid(),
        1000,
        1000,
        mapping=MAPPING,
    )


def test_extra_outer_leading_provider_column_is_informational():
    grid = _grid()
    evidence = _evidence([0.01, *grid.x_boundaries], column_count=10)
    assert evidence["column_count_conflict"] is True
    assert evidence["extra_provider_outer_boundaries"]
    assert evidence["missing_grid_internal_boundaries"] == []
    assert evidence["material_disagreement"] is False
    assert evidence["informational_column_disagreement"] is True


def _trusted_schema_mapping() -> HeaderMappingResult:
    return HeaderMappingResult(
        status="trusted",
        header_rows=(0,),
        mapping=MAPPING,
        candidates_by_column={},
        best_score=1.0,
        second_best_score=0.0,
        assignment_margin=1.0,
        unmapped_columns=(),
        missing_core_fields=(),
        reasons=(),
    )


def test_primary_material_internal_disagreement_reaches_schema_gate():
    edges = list(_grid().x_boundaries)
    edges.pop(6)
    evidence = _evidence(edges)
    gate_evidence = _schema_gate_structural_evidence(evidence)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=9,
        mapping=_trusted_schema_mapping(),
        family=TableFamilyClassifier().assess(BoundedFamilyContext()),
        structural=gate_evidence,
    )
    assert evidence["material_column_disagreement"] is True
    assert gate_evidence.material_column_conflict is True
    assert assessment.status == "ambiguous"


def test_primary_nvk_style_extra_outer_column_stays_schema_eligible():
    evidence = _evidence([0.01, *_grid().x_boundaries], column_count=10)
    gate_evidence = _schema_gate_structural_evidence(evidence)
    assessment = DEFAULT_SCHEMA_GATE.assess(
        column_count=9,
        mapping=_trusted_schema_mapping(),
        family=TableFamilyClassifier().assess(BoundedFamilyContext()),
        structural=gate_evidence,
    )
    assert evidence["material_column_disagreement"] is False
    assert gate_evidence.material_column_conflict is False
    assert assessment.status == "supported"


def test_provider_bbox_jitter_is_clustered_and_informational():
    grid = _grid()
    evidence = _evidence(list(grid.x_boundaries), jitter=0.004)
    assert len(evidence["provider_x_boundaries_raw"]) > len(
        evidence["provider_x_boundaries_clustered"]
    )
    assert evidence["material_disagreement"] is False
    assert "provider_bbox_edge_jitter" in evidence["informational_disagreement_reasons"]


def test_missing_unit_quantity_boundary_is_material():
    edges = list(_grid().x_boundaries)
    missing = edges.pop(6)
    evidence = _evidence(edges)
    assert round(missing, 6) in evidence["missing_grid_internal_boundaries"]
    assert round(missing, 6) in evidence["critical_boundary_conflicts"]
    assert evidence["material_column_disagreement"] is True


def test_missing_quantity_mass_boundary_is_material():
    edges = list(_grid().x_boundaries)
    missing = edges.pop(7)
    evidence = _evidence(edges)
    assert round(missing, 6) in evidence["critical_boundary_conflicts"]
    assert evidence["material_disagreement"] is True


def test_internal_provider_edge_splitting_quantity_is_material():
    grid = _grid()
    edges = sorted([*grid.x_boundaries, (grid.x_boundaries[6] + grid.x_boundaries[7]) / 2])
    evidence = _evidence(edges)
    assert evidence["extra_provider_internal_boundaries"]
    assert evidence["critical_boundary_conflicts"]
    assert evidence["material_disagreement"] is True


def test_provider_row_merge_is_informational_with_trusted_grid():
    grid = _grid()
    payload = _payload(list(grid.x_boundaries))
    for cell in payload["textAnnotation"]["tables"][0]["cells"]:
        cell["boundingBox"]["vertices"][2]["y"] = 1000
        cell["boundingBox"]["vertices"][3]["y"] = 1000
    evidence = _primary_structural_evidence(payload, grid, 1000, 1000, mapping=MAPPING)
    assert evidence["row_boundary_conflicts"] == [0.5]
    assert evidence["material_disagreement"] is False
    assert "provider_row_merge" in evidence["informational_disagreement_reasons"]


def _row(grid: PhysicalGrid, *, position: str = "", evidence: dict | None = None) -> OcrRow:
    return OcrRow(
        source_row=2,
        values={"position": position, "name": "Насос", "unit": "шт.", "quantity": ""},
        confidences={},
        sources={"name": "yandex_vision", "unit": "yandex_vision"},
        bbox={},
        metadata={
            "structured_table": True,
            "provider_has_explicit_rows": True,
            "reconstruction_mode": "geometry_first",
            "schema_assessment": {"status": "supported"},
            "column_mapping": {str(key): list(value) for key, value in MAPPING.items()},
            "physical_grid_cells": {
                "position": {"row_index": 1, "column_index": 0, "bbox": grid.cell(1, 0).as_bbox()},
                "quantity": {"row_index": 1, "column_index": 6, "bbox": grid.cell(1, 6).as_bbox()},
            },
            "structural_evidence": evidence or {},
            "ambiguous_physical_cells": [],
            "weak_critical_fields": [],
        },
    )


def test_local_assignment_ambiguity_at_target_retains_blocker():
    grid = _grid()
    row = _row(grid)
    row.metadata["structural_ambiguity"] = True
    row.metadata["word_assignment_ambiguity"] = True
    row.metadata["ambiguous_physical_cells"] = [{"row_index": 1, "column_index": 6}]
    safety = target_cell_structural_safety(row, "quantity", grid)
    assert safety["safe"] is False
    assert "local_word_assignment_ambiguity" in safety["reasons"]


class _Settings:
    settings = SimpleNamespace(yandex=SimpleNamespace(
        folder_id="folder",
        vision_model="table",
        vision_base_url="https://ocr.invalid",
        language_codes=["ru", "en"],
        chunk_pages=1,
        request_timeout_s=1.0,
        operation_timeout_s=1.0,
    ))


class _Http:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, body=None, headers=None, timeout=30.0):
        request = json.loads(body)
        assert base64.b64decode(request["content"]).startswith(b"\x89PNG")
        self.calls += 1
        response = {"result": {"textAnnotation": {"width": 10, "height": 10, "fullText": "1"}}}
        return _HttpResponse(200, json.dumps(response).encode())


def _provider(http=None) -> YandexVisionProvider:
    secrets = MemorySecretStore()
    secrets.set("yandex.api_key", "test-key")
    return YandexVisionProvider(
        _Settings(), secrets, cache_dir=None, http=http or _Http(),
        sleep_fn=lambda _seconds: None, reconstruction_mode="geometry",
    )


def _raster(grid: PhysicalGrid, *, glyph_column: int | None) -> RasterGridPage:
    image = np.full((200, 1000), 255, dtype=np.uint8)
    if glyph_column is not None:
        cell = grid.cell(1, glyph_column)
        cv2.putText(
            image, "1", (int(cell.bounds[0] * 1000) + 35, 170),
            cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3,
        )
    return RasterGridPage(
        PhysicalGridDetection(grid=grid, source="test"),
        image,
        np.zeros_like(image),
    )


def _stats() -> dict:
    return {"exact_cell_checked": 0, "exact_cell_requests": 0, "exact_cell_candidates": 0}


def test_informational_global_conflict_allows_exact_cell_locally():
    grid = _grid()
    evidence = _evidence([0.01, *grid.x_boundaries], column_count=10)
    row = _row(grid, evidence=evidence)
    row.metadata["structural_disagreement"] = False
    row.metadata["informational_structural_disagreement"] = True
    http = _Http()
    provider = _provider(http)
    stats = _stats()
    provider._exact_cell_verify_page(
        1, _raster(grid, glyph_column=6), [row], provider._yandex_settings(),
        "test-key", None, [], stats,
    )
    assert http.calls == 1
    assert stats["exact_cell_checked"] == 1
    assert row.values["quantity"] == ""
    assert row.metadata["value_candidates"]["quantity"]["auto_trusted"] is False


def test_material_conflict_touching_quantity_disables_exact_cell():
    grid = _grid()
    edges = list(grid.x_boundaries)
    edges.pop(6)
    row = _row(grid, evidence=_evidence(edges))
    http = _Http()
    provider = _provider(http)
    stats = _stats()
    provider._exact_cell_verify_page(
        1, _raster(grid, glyph_column=6), [row], provider._yandex_settings(),
        "test-key", None, [], stats,
    )
    assert http.calls == 0
    assert stats["exact_cell_checked"] == 0
    assert "material_column_conflict_at_target" in row.metadata[
        "target_cell_structural_safety"
    ]["quantity"]["reasons"]


def test_missing_position_with_raster_glyph_adds_identity_blocker():
    assert CRITICAL_FIELDS == ("quantity", "unit", "mass")
    grid = _grid()
    row = _row(grid)
    assert _provider()._mark_identity_cell_loss(_raster(grid, glyph_column=0), [row]) == 1
    assert row.values["position"] == ""
    assert row.metadata["identity_cell_missing"] is True
    assert row.metadata["identity_field"] == "position"
    assert row.metadata["identity_raster_glyph"] is True
    assert row.metadata["identity_candidate_source"] is None
    final = {
        **row.values,
        "row_type": "item",
        "status": "verified",
        "review_reasons": row.metadata["review_reasons"],
        "ocr_metadata": row.metadata,
    }
    refresh_review_state(final)
    assert final["status"] == "review"
    assert "identity_cell_missing" in critical_blockers_for_row(final)


def test_empty_position_cell_without_raster_glyph_has_no_identity_blocker():
    grid = _grid()
    row = _row(grid)
    assert _provider()._mark_identity_cell_loss(_raster(grid, glyph_column=None), [row]) == 0
    assert not row.metadata.get("identity_cell_missing")


def test_neighboring_text_leak_is_not_identity_glyph_evidence():
    crop = np.full((300, 560), 255, dtype=np.uint8)
    cv2.line(crop, (40, 60), (520, 250), 0, 5)
    assert crop_has_isolated_glyph(crop) is False


def test_legitimate_unnumbered_item_without_position_glyph_is_not_blocked():
    grid = _grid()
    row = _row(grid)
    row.values["name"] = "Аккумулирующий резервуар"
    row.values["quantity"] = "1"
    assert _provider()._mark_identity_cell_loss(_raster(grid, glyph_column=None), [row]) == 0
    assert "identity_cell_missing" not in row.metadata.get("review_reasons", [])


def test_identity_blocker_clears_only_after_explicit_position_edit():
    row = {
        "name": "Насос",
        "position": "",
        "unit": "шт.",
        "quantity": "1",
        "mass": "",
        "row_type": "item",
        "status": "verified",
        "review_reasons": ["identity_cell_missing"],
        "critical_blockers": ["identity_cell_missing"],
        "ocr_metadata": {"provider": "yandex_vision", "identity_cell_missing": True},
    }
    refresh_review_state(row)
    assert row["status"] == "review"
    assert "identity_cell_missing" in row["critical_blockers"]
    row.update({"position": "7", "edited_fields": ["position"], "status": "verified"})
    refresh_review_state(row)
    assert "identity_cell_missing" not in row["critical_blockers"]
    assert row["ocr_metadata"]["identity_cell_missing"] is False


def test_identity_loss_keeps_page_reviewable_after_structural_localization():
    status = page_status_from_diagnostics(
        38,
        {
            "selected_mode": "geometry_first",
            "geometry_grid": {"high_confidence": True},
            "schema": {"status": "supported"},
            "structural_evidence": {"material_disagreement": False},
            "identity_cell_missing_count": 2,
        },
        row_count=23,
    )
    assert status.output_status == "REVIEW_REQUIRED"
    assert status.blockers == ["identity_cell_missing"]
