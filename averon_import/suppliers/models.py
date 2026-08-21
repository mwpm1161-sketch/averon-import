from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ProductQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_id: str
    search_text: str
    name: str = ""
    manufacturer: str = ""
    model: str = ""
    article: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)


class ProductOffer(BaseModel):
    model_config = ConfigDict(extra="allow")

    supplier: str
    title: str
    url: HttpUrl | str
    price: Decimal | None = None
    currency: str = "RUB"
    availability: str = ""
    manufacturer: str = ""
    model: str = ""
    article: str = ""
    retrieved_at: datetime = Field(default_factory=datetime.now)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class ProductMatch(BaseModel):
    query: ProductQuery
    offer: ProductOffer
    score: float = Field(ge=0, le=100)
    exact_article: bool = False
    hard_conflicts: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def acceptable(self) -> bool:
        return not self.hard_conflicts and self.score >= 70
