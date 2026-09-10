"""Fail-closed bridge from PageScene shadow evidence to production geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from averon_import.services.ocr.page_scene import PageSceneIR, TableRegionCandidate, TRUSTED
from averon_import.services.ocr.physical_grid import PhysicalGrid


def _evidence(candidate: TableRegionCandidate) -> Mapping[str, Any]:
    value = candidate.schema_evidence or {}
    return value if isinstance(value, Mapping) else {}


def _selected_profile(candidate: TableRegionCandidate) -> Mapping[str, Any]:
    value = _evidence(candidate).get("profile_match") or {}
    return value if isinstance(value, Mapping) else {}


def _schema(candidate: TableRegionCandidate) -> Mapping[str, Any]:
    value = _evidence(candidate).get("schema") or {}
    return value if isinstance(value, Mapping) else {}


def _bbox(raw: Any, width: float, height: float) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    vertices = raw.get("vertices") or ()
    points: list[tuple[float, float]] = []
    for point in vertices:
        if not isinstance(point, Mapping):
            continue
        try:
            points.append((float(point["x"]), float(point["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        return None
    scale_x = width if width > 1 else 1.0
    scale_y = height if height > 1 else 1.0
    values = (
        min(point[0] for point in points) / scale_x,
        min(point[1] for point in points) / scale_y,
        max(point[0] for point in points) / scale_x,
        max(point[1] for point in points) / scale_y,
    )
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _provider_cells_associated(candidate: TableRegionCandidate, grid: PhysicalGrid) -> bool:
    evidence = _evidence(candidate)
    cells = evidence.get("provider_cells") or ()
    if not cells:
        # A candidate without provider evidence is valid for vector/raster
        # pages.  A provider-associated candidate must prove its cells.
        return "provider" not in candidate.proposal_sources
    page_size = evidence.get("provider_page_size") or {}
    try:
        width = float(page_size.get("width") or 0)
        height = float(page_size.get("height") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    for cell in cells:
        bounds = _bbox(cell.get("boundingBox") if isinstance(cell, Mapping) else None, width, height)
        if bounds is None:
            return False
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
        if not any(
            item.bounds[0] <= center[0] <= item.bounds[2]
            and item.bounds[1] <= center[1] <= item.bounds[3]
            for item in grid.cells
        ):
            return False
    return True


@dataclass(frozen=True, slots=True)
class RegionArbitrationResult:
    can_activate: bool
    selected_region_ref: str | None = None
    selected_grid: PhysicalGrid | None = None
    reasons: tuple[str, ...] = ()
    candidate_reports: tuple[Mapping[str, Any], ...] = ()

    @property
    def activated(self) -> bool:
        return self.can_activate

    def as_dict(self) -> dict[str, Any]:
        return {
            "can_activate": self.can_activate,
            "activated": self.activated,
            "selected_region_ref": self.selected_region_ref,
            "selected_grid": self.selected_grid.as_dict() if self.selected_grid is not None else None,
            "reasons": list(self.reasons),
            "candidate_reports": [dict(item) for item in self.candidate_reports],
        }


class PageSceneRegionArbiter:
    """Select exactly one safe, authoritative equipment region."""

    def decide(self, scene: PageSceneIR | None) -> RegionArbitrationResult:
        candidates = tuple(scene.region_candidates if scene is not None else ())
        reports: list[dict[str, Any]] = []
        eligible: list[tuple[TableRegionCandidate, PhysicalGrid]] = []
        for candidate in candidates:
            schema = _schema(candidate)
            profile_match = _selected_profile(candidate)
            profile = profile_match.get("selected_profile") or {}
            reasons: list[str] = []
            grid = candidate.trusted_grid
            if candidate.physical_status != TRUSTED or grid is None or not grid.high_confidence:
                reasons.append("physical_grid_not_trusted")
            if str(candidate.schema_status or "").lower() != "supported" or str(schema.get("status") or "").lower() != "supported":
                reasons.append("schema_not_supported")
            if str(profile_match.get("status") or "") != "MATCHED":
                reasons.append("profile_not_unique")
            if not bool(profile.get("production_authoritative")):
                reasons.append("profile_not_production_authoritative")
            if "provider" in candidate.proposal_sources and candidate.provider_association != "unique":
                reasons.append("provider_association_not_unique")
            for reason in candidate.rejection_reasons:
                if reason in {
                    "association_ambiguous",
                    "raster_vector_conflict",
                    "unsafe_physical_column_anchoring",
                    "critical_boundary_conflicts",
                    "material_column_conflict",
                    "material_disagreement",
                }:
                    reasons.append(reason)
            for reason in schema.get("reasons") or ():
                if any(token in str(reason) for token in (
                    "critical_boundary", "unsafe_physical", "material_column", "structural_boundary",
                )):
                    reasons.append(str(reason))
            if grid is not None and not _provider_cells_associated(candidate, grid):
                reasons.append("provider_cells_not_spatially_associated")
            report = {
                "ref": candidate.ref,
                "physical_status": candidate.physical_status,
                "schema_status": candidate.schema_status,
                "profile_status": candidate.profile_status,
                "provider_association": candidate.provider_association,
                "reasons": list(dict.fromkeys(reasons)),
            }
            reports.append(report)
            if not reasons and grid is not None:
                eligible.append((candidate, grid))

        supported_or_unknown = [
            candidate for candidate in candidates
            if str(candidate.schema_status or "").lower() in {"supported", "unknown", "unknown_spec_schema"}
        ]
        if len(eligible) != 1:
            reason = "no_unique_safe_authoritative_region" if not eligible else "multiple_safe_authoritative_regions"
            return RegionArbitrationResult(
                can_activate=False,
                reasons=tuple(dict.fromkeys((reason,) + (("competing_specification_region",) if len(supported_or_unknown) > 1 else ()))),
                candidate_reports=tuple(reports),
            )
        selected, grid = eligible[0]
        if len(supported_or_unknown) != 1:
            return RegionArbitrationResult(
                can_activate=False,
                reasons=("competing_specification_region",),
                candidate_reports=tuple(reports),
            )
        return RegionArbitrationResult(
            can_activate=True,
            selected_region_ref=selected.ref,
            selected_grid=grid,
            reasons=("unique_trusted_authoritative_equipment_region",),
            candidate_reports=tuple(reports),
        )

    arbitrate = decide
    select = decide


__all__ = ["PageSceneRegionArbiter", "RegionArbitrationResult"]
