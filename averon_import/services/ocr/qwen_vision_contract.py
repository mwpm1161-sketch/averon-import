"""Untrusted image transcription, before projection into Averon's columns.

Array order is the model's proposed reading order, never a physical row ID.
All fields are required (nullable where appropriate) for strict JSON Schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from averon_import.core.constants import BASE_COLUMNS
from averon_import.services.ocr.base import OcrRow


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CandidateBox(_StrictModel):
    """Normalized coordinates within the submitted image; not trusted geometry."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_image(self) -> CandidateBox:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("candidate box outside image")
        return self


class HandwrittenReplacement(_StrictModel):
    """Handwritten text, scoped by its containing cell, row, or page."""

    text: str
    bbox_candidate: CandidateBox | None


class VisionCell(_StrictModel):
    column_index: int = Field(ge=0)
    column_span: int = Field(ge=1)
    row_span: int = Field(ge=1)
    raw_text: str
    alternatives: list[str]
    handwritten_replacements: list[HandwrittenReplacement]
    unreadable: bool
    struck_through: bool
    handwritten: bool
    bbox_candidate: CandidateBox | None


class VisionRow(_StrictModel):
    kind: Literal["item", "component", "continuation", "header", "section", "note", "unknown"]
    logical_row_index: int | None = Field(ge=0)
    continuation_of: int | None = Field(ge=0)
    continues_previous_page: bool
    struck_through: bool
    handwritten: bool
    handwritten_replacements: list[HandwrittenReplacement]
    bbox_candidate: CandidateBox | None
    cells: list[VisionCell]


class VisionTable(_StrictModel):
    headers: list[str]
    rows: list[VisionRow]
    continues_previous_page: bool
    bbox_candidate: CandidateBox | None

    @model_validator(mode="after")
    def validate_cell_addresses(self) -> VisionTable:
        occupied: set[tuple[int, int]] = set()
        for row_index, row in enumerate(self.rows):
            if row.continuation_of is not None and row.continuation_of >= row_index:
                raise ValueError("continuation must refer to a preceding candidate row")
            for cell in row.cells:
                column_stop = cell.column_index + cell.column_span
                row_stop = row_index + cell.row_span
                if column_stop > len(self.headers):
                    raise ValueError("candidate cell outside table")
                if row_stop > len(self.rows):
                    raise ValueError("row span outside candidate table")
                slots = {
                    (covered_row, covered_column)
                    for covered_row in range(row_index, row_stop)
                    for covered_column in range(cell.column_index, column_stop)
                }
                if occupied.intersection(slots):
                    raise ValueError("invalid or overlapping candidate cells")
                occupied.update(slots)
        return self


class VisionPage(_StrictModel):
    page: int = Field(ge=1)
    tables: list[VisionTable]
    unreadable_fragments: list[str]
    handwritten_replacements: list[HandwrittenReplacement]


# Exact aliases only: ambiguity/unknown columns remain in the raw extraction.
_ALIASES = {
    "position": ("позиция", "поз.", "поз", "№", "№ п/п"),
    "name": ("наименование", "наименование и техническая характеристика", "описание"),
    "type_mark": ("тип", "марка", "тип / марка / обозначение", "обозначение"),
    "code": ("код", "код оборудования", "артикул"),
    "manufacturer": ("производитель", "изготовитель", "завод-изготовитель"),
    "unit": ("единица измерения", "ед. изм.", "ед.изм.", "ед. изм"),
    "quantity": ("количество", "кол-во", "кол."),
    "mass": ("масса единицы, кг", "масса единицы", "масса"),
    "note": ("примечание", "примечания"),
}


def _header(value: str) -> str:
    return " ".join(value.casefold().split())


def project_page(page: VisionPage, *, image_crop: dict[str, float] | None = None) -> list[OcrRow]:
    """Project only unambiguous, unchanged single-cell readings; never merge rows.

Raw model content belongs to result metadata, never stats/diagnostics/logs.
The first output row retains the whole intermediate page, including empty
tables and blank rows. Candidate coordinates stay out of OcrRow.bbox.
"""

    lookup = {_header(alias): key for key, aliases in _ALIASES.items() for alias in aliases}
    output: list[OcrRow] = []
    page_handwriting_unlocalized = bool(page.handwritten_replacements)
    for table_index, table in enumerate(page.tables):
        proposed = {index: lookup.get(_header(title)) for index, title in enumerate(table.headers)}
        ambiguous = {key for key in proposed.values() if key and list(proposed.values()).count(key) > 1}
        mapping = {index: key for index, key in proposed.items() if key and key not in ambiguous}
        for row_index, row in enumerate(table.rows):
            if not (row.struck_through or row.handwritten or row.handwritten_replacements or any(
                cell.raw_text or cell.alternatives or cell.handwritten_replacements or cell.unreadable
                or cell.struck_through or cell.handwritten
                for cell in row.cells
            )):
                continue
            values = {column["key"]: "" for column in BASE_COLUMNS}
            reasons = ["qwen_vision_experimental", "structural_layout_ambiguous"]
            if ambiguous:
                reasons.append("ambiguous_columns")
            if row.struck_through or any(cell.struck_through for cell in row.cells):
                reasons.append("vision_struck_through")
            if row.handwritten or any(cell.handwritten for cell in row.cells):
                reasons.append("vision_handwritten")
            if page_handwriting_unlocalized or row.handwritten or row.handwritten_replacements:
                reasons.append("vision_handwriting_unlocalized")
            if any(cell.handwritten_replacements for cell in row.cells):
                reasons.append("vision_handwritten")
            if row.continuation_of is not None or row.continues_previous_page or table.continues_previous_page:
                reasons.append("vision_continuation_unverified")
            suppress_row_values = bool(
                page_handwriting_unlocalized or row.handwritten or row.handwritten_replacements
            )
            for cell in row.cells:
                key = mapping.get(cell.column_index)
                conflicting = (
                    suppress_row_values or row.struck_through or cell.struck_through
                    or cell.handwritten or bool(cell.handwritten_replacements)
                    or cell.unreadable or bool(cell.alternatives)
                    or cell.column_span != 1 or cell.row_span != 1
                )
                if key and not conflicting:
                    values[key] = cell.raw_text
                if cell.unreadable or cell.alternatives:
                    reasons.append("vision_reading_ambiguous")
            metadata = {
                "experimental": True,
                "semantic_authoritative": False,
                "semantic_review": True,
                "physical_row_refs": [],
                # This server-owned flag only protects candidate records from
                # legacy text-fragment merging. It does not verify physical rows.
                "provider_has_explicit_rows": True,
                "vision_rows_are_candidate_observations": True,
                "review_reasons": list(dict.fromkeys(reasons)),
                "vision_candidate_table_index": table_index,
                "vision_candidate_row_index": row_index,
                "vision_headers": list(table.headers),
                "vision_raw_row": row.model_dump(),
                "vision_header_mapping": mapping,
                "vision_ambiguous_fields": sorted(ambiguous),
                "vision_image_crop": image_crop,
                "vision_coordinate_space": "submitted_image_normalized_unverified",
            }
            if not output:
                metadata["vision_raw_page"] = page.model_dump()
            output.append(OcrRow(
                source_row=len(output), values=values, confidences={},
                sources={key: "qwen_vision:experimental" for key, value in values.items() if value},
                bbox={}, metadata=metadata,
            ))
    if not output and (page.tables or page.unreadable_fragments or page.handwritten_replacements):
        # The DTO has no page metadata slot. Preserve headers/unreadable page
        # evidence in an explicitly non-output record, not in diagnostic stats.
        output.append(OcrRow(
            source_row=0, values={column["key"]: "" for column in BASE_COLUMNS},
            confidences={}, sources={}, bbox={}, metadata={
                "experimental": True, "vision_evidence_only": True,
                "semantic_authoritative": False, "semantic_review": True,
                "physical_row_refs": [], "vision_raw_page": page.model_dump(),
                "review_reasons": ["qwen_vision_experimental", "structural_layout_ambiguous"],
            },
        ))
    return output
