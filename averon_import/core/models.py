from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BoundingBox(BaseModel):
    """Normalized coordinates of a source row on a rendered PDF page."""

    model_config = ConfigDict(extra="allow")

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)


class SpecificationRow(BaseModel):
    """Stable application model for one specification row.

    ``extra='allow'`` keeps the model forward-compatible while the application
    evolves (AI evidence, supplier offers, etc.), but the core fields are typed
    so API boundaries no longer accept arbitrary row shapes silently.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    position: str = ""
    name: str = ""
    type_mark: str = ""
    code: str = ""
    manufacturer: str = ""
    unit: str = ""
    quantity: str = ""
    mass: str = ""
    note: str = ""
    section: str = ""
    system: str = ""
    row_type: str = "item"
    page: int = Field(ge=1)
    confidence: float = Field(default=0.0, ge=0, le=100)
    status: str = "review"
    bbox: BoundingBox | dict[str, Any] | None = None
    confidences: dict[str, float] = Field(default_factory=dict)
    ocr_sources: dict[str, str] = Field(default_factory=dict)
    source_row: int | str | None = None
    edited: bool = False
    selected: bool | None = None


class RecognitionResult(BaseModel):
    """Persisted recognition result contract."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(ge=1)
    pages: list[int]
    rows: list[SpecificationRow]
    page_tables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    ocr_mode: str = "standard"
