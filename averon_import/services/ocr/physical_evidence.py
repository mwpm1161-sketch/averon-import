"""Provider-neutral physical evidence retained before schema decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from averon_import.services.ocr.table_ir import (
    PhysicalTableIR,
    bbox_as_dict,
    thaw_value,
)


@dataclass(slots=True)
class PhysicalEvidenceSnapshot:
    grid: dict[str, Any] | None = None
    selected_table_bounds: dict[str, Any] = field(default_factory=dict)
    physical_rows: tuple[dict[str, Any], ...] = ()
    spatial_words: tuple[dict[str, Any], ...] = ()
    assigned_word_refs: dict[str, tuple[int, ...]] = field(default_factory=dict)
    header_mapping: dict[str, Any] = field(default_factory=dict)
    observed_schema: dict[str, Any] | None = None
    family_assessment: dict[str, Any] | None = None
    profile_match: dict[str, Any] | None = None
    structural_evidence: dict[str, Any] | None = None
    raster_witness: dict[str, Any] | None = None
    physical_table_ir: PhysicalTableIR | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_physical_table_ir(
        cls,
        physical_table: PhysicalTableIR,
        *,
        header_mapping: dict[str, Any] | None = None,
        observed_schema: dict[str, Any] | None = None,
        family_assessment: dict[str, Any] | None = None,
        profile_match: dict[str, Any] | None = None,
        structural_evidence: dict[str, Any] | None = None,
        raster_witness: dict[str, Any] | None = None,
    ) -> "PhysicalEvidenceSnapshot":
        """Expose the new IR through the existing diagnostics shape.

        This adapter is intentionally one-way and non-invasive: current
        provider consumers can continue reading the historical dictionaries,
        while shadow-mode code can retain the typed physical table itself.
        """

        grid = {
            "source": physical_table.grid_source,
            "x_boundaries": list(physical_table.x_boundaries),
            "y_boundaries": list(physical_table.y_boundaries),
            **thaw_value(physical_table.grid_evidence),
        }
        rows = []
        for row in physical_table.rows:
            rows.append(
                {
                    "source_row_index": row.ref.row_index,
                    "row_index": row.ref.row_index,
                    "bbox": bbox_as_dict(row.bbox),
                    "cells": [
                        {
                            "source_cell_index": cell.ref.source_cell_index,
                            "column_index": cell.ref.column_index,
                            "bbox": bbox_as_dict(cell.bbox),
                            "raw_text": cell.raw_text,
                            "word_refs": [word_ref.source_word_index for word_ref in cell.word_refs],
                            "raster_evidence": thaw_value(cell.raster_evidence),
                        }
                        for cell in row.cells
                    ],
                    "raster_nonempty": row.raster_nonempty,
                    "raster_metrics": thaw_value(row.raster_metrics),
                }
            )
        words = [
            {
                "source_word_index": word.ref.source_word_index,
                "text": word.text,
                "bbox": bbox_as_dict(word.bbox),
                "provider_word_ref": word.provider_word_ref,
            }
            for word in physical_table.words
        ]
        assigned = {
            str(key): tuple(ref.source_word_index for ref in refs)
            for key, refs in physical_table.assigned_word_refs.items()
        }
        return cls(
            grid=grid,
            selected_table_bounds=bbox_as_dict(physical_table.bounds),
            physical_rows=tuple(rows),
            spatial_words=tuple(words),
            assigned_word_refs=assigned,
            header_mapping=dict(header_mapping or {}),
            observed_schema=dict(observed_schema) if observed_schema else None,
            family_assessment=dict(family_assessment) if family_assessment else None,
            profile_match=dict(profile_match) if profile_match else None,
            structural_evidence=thaw_value(structural_evidence or physical_table.structural_evidence),
            raster_witness=thaw_value(raster_witness or physical_table.raster_witness),
            physical_table_ir=physical_table,
        )

    def to_physical_table_ir(self) -> PhysicalTableIR | None:
        """Return the typed table when this snapshot came from the adapter."""

        return self.physical_table_ir

    from_ir = from_physical_table_ir
    to_ir = to_physical_table_ir

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
            "observed_schema": dict(self.observed_schema) if self.observed_schema else None,
            "family_assessment": dict(self.family_assessment) if self.family_assessment else None,
            "profile_match": dict(self.profile_match) if self.profile_match else None,
            "structural_evidence": dict(self.structural_evidence) if self.structural_evidence else None,
            "raster_witness": dict(self.raster_witness) if self.raster_witness else None,
        }
