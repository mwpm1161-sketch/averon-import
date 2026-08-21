from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AiReviewSuggestion(BaseModel):
    """Non-destructive AI proposal for one recognized specification row."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str | None] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)
    notes: str = ""
    provider: str = ""
    model: str = ""
