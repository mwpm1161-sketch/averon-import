from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from averon_import.core.models import SpecificationRow


class CropArea(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class RecognitionRequest(BaseModel):
    pages: list[int]
    crop: CropArea | None = None
    dpi: int = Field(default=300, ge=150, le=400)
    ocr_mode: Literal["standard", "accurate"] = "standard"


class SaveRowsRequest(BaseModel):
    rows: list[SpecificationRow]


class ExportRequest(BaseModel):
    columns: list[str]
    rows: list[SpecificationRow]
    include_headers: bool = True
    only_exportable: bool = True
    filename: str = "averon_import.xlsx"
    sheet_name: str = "Спецификация"
