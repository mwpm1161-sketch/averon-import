"""Provider-neutral physical evidence retained before schema decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PhysicalEvidenceSnapshot:
    grid: dict[str, Any] | None = None
    selected_table_bounds: dict[str, Any] = field(default_factory=dict)
    physical_rows: tuple[dict[str, Any], ...] = ()
    spatial_words: tuple[dict[str, Any], ...] = ()
    assigned_word_refs: dict[str, tuple[int, ...]] = field(default_factory=dict)
    header_mapping: dict[str, Any] = field(default_factory=dict)
    family_assessment: dict[str, Any] | None = None
    structural_evidence: dict[str, Any] | None = None
    raster_witness: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid": dict(self.grid) if self.grid else None,
            "selected_table_bounds": dict(self.selected_table_bounds),
            "physical_rows": [dict(row) for row in self.physical_rows],
            "spatial_words": [dict(word) for word in self.spatial_words],
            "assigned_word_refs": {
                str(key): list(values) for key, values in self.assigned_word_refs.items()
            },
            "header_mapping": dict(self.header_mapping),
            "family_assessment": dict(self.family_assessment) if self.family_assessment else None,
            "structural_evidence": dict(self.structural_evidence) if self.structural_evidence else None,
            "raster_witness": dict(self.raster_witness) if self.raster_witness else None,
        }
