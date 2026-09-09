"""Provider-neutral observed header/schema evidence.

An :class:`ObservedSchema` is deliberately weaker than a supported schema.
It records what the physical header appears to mean, without deciding whether
the table is a product schema or whether a field is applicable to every row.
This keeps header interpretation separate from profile applicability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping

from .header_evidence import HeaderMappingResult, SemanticCandidate


_TYPED_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<number>[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\s*"
    r"(?P<unit>м\s*(?:2|3|²|³)|кг|т|шт\.?|м|см|мм|л|м2|м3)\s*$",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: str) -> str:
    return " ".join(_text(value).lower().replace("ё", "е").split())


@dataclass(frozen=True, slots=True)
class TypedMeasurement:
    """A deterministic value/unit pair found in one local source fragment."""

    raw_text: str
    numeric_text: str
    unit_text: str
    role: str = "measurement"
    source_fragment: str = ""
    provenance: tuple[dict[str, Any], ...] = ()

    @classmethod
    def parse(
        cls,
        raw_text: str,
        *,
        role: str = "measurement",
        provenance: Iterable[Mapping[str, Any]] = (),
    ) -> "TypedMeasurement | None":
        """Parse only a complete, local ``number + unit`` expression.

        Arbitrary note prose is intentionally rejected.  This helper never
        infers a unit from a quantity or converts a note heuristically.
        """

        raw = _text(raw_text)
        match = _TYPED_MEASUREMENT_RE.fullmatch(raw)
        if not match:
            return None
        return cls(
            raw_text=raw,
            numeric_text=match.group("number").replace(",", "."),
            unit_text=" ".join(match.group("unit").split()).lower(),
            role=str(role),
            source_fragment=raw,
            provenance=tuple(dict(item) for item in provenance),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "numeric_text": self.numeric_text,
            "unit_text": self.unit_text,
            "role": self.role,
            "source_fragment": self.source_fragment,
            "provenance": [dict(item) for item in self.provenance],
        }


def semantic_concept_for_field(field_name: str, normalized_header: str = "") -> str:
    """Translate legacy mapper names to the richer internal ontology."""

    header = _normalized(normalized_header)
    if field_name == "position" and "поз" in header and "детал" in header:
        return "detail_position"
    if field_name in {"type_mark", "designation"} and header == "обозначение":
        return "designation"
    if field_name in {"type_mark", "product_mark"} and "марка изделия" in header:
        return "product_mark"
    if field_name == "mass" and "масса изделия" in header:
        return "mass_total"
    if field_name == "mass":
        return "mass_per_unit"
    return str(field_name)


@dataclass(frozen=True, slots=True)
class ObservedColumn:
    physical_column: int
    raw_header_fragments: tuple[str, ...] = ()
    normalized_header: str = ""
    semantic_candidates: tuple[SemanticCandidate, ...] = ()
    selected_semantic_concept: str | None = None
    evidence_strength: str = "unknown"
    ambiguity: tuple[str, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "physical_column": self.physical_column,
            "raw_header_fragments": list(self.raw_header_fragments),
            "normalized_header": self.normalized_header,
            "semantic_candidates": [candidate.as_dict() for candidate in self.semantic_candidates],
            "selected_semantic_concept": self.selected_semantic_concept,
            "evidence_strength": self.evidence_strength,
            "ambiguity": list(self.ambiguity),
            "provenance": [dict(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class ObservedSchema:
    """Physical/header evidence before profile applicability is evaluated."""

    columns: tuple[ObservedColumn, ...] = ()
    header_rows: tuple[int, ...] = ()
    mapped_concepts: tuple[str, ...] = ()
    unmapped_columns: tuple[int, ...] = ()
    assignment_margin: float = 0.0
    contradictions: tuple[str, ...] = ()
    mapping_status: str = "unavailable"
    provenance: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping_result(
        cls,
        result: HeaderMappingResult,
        column_count: int | None = None,
        *,
        provenance: Iterable[Mapping[str, Any]] = (),
    ) -> "ObservedSchema":
        cells_by_column: dict[int, list[Any]] = {}
        for cell in result.header_cells:
            cells_by_column.setdefault(int(cell.physical_column), []).append(cell)
        count = max(
            int(column_count or 0),
            max((int(column) for column in result.mapping), default=-1) + 1,
            max((int(column) for column in result.unmapped_columns), default=-1) + 1,
        )
        columns: list[ObservedColumn] = []
        mapped: list[str] = []
        contradictions = list(result.reasons)
        for column in range(count):
            cells = cells_by_column.get(column, [])
            raw = tuple(_text(cell.raw_text) for cell in cells if _text(cell.raw_text))
            normalized = " ".join(_normalized(value) for value in raw).strip()
            candidates: list[SemanticCandidate] = []
            for cell in cells:
                candidates.extend(cell.semantic_candidates)
            unique_candidates: dict[tuple[str, float, tuple[str, ...]], SemanticCandidate] = {}
            for candidate in candidates:
                unique_candidates[(candidate.field, candidate.score, candidate.evidence)] = candidate
            selected_fields = tuple(result.mapping.get(column, ()))
            selected = (
                semantic_concept_for_field(selected_fields[0], normalized)
                if selected_fields
                else None
            )
            if selected:
                mapped.append(selected)
            strong = [candidate for candidate in candidates if candidate.score >= 0.68]
            ambiguity: list[str] = []
            if len(strong) > 1:
                ambiguity.append("multiple_strong_candidates")
            if any("conflict" in reason for reason in result.reasons):
                ambiguity.append("mapping_conflict")
            columns.append(
                ObservedColumn(
                    physical_column=column,
                    raw_header_fragments=raw,
                    normalized_header=normalized,
                    semantic_candidates=tuple(
                        sorted(unique_candidates.values(), key=lambda item: (-item.score, item.field))
                    ),
                    selected_semantic_concept=selected,
                    evidence_strength=(
                        "strong" if selected and any(candidate.field in selected_fields and candidate.score >= 0.68 for candidate in candidates)
                        else "weak" if selected
                        else "unmapped"
                    ),
                    ambiguity=tuple(ambiguity),
                    provenance=tuple(
                        provenance_item
                        for cell in cells
                        for provenance_item in cell.provenance
                    ),
                )
            )
        return cls(
            columns=tuple(columns),
            header_rows=tuple(result.header_rows),
            mapped_concepts=tuple(sorted(set(mapped))),
            unmapped_columns=tuple(sorted(int(column) for column in result.unmapped_columns)),
            assignment_margin=float(result.assignment_margin),
            contradictions=tuple(dict.fromkeys(contradictions)),
            mapping_status=str(result.status),
            provenance=tuple(dict(item) for item in provenance),
        )

    @classmethod
    def from_concepts(
        cls,
        concepts: Iterable[str],
        *,
        columns: Iterable[ObservedColumn] = (),
        header_rows: Iterable[int] = (0,),
        assignment_margin: float = 1.0,
        mapping_status: str = "trusted",
        provenance: Iterable[Mapping[str, Any]] = (),
    ) -> "ObservedSchema":
        return cls(
            columns=tuple(columns),
            header_rows=tuple(int(row) for row in header_rows),
            mapped_concepts=tuple(sorted({str(concept) for concept in concepts})),
            assignment_margin=float(assignment_margin),
            mapping_status=str(mapping_status),
            provenance=tuple(dict(item) for item in provenance),
        )

    def has(self, concept: str) -> bool:
        return str(concept) in set(self.mapped_concepts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": [column.as_dict() for column in self.columns],
            "header_rows": list(self.header_rows),
            "mapped_concepts": list(self.mapped_concepts),
            "unmapped_columns": list(self.unmapped_columns),
            "assignment_margin": round(float(self.assignment_margin), 4),
            "contradictions": list(self.contradictions),
            "mapping_status": self.mapping_status,
            "provenance": [dict(item) for item in self.provenance],
        }


__all__ = [
    "ObservedColumn",
    "ObservedSchema",
    "TypedMeasurement",
    "semantic_concept_for_field",
]
