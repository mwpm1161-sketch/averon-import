"""Typed evidence emitted while interpreting a physical table header."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class HeaderSourceCell:
    """Immutable provider-neutral input cell used by the semantic layer."""

    physical_row: int
    physical_column: int
    bbox: dict[str, Any]
    raw_text: str
    row_span: int = 1
    column_span: int = 1
    provenance: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    field: str
    score: float
    score_components: tuple[tuple[str, float], ...] = ()
    matched_tokens: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "score": round(float(self.score), 4),
            "score_components": {
                key: round(float(value), 4) for key, value in self.score_components
            },
            "matched_tokens": list(self.matched_tokens),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class HeaderCellEvidence:
    physical_row: int
    physical_column: int
    bbox: dict[str, Any]
    raw_text: str
    visual_lines: tuple[str, ...]
    joined_text: str
    dehyphenated_text: str
    normalized_text: str
    normalized_tokens: tuple[str, ...]
    semantic_candidates: tuple[SemanticCandidate, ...] = ()
    body_evidence: tuple[str, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()
    row_span: int = 1
    column_span: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.physical_row,
            "column": self.physical_column,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "semantic_candidates": [
                candidate.as_dict() for candidate in self.semantic_candidates
            ],
            "body_evidence": list(self.body_evidence),
            "provenance": [dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class HeaderRegionEvidence:
    header_rows: tuple[int, ...]
    cells: tuple[HeaderCellEvidence, ...]
    body_evidence: tuple[str, ...] = ()
    numbering_rows: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "header_rows": list(self.header_rows),
            "body_evidence": list(self.body_evidence),
            "numbering_rows": list(self.numbering_rows),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HeaderMappingResult:
    status: str
    header_rows: tuple[int, ...]
    mapping: dict[int, tuple[str, ...]]
    candidates_by_column: dict[int, tuple[SemanticCandidate, ...]]
    best_score: float
    second_best_score: float
    assignment_margin: float
    unmapped_columns: tuple[int, ...]
    missing_core_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    header_cells: tuple[HeaderCellEvidence, ...] = ()
    candidate_regions: tuple[HeaderRegionEvidence, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def trusted(self) -> bool:
        return self.status == "trusted"

    def as_dict(self) -> dict[str, Any]:
        return {
            "header_candidate_rows": list(self.header_rows),
            "header_cells": [cell.as_dict() for cell in self.header_cells],
            "selected_mapping": {
                str(column): list(fields)
                for column, fields in sorted(self.mapping.items())
            },
            "candidates_by_column": {
                str(column): [candidate.as_dict() for candidate in candidates]
                for column, candidates in sorted(self.candidates_by_column.items())
            },
            "mapping_score": round(float(self.best_score), 4),
            "second_mapping_score": round(float(self.second_best_score), 4),
            "assignment_margin": round(float(self.assignment_margin), 4),
            "mapping_status": self.status,
            "mapping_reasons": list(self.reasons),
            "unmapped_columns": list(self.unmapped_columns),
            "missing_core_fields": list(self.missing_core_fields),
            "candidate_regions": [region.as_dict() for region in self.candidate_regions],
            **dict(self.diagnostics),
        }
