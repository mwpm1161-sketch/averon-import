from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AiCorrectedRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    values: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class AiCorrectionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rows: list[AiCorrectedRow] = Field(default_factory=list)
