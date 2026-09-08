"""Provider-neutral page safety contract for structured OCR output.

The contract is deliberately small and serializable.  It is the boundary
between page reconstruction diagnostics and document/export policy: an empty
row list is not sufficient evidence that a page was safely processed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


LAYOUT_TRUSTED = "TRUSTED"
LAYOUT_AMBIGUOUS = "AMBIGUOUS"
LAYOUT_FAILED = "FAILED"

SCHEMA_SUPPORTED = "SUPPORTED"
SCHEMA_AMBIGUOUS = "AMBIGUOUS"
SCHEMA_UNSUPPORTED = "UNSUPPORTED"
SCHEMA_UNKNOWN = "UNKNOWN"

OUTPUT_USABLE = "USABLE"
OUTPUT_REVIEW_REQUIRED = "REVIEW_REQUIRED"
OUTPUT_NO_SPEC = "NO_SPEC_OUTPUT"


@dataclass(slots=True)
class PageExtractionStatus:
    page: int
    layout_status: str = LAYOUT_FAILED
    schema_status: str = SCHEMA_UNKNOWN
    output_status: str = OUTPUT_NO_SPEC
    blockers: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    def add_blocker(self, reason: str) -> None:
        reason = str(reason).strip()
        if reason and reason not in self.blockers:
            self.blockers.append(reason)

    def as_dict(self) -> dict:
        return {
            "page": int(self.page),
            "layout_status": self.layout_status,
            "schema_status": self.schema_status,
            "output_status": self.output_status,
            "blockers": list(dict.fromkeys(self.blockers)),
            "diagnostics": dict(self.diagnostics),
        }


def page_status_from_diagnostics(
    page: int,
    diagnostics: dict | None,
    *,
    row_count: int = 0,
) -> PageExtractionStatus:
    """Derive a conservative page contract from reconstruction diagnostics."""
    data = diagnostics if isinstance(diagnostics, dict) else {}
    grid = data.get("geometry_grid") if isinstance(data.get("geometry_grid"), dict) else {}
    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    schema_status = str(
        schema.get("status") or data.get("schema_status") or SCHEMA_UNKNOWN
    ).lower()
    schema_map = {
        "supported": SCHEMA_SUPPORTED,
        "ambiguous": SCHEMA_AMBIGUOUS,
        "unsupported": SCHEMA_UNSUPPORTED,
        "unknown": SCHEMA_UNKNOWN,
    }
    schema_value = schema_map.get(schema_status, SCHEMA_UNKNOWN)
    selected_mode = str(data.get("selected_mode") or "")
    high_grid = bool(grid.get("high_confidence"))
    validation_failed = bool(data.get("validation_errors"))
    fallback_reason = str(data.get("fallback_reason") or "")
    geometry_failed = validation_failed or fallback_reason in {
        "physical_grid_unavailable",
        "invalid_physical_grid",
        "physical_grid_low_confidence",
        "geometry_semantic_mapping_unavailable",
        "geometry_assignment_unsafe",
        "ambiguous_or_unassigned_row",
    }
    if high_grid and not geometry_failed:
        layout = LAYOUT_TRUSTED
    elif grid or data.get("geometry_source"):
        layout = LAYOUT_AMBIGUOUS
    else:
        layout = LAYOUT_FAILED

    status = PageExtractionStatus(
        page=int(page),
        layout_status=layout,
        schema_status=schema_value,
        diagnostics={
            "selected_mode": selected_mode,
            "fallback_reason": fallback_reason,
        },
    )
    if schema_value == SCHEMA_AMBIGUOUS:
        status.add_blocker("structural_schema_ambiguous")
    elif schema_value in {SCHEMA_UNSUPPORTED, SCHEMA_UNKNOWN}:
        status.add_blocker(
            "unsupported_table_schema" if schema_value == SCHEMA_UNSUPPORTED else "schema_unknown"
        )
    if layout != LAYOUT_TRUSTED and selected_mode not in {"table_fallback", "table_shadow"}:
        status.add_blocker("structural_layout_ambiguous" if layout == LAYOUT_AMBIGUOUS else "schema_unknown")
    if data.get("material_disagreement") or (
        isinstance(data.get("structural_evidence"), dict)
        and data["structural_evidence"].get("material_disagreement")
    ):
        status.add_blocker("structural_boundary_conflict")
    if data.get("assignment_safety") == "fallback_required":
        status.add_blocker("physical_row_loss_suspected")
    if data.get("physical_row_loss_suspected"):
        status.add_blocker("physical_row_loss_suspected")
    if int(data.get("identity_cell_missing_count") or 0) > 0:
        status.add_blocker("identity_cell_missing")
    if data.get("assembly_error"):
        status.add_blocker("assembly_error")
    if (
        row_count
        and not status.blockers
        and schema_value == SCHEMA_SUPPORTED
        and layout == LAYOUT_TRUSTED
    ):
        status.output_status = OUTPUT_USABLE
    elif row_count:
        status.output_status = OUTPUT_REVIEW_REQUIRED
    else:
        status.output_status = OUTPUT_NO_SPEC
    status.diagnostics["row_count"] = int(row_count)
    status.diagnostics["geometry_trusted"] = bool(high_grid and not geometry_failed)
    return status
