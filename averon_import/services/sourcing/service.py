from __future__ import annotations

import time
from decimal import Decimal
import hashlib
import json
from typing import Any

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.matching import OfferMatcher, recommended_offer
from averon_import.services.sourcing.models import (
    MatchDecision,
    ProductIntent,
    ProjectSourcingResult,
    SourcingResult,
)
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.providers.base import SourcingProvider


class SourcingService:
    """Coordinates understanding, provider retrieval, matching and safe totals."""

    def __init__(
        self,
        providers: dict[str, SourcingProvider],
        *,
        default_provider: str = "local_catalog",
        ai: SourcingAIService | None = None,
        matcher: OfferMatcher | None = None,
        cache: SourcingCache | None = None,
    ):
        self.providers = providers
        self.default_provider = default_provider
        self.ai = ai or SourcingAIService()
        self.matcher = matcher or OfferMatcher()
        self.cache = cache or SourcingCache()

    def provider(self, key: str | None = None) -> SourcingProvider:
        selected = key or self.default_provider
        if selected not in self.providers:
            raise ValueError(f"Неизвестный sourcing provider: {selected}")
        return self.providers[selected]

    def set_ai(self, ai: SourcingAIService) -> None:
        self.ai = ai

    def understand_row(self, row: dict[str, Any]) -> ProductIntent:
        intent, _ = self.understand_row_with_warnings(row)
        return intent

    def understand_row_with_warnings(self, row: dict[str, Any]) -> tuple[ProductIntent, list[str]]:
        source = dict(row)
        cache_key = self._intent_cache_key(source)
        cached = self.cache.get_intent(cache_key)
        if cached is not None:
            return cached
        fallback = build_fallback_intent(source)
        result = self.ai.understand(source, fallback)
        self.cache.set_intent(cache_key, *result)
        return result

    def search_row(
        self,
        row: dict[str, Any],
        *,
        provider_key: str | None = None,
        limit: int = 20,
    ) -> SourcingResult:
        intent, warnings = self.understand_row_with_warnings(row)
        return self.search_intent(intent, provider_key=provider_key, limit=limit, warnings=warnings)

    def search_intent(
        self,
        intent: ProductIntent,
        *,
        provider_key: str | None = None,
        limit: int = 20,
        warnings: list[str] | None = None,
    ) -> SourcingResult:
        provider = self.provider(provider_key)
        stats = provider.stats() if hasattr(provider, "stats") else {}
        catalog_version = (stats or {}).get("catalog_version", "unknown")
        cache_key = f"search:{provider.key}:{catalog_version}:{intent.fingerprint}:{max(1, min(int(limit), 100))}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"warnings": list(dict.fromkeys([*(warnings or []), *cached.warnings, "Результат взят из актуального cache каталога"]))})
        started = time.perf_counter()
        offers = provider.search(intent, limit=limit)
        retrieval_time = time.perf_counter() - started
        match_results = self.matcher.match(intent, offers)
        match_results, ranking_warnings = self.ai.rank_matches(intent, match_results)
        result = SourcingResult(
            intent=intent,
            recommended_offer=recommended_offer(match_results),
            offers=offers,
            match_results=match_results,
            warnings=list(dict.fromkeys([*(warnings or []), *ranking_warnings])),
            timings={"retrieval_s": round(retrieval_time, 6)},
            ai_mode=("qwen" if self.ai.available else "fallback"),
        )
        self.cache.set(cache_key, result)
        return result

    def search_project(
        self,
        rows: list[dict[str, Any]],
        *,
        provider_key: str | None = None,
        limit: int = 20,
    ) -> ProjectSourcingResult:
        eligible = [
            dict(row) for row in rows
            if row.get("selected", True) is not False
            and row.get("row_type") in {"item", "component", "item_candidate"}
        ]
        results: list[SourcingResult] = []
        warnings: list[str] = []
        totals: dict[str, Decimal] = {}
        matched = alternatives = review = without = 0
        for row in eligible:
            result = self.search_row(row, provider_key=provider_key, limit=limit)
            results.append(result)
            warnings.extend(result.warnings)
            best = result.recommended_offer
            if not result.offers:
                without += 1
            elif best is None:
                review += 1
            else:
                decision = next(
                    (item.decision for item in result.match_results if item.offer.offer_id == best.offer_id),
                    MatchDecision.REVIEW,
                )
                if decision == MatchDecision.ALTERNATIVE:
                    alternatives += 1
                elif decision in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}:
                    matched += 1
                else:
                    review += 1
                quantity = _trusted_quantity(row)
                if quantity is None:
                    warnings.append(f"{result.intent.source_row_id}: quantity requires confirmation")
                    continue
                if best.price is None or not best.currency:
                    continue
                totals[best.currency] = totals.get(best.currency, Decimal("0")) + best.price * quantity
        currencies = sorted(totals)
        if len(currencies) > 1:
            warnings.append("В проекте несколько валют; итог не суммировался в одну сумму")
        return ProjectSourcingResult(
            positions_total=len(eligible),
            positions_processed=len(results),
            positions_matched=matched,
            positions_alternatives=alternatives,
            positions_review=review,
            positions_without_offers=without,
            estimated_total=(totals[currencies[0]] if len(currencies) == 1 else None),
            currency=(currencies[0] if len(currencies) == 1 else None),
            estimated_totals=totals,
            warnings=list(dict.fromkeys(warnings)),
            results=results,
        )

    def public_config(self) -> dict[str, Any]:
        active = self.provider()
        stats = active.stats() if hasattr(active, "stats") else {}
        ai = self.ai.public_config()
        return {
            "provider": {"key": active.key, "label": active.label},
            "providers": [
                {
                    "key": provider.key,
                    "label": provider.label,
                    "catalog_item_count": (provider.stats() or {}).get("item_count", 0),
                    "configured": True,
                }
                for provider in self.providers.values()
            ],
            "catalog_item_count": stats.get("item_count", 0),
            "ai_available": ai["available"],
            "ai": ai,
        }

    def health(self) -> dict[str, Any]:
        return {"ai": self.ai.public_config()}

    def _intent_cache_key(self, row: dict[str, Any]) -> str:
        payload = {
            key: row.get(key, "")
            for key in (
                "id", "source_row_id", "page", "source_row", "name", "title",
                "type_mark", "code", "manufacturer", "unit", "quantity", "note",
            )
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        availability = "ai" if self.ai.available else "fallback"
        return "intent:" + hashlib.sha256(f"{availability}:".encode() + encoded.encode("utf-8")).hexdigest()


def _trusted_quantity(row: dict[str, Any]) -> Decimal | None:
    raw = str(row.get("quantity") or "").strip()
    if not raw or row.get("quantity_trusted") is False:
        return None
    if row.get("status") in {"review", "unrecognized"}:
        return None
    try:
        value = Decimal(raw.replace(",", "."))
    except Exception:
        return None
    return value if value.is_finite() and value >= 0 else None
