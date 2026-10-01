from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TenderUnitBasis:
    raw_unit: str
    base_unit: str | None
    dimension: str | None
    scale: str | None
    conversion_basis: str | None
    trusted: bool


@dataclass(frozen=True)
class TenderMapping:
    resource_code: int | None = None
    name: int | None = None
    unit: int | None = None
    quantity: int | None = None
    article: int | None = None
    manufacturer: int | None = None
    model: int | None = None


@dataclass(frozen=True)
class TenderSourceRow:
    source_row_id: str
    sheet_name: str
    excel_row: int
    row_type: Literal["item", "section", "total", "ignored", "invalid"]
    resource_code: str
    name: str
    raw_unit: str
    quantity_raw: str
    quantity: str | None
    quantity_trusted: bool
    article: str
    manufacturer: str
    model: str
    source_cells: dict[str, str] = field(default_factory=dict)
    unit_basis: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TenderSourceManifest:
    source_sha256: str
    sheet_names_order: list[str]
    sheets: list[dict]
    meaningful_cells: list[dict]
    defined_names: list[dict]
    unsupported_preservation_sensitive_objects: list[str]
    selected_sheet: str
    header_row: int
    detected_headers: list[str]


@dataclass(frozen=True)
class TenderWorkbookStructure:
    sheet_name: str
    header_row: int
    headers: list[str]
    mapping: dict[str, int | None]
    logical_right_edge: int
    logical_right_column: str | None
    future_output_columns: list[dict]


@dataclass(frozen=True)
class TenderPreview:
    preview_id: str
    owner_id: str
    filename: str
    source_sha256: str
    file_size_bytes: int
    expires_at: str
    parser_version: int | None
    state: str
