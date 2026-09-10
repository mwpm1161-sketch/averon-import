"""Typed page-level disposition for conservative OCR/export decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


SPEC_OUTPUT = "SPEC_OUTPUT"
CONFIRMED_NON_SPEC = "CONFIRMED_NON_SPEC"
POSSIBLE_SPEC_UNRESOLVED = "POSSIBLE_SPEC_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class PageDispositionDecision:
    """A page classification independent from row/output status."""

    disposition: str = POSSIBLE_SPEC_UNRESOLVED
    reasons: tuple[str, ...] = ()
    candidate_refs: tuple[str, ...] = ()
    selected_region_ref: str | None = None
    provenance: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        allowed = {SPEC_OUTPUT, CONFIRMED_NON_SPEC, POSSIBLE_SPEC_UNRESOLVED}
        if self.disposition not in allowed:
            object.__setattr__(self, "disposition", POSSIBLE_SPEC_UNRESOLVED)
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(str(item) for item in self.reasons)))
        object.__setattr__(self, "candidate_refs", tuple(dict.fromkeys(str(item) for item in self.candidate_refs)))
        object.__setattr__(self, "provenance", tuple(dict(item) for item in self.provenance))

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "candidate_refs": list(self.candidate_refs),
            "selected_region_ref": self.selected_region_ref,
            "provenance": [dict(item) for item in self.provenance],
        }


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _schema_evidence(candidate: Any) -> Mapping[str, Any]:
    value = _value(candidate, "schema_evidence", {})
    return value if isinstance(value, Mapping) else {}


def _family(candidate: Any) -> Mapping[str, Any]:
    evidence = _schema_evidence(candidate)
    value = evidence.get("family") or evidence.get("family_assessment") or {}
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    return value if isinstance(value, Mapping) else {}


def _authoritative_route_ready(authoritative: Mapping[str, Any]) -> bool:
    """Return whether the already-selected production route is safe.

    Page-scene arbitration is shadow evidence.  The production reconstruction
    route is allowed to remain authoritative when it has its own trusted grid
    and schema contract.  This helper deliberately accepts diagnostics only;
    it never promotes a route that still carries a material safety blocker.
    """
    grid = authoritative.get("geometry_grid")
    grid = grid if isinstance(grid, Mapping) else {}
    schema = authoritative.get("schema")
    schema = schema if isinstance(schema, Mapping) else {}
    schema_status = str(
        schema.get("status") or authoritative.get("schema_status") or ""
    ).lower()
    if schema_status != "supported" or not bool(grid.get("high_confidence")):
        return False
    if str(authoritative.get("selected_mode") or "") != "geometry_first":
        return False
    if authoritative.get("physical_row_loss_suspected"):
        return False
    if int(authoritative.get("identity_cell_missing_count") or 0) > 0:
        return False
    if authoritative.get("schema_profile_shadow_only"):
        return False
    if authoritative.get("semantic_resolution_error"):
        return False
    if authoritative.get("assignment_safety") == "fallback_required":
        return False
    if authoritative.get("material_disagreement"):
        return False
    if authoritative.get("material_column_disagreement"):
        return False
    if authoritative.get("critical_boundary_conflicts"):
        return False
    structural = authoritative.get("structural_evidence")
    if isinstance(structural, Mapping):
        if structural.get("material_disagreement"):
            return False
        if structural.get("critical_boundary_conflicts"):
            return False
        if structural.get("material_column_disagreement"):
            return False
    profile = authoritative.get("profile_match")
    if not isinstance(profile, Mapping):
        return False
    selected = profile.get("selected_profile")
    if not isinstance(selected, Mapping) or not bool(selected.get("production_authoritative")):
        return False
    return True


def page_disposition_from_scene(
    scene: Any,
    *,
    arbitration: Any = None,
    authoritative: Mapping[str, Any] | None = None,
) -> PageDispositionDecision:
    """Classify a PageScene without using page text, filenames or page numbers.

    A missing scene, missing table candidate, or ambiguous family is always
    unresolved.  Positive OTHER_TABLE evidence is sufficient only when no
    supported/unknown specification candidate competes with it.
    """
    candidates = tuple(_value(scene, "region_candidates", ()) or ()) if scene is not None else ()
    refs = tuple(str(_value(item, "ref", "")) for item in candidates if _value(item, "ref", ""))
    supported = [item for item in candidates if str(_value(item, "schema_status", "")).lower() == "supported"]
    unknown = [
        item for item in candidates
        if str(_value(item, "schema_status", "")).lower() in {"unknown", "unknown_spec_schema"}
    ]
    arbitration_allowed = True
    if arbitration is not None:
        arbitration_allowed = bool(_value(arbitration, "can_activate", False))
    supported_ready = [
        item for item in supported
        if str(_value(item, "physical_status", "")) == "TRUSTED"
        and bool((_selected := (_schema_evidence(item).get("profile_match") or {}).get("selected_profile")) and _selected.get("production_authoritative"))
        and not set(_value(item, "rejection_reasons", ()) or ()).intersection({
            "association_ambiguous", "raster_vector_conflict", "material_column_conflict",
            "critical_boundary_conflicts", "unsafe_physical_column_anchoring",
        })
    ]
    if authoritative and _authoritative_route_ready(authoritative):
        return PageDispositionDecision(
            disposition=SPEC_OUTPUT,
            reasons=("authoritative_production_route",),
            candidate_refs=refs,
            provenance=(
                {"source": "authoritative_reconstruction"},
                {"source": "page_scene_shadow", "arbitration_allowed": arbitration_allowed},
            ),
        )

    if supported and len(supported) == 1 and len(supported_ready) == 1 and arbitration_allowed:
        return PageDispositionDecision(
            disposition=SPEC_OUTPUT,
            reasons=("supported_specification_region_present",),
            candidate_refs=refs,
            provenance=({"source": "page_scene"},),
        )

    other = [
        item for item in candidates
        if str(_family(item).get("family") or "") == "OTHER_TABLE"
        or str(_family(item).get("status") or "") == "OTHER_TABLE"
    ]
    if other and not supported and not unknown:
        return PageDispositionDecision(
            disposition=CONFIRMED_NON_SPEC,
            reasons=("confirmed_other_table", "no_competing_specification_region"),
            candidate_refs=refs,
            provenance=({"source": "bounded_family_evidence"},),
        )

    reasons = ["possible_specification_unresolved"]
    if not candidates:
        reasons.append("no_table_candidate_is_not_positive_non_spec_evidence")
    elif unknown:
        reasons.append("unknown_specification_region_present")
    elif any(str(_family(item).get("status") or "") == "AMBIGUOUS" for item in candidates):
        reasons.append("family_evidence_ambiguous")
    return PageDispositionDecision(
        disposition=POSSIBLE_SPEC_UNRESOLVED,
        reasons=tuple(reasons),
        candidate_refs=refs,
        provenance=({"source": "page_scene"},),
    )


__all__ = [
    "CONFIRMED_NON_SPEC",
    "PageDispositionDecision",
    "POSSIBLE_SPEC_UNRESOLVED",
    "SPEC_OUTPUT",
    "page_disposition_from_scene",
]
