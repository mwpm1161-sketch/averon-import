from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourcingModel(BaseModel):
    """Strict transport/domain model shared by providers and API layers."""

    model_config = ConfigDict(extra="forbid")


class ProductIntent(SourcingModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_row_id: str
    source_text: str
    product_class: str = ""
    normalized_name: str = ""
    manufacturer: str = ""
    brand: str = ""
    model: str = ""
    article: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    required_attributes: dict[str, Any] = Field(default_factory=dict)
    preferred_attributes: dict[str, Any] = Field(default_factory=dict)
    quantity: str = ""
    unit: str = ""
    search_queries: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("search_queries")
    @classmethod
    def _limit_queries(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()][:4]

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("evidence", None)
        payload.pop("uncertainties", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SuggestionResolution(str, Enum):
    SOURCE_LOCKED = "SOURCE_LOCKED"
    ACCEPTED_GROUNDED = "ACCEPTED_GROUNDED"
    PREFERRED_AI_INFERENCE = "PREFERRED_AI_INFERENCE"
    REJECTED_UNGROUNDED = "REJECTED_UNGROUNDED"
    UNCHANGED = "UNCHANGED"


class ProductUnderstandingSuggestion(SourcingModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    source_value: Any = None
    proposed_value: Any = None
    resolution: SuggestionResolution
    reason: str = ""
    grounded: bool = False


class ProductUnderstandingProvenance(SourcingModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str = ""
    parser_revision: str
    latency_ms: float | None = Field(default=None, ge=0)


class ProductUnderstandingResult(SourcingModel):
    """Auditable envelope around the safe downstream ProductIntent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_intent: ProductIntent
    ai_proposal: ProductIntent | None = None
    resolved_intent: ProductIntent
    suggestions: list[ProductUnderstandingSuggestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    mode: Literal["qwen", "fallback"] = "fallback"
    provenance: ProductUnderstandingProvenance


class Offer(SourcingModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offer_id: str
    provider: str
    source_item_id: str = ""
    title: str
    article: str = ""
    manufacturer: str = ""
    brand: str = ""
    price: Decimal | None = None
    currency: str = ""
    price_unit: str = "шт."
    availability: bool | None = None
    availability_text: str = ""
    url: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("price", mode="before")
    @classmethod
    def _validate_price(cls, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("price must be numeric or null") from exc
        if not result.is_finite() or result < 0:
            raise ValueError("price must be a finite non-negative number")
        return result

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return str(value or "").strip().upper()


class MatchDecision(str, Enum):
    MATCH = "MATCH"
    LIKELY_MATCH = "LIKELY_MATCH"
    ALTERNATIVE = "ALTERNATIVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class MatchResult(SourcingModel):
    offer: Offer
    decision: MatchDecision
    rank: int = Field(ge=1)
    matched_attributes: list[str] = Field(default_factory=list)
    conflicting_attributes: list[str] = Field(default_factory=list)
    missing_attributes: list[str] = Field(default_factory=list)
    explanation: str = ""
    ai_evidence: dict[str, Any] = Field(default_factory=dict)
    deterministic_evidence: dict[str, Any] = Field(default_factory=dict)


class SourcingResult(SourcingModel):
    intent: ProductIntent
    understanding: ProductUnderstandingResult | None = None
    recommended_offer: Offer | None = None
    offers: list[Offer] = Field(default_factory=list)
    match_results: list[MatchResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    ai_mode: str = "fallback"


class ProjectSourcingResult(SourcingModel):
    positions_total: int = 0
    positions_processed: int = 0
    positions_matched: int = 0
    positions_alternatives: int = 0
    positions_review: int = 0
    positions_without_offers: int = 0
    estimated_total: Decimal | None = None
    currency: str | None = None
    estimated_totals: dict[str, Decimal] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    results: list[SourcingResult] = Field(default_factory=list)


class SourcingProviderInfo(SourcingModel):
    key: str
    label: str
    catalog_item_count: int = 0
    configured: bool = True
