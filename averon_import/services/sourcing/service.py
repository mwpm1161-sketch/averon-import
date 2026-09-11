from __future__ import annotations

import time
from decimal import Decimal
import hashlib
import json
from typing import Any, Callable

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.matching import OfferMatcher, recommended_offer
from averon_import.services.sourcing.models import (
    MatchDecision,
    ProductIntent,
    ProductUnderstandingResult,
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

    def understand_row_result(self, row: dict[str, Any]) -> ProductUnderstandingResult:
        result, _ = self._understand_row_result_with_cache(row)
        return result

    def _understand_row_result_with_cache(
        self,
        row: dict[str, Any],
    ) -> tuple[ProductUnderstandingResult, bool]:
        source = dict(row)
        cache_key = self._intent_cache_key(source)
        current_ai_identity = self.ai.cache_identity()
        cached = self.cache.get_understanding(cache_key)
        if cached is not None and self._can_reuse_understanding(cached, current_ai_identity):
            return cached, True
        fallback = build_fallback_intent(source)
        result = self.ai.understand_with_audit(source, fallback)
        if self._can_persist_understanding(result, current_ai_identity):
            self.cache.set_understanding(cache_key, result)
        return result, False

    @staticmethod
    def _can_reuse_understanding(
        cached: ProductUnderstandingResult,
        current_ai_identity: dict[str, Any],
    ) -> bool:
        """Reject fallback entries stored under an active Qwen identity."""

        return not (
            str(current_ai_identity.get("mode") or "") == "qwen"
            and cached.mode == "fallback"
        )

    @staticmethod
    def _can_persist_understanding(
        result: ProductUnderstandingResult,
        current_ai_identity: dict[str, Any],
    ) -> bool:
        """Persist fallback only when fallback is the configured cache mode."""

        return not (
            str(current_ai_identity.get("mode") or "") == "qwen"
            and result.mode == "fallback"
        )

    def understand_row_with_warnings(self, row: dict[str, Any]) -> tuple[ProductIntent, list[str]]:
        result = self.understand_row_result(row)
        return result.resolved_intent, list(result.warnings)

    def search_row(
        self,
        row: dict[str, Any],
        *,
        provider_key: str | None = None,
        limit: int = 20,
        ai_rerank: bool = True,
        catalog_version: str | None = None,
    ) -> SourcingResult:
        understanding = self.understand_row_result(row)
        return self.search_intent(
            understanding.resolved_intent,
            provider_key=provider_key,
            limit=limit,
            warnings=list(understanding.warnings),
            understanding=understanding,
            ai_rerank=ai_rerank,
            catalog_version=catalog_version,
        )

    def search_intent(
        self,
        intent: ProductIntent,
        *,
        provider_key: str | None = None,
        limit: int = 20,
        warnings: list[str] | None = None,
        understanding: ProductUnderstandingResult | None = None,
        ai_rerank: bool = True,
        catalog_version: str | None = None,
    ) -> SourcingResult:
        provider = self.provider(provider_key)
        if catalog_version is None:
            stats = provider.stats() if hasattr(provider, "stats") else {}
            catalog_version = (stats or {}).get("catalog_version", "unknown")
        cache_key = f"search:{provider.key}:{catalog_version}:{intent.fingerprint}:{max(1, min(int(limit), 100))}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return self._compose_cached_result(cached, understanding, warnings)
        started = time.perf_counter()
        offers = provider.search(intent, limit=limit)
        retrieval_time = time.perf_counter() - started
        matching_started = time.perf_counter()
        match_results = self.matcher.match(intent, offers)
        matching_time = time.perf_counter() - matching_started
        ranking_warnings: list[str] = []
        ranking_time = 0.0
        if ai_rerank:
            ranking_started = time.perf_counter()
            match_results, ranking_warnings = self.ai.rank_matches(intent, match_results)
            ranking_time = time.perf_counter() - ranking_started
        result = SourcingResult(
            intent=intent,
            understanding=understanding,
            recommended_offer=recommended_offer(match_results),
            offers=offers,
            match_results=match_results,
            warnings=list(dict.fromkeys([*(warnings or []), *ranking_warnings])),
            timings={
                "retrieval_s": round(retrieval_time, 6),
                "matching_s": round(matching_time, 6),
                "ai_rank_s": round(ranking_time, 6),
                "search_cache_hit": 0.0,
            },
            ai_mode=self._current_ai_mode(understanding),
        )
        # Catalog offers and deterministic match facts are reusable. Audit
        # provenance and parser warnings belong to the current response only.
        self.cache.set(
            cache_key,
            result.model_copy(update={"understanding": None, "warnings": [], "ai_mode": "fallback"}),
        )
        return result

    @staticmethod
    def _current_ai_mode(understanding: ProductUnderstandingResult | None) -> str:
        return understanding.mode if understanding is not None else "fallback"

    def _compose_cached_result(
        self,
        cached: SourcingResult,
        understanding: ProductUnderstandingResult | None,
        warnings: list[str] | None,
    ) -> SourcingResult:
        # Deliberately overwrite any legacy cached understanding/ai_mode too:
        # old cache entries must not leak stale parser provenance.
        merged_warnings = list(dict.fromkeys([
            *(warnings or []),
            "Результат взят из актуального cache каталога",
        ]))
        return cached.model_copy(update={
            "understanding": understanding,
            "ai_mode": self._current_ai_mode(understanding),
            "warnings": merged_warnings,
            "timings": {**cached.timings, "search_cache_hit": 1.0},
        })

    def search_project(
        self,
        rows: list[dict[str, Any]],
        *,
        provider_key: str | None = None,
        limit: int = 20,
        progress: Callable[[int, int, str], None] | None = None,
        ai_rerank: bool = False,
    ) -> ProjectSourcingResult:
        eligible = [
            dict(row) for row in rows
            if row.get("selected", True) is not False
            and row.get("row_type") in {"item", "component", "item_candidate"}
        ]
        total = len(eligible)
        started_project = time.perf_counter()
        provider = self.provider(provider_key)
        catalog_version = self._project_catalog_version(provider)
        if progress:
            progress(0, total, "Проверяем каталог предложений")
        results: list[SourcingResult] = []
        warnings: list[str] = []
        totals: dict[str, Decimal] = {}
        matched = alternatives = review = without = 0
        understanding_time = retrieval_time = matching_time = ranking_time = 0.0
        understanding_cache_hits = search_cache_hits = 0
        for index, row in enumerate(eligible):
            label = _project_row_label(row)
            if progress:
                progress(index, total, f"Анализируем позицию {index + 1} из {total}: {label}")
            understanding_started = time.perf_counter()
            understanding, understanding_cache_hit = self._understand_row_result_with_cache(row)
            understanding_time += time.perf_counter() - understanding_started
            if understanding_cache_hit:
                understanding_cache_hits += 1
            try:
                result = self.search_intent(
                    understanding.resolved_intent,
                    provider_key=provider_key,
                    limit=limit,
                    warnings=list(understanding.warnings),
                    understanding=understanding,
                    ai_rerank=ai_rerank,
                    catalog_version=catalog_version,
                )
            except ValueError as exc:
                result = SourcingResult(
                    intent=understanding.resolved_intent,
                    understanding=understanding,
                    warnings=[_safe_provider_warning(exc)],
                    timings={"provider_error": 1.0, "search_cache_hit": 0.0},
                    ai_mode=self._current_ai_mode(understanding),
                )
            results.append(result)
            warnings.extend(result.warnings)
            if result.timings.get("search_cache_hit"):
                search_cache_hits += 1
            else:
                retrieval_time += result.timings.get("retrieval_s", 0.0)
                matching_time += result.timings.get("matching_s", 0.0)
                ranking_time += result.timings.get("ai_rank_s", 0.0)
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
                elif best.price is not None and best.currency:
                    totals[best.currency] = totals.get(best.currency, Decimal("0")) + best.price * quantity
            if progress:
                progress(index + 1, total, f"Готово {index + 1} из {total}: {label}")
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
            timings={
                "total_s": round(time.perf_counter() - started_project, 6),
                "understanding_s": round(understanding_time, 6),
                "retrieval_s": round(retrieval_time, 6),
                "matching_s": round(matching_time, 6),
                "ai_rank_s": round(ranking_time, 6),
                "understanding_cache_hits": float(understanding_cache_hits),
                "search_cache_hits": float(search_cache_hits),
                "provider_stats_calls": 1.0,
            },
        )

    def _project_catalog_version(self, provider: SourcingProvider) -> str:
        try:
            stats = provider.stats() if hasattr(provider, "stats") else {}
        except Exception as exc:
            if provider.key == "demo_store_http":
                raise ValueError("Averon Demo Store: проверка каталога не выполнена") from exc
            return "unknown"
        if not isinstance(stats, dict):
            stats = {}
        if provider.key == "demo_store_http" and stats.get("reachable") is False:
            raise ValueError(str(stats.get("error") or "Averon Demo Store: каталог недоступен"))
        return str(stats.get("catalog_version") or "unknown")

    def public_config(self) -> dict[str, Any]:
        active = self.provider()
        ai = self.ai.public_config()
        provider_rows = []
        stats_by_key: dict[str, dict[str, Any]] = {}
        for provider in self.providers.values():
            provider_stats = self._safe_stats(provider)
            stats_by_key[provider.key] = provider_stats
            provider_rows.append(
                {
                    "key": provider.key,
                    "label": provider.label,
                    "catalog_item_count": provider_stats.get("item_count", 0),
                    "configured": provider_stats.get("configured", True),
                    "reachable": provider_stats.get("reachable", True),
                    **(
                        {"error": provider_stats["error"]}
                        if provider_stats.get("error")
                        else {}
                    ),
                }
            )
        return {
            "provider": {"key": active.key, "label": active.label},
            "providers": provider_rows,
            "catalog_item_count": stats_by_key.get(active.key, {}).get("item_count", 0),
            "ai_available": ai["available"],
            "ai": ai,
        }

    @staticmethod
    def _safe_stats(provider: SourcingProvider) -> dict[str, Any]:
        if not hasattr(provider, "stats"):
            return {}
        try:
            stats = provider.stats()
        except Exception:
            return {
                "configured": True,
                "reachable": False,
                "item_count": 0,
                "catalog_version": "unavailable",
                "error": "provider health unavailable",
            }
        return stats if isinstance(stats, dict) else {}

    def health(self) -> dict[str, Any]:
        return {"ai": self.ai.public_config()}

    def _intent_cache_key(self, row: dict[str, Any]) -> str:
        payload = {
            key: row.get(key, "")
            for key in (
                "id", "source_row_id", "source_text", "page", "source_row", "name", "title",
                "type_mark", "model", "code", "manufacturer", "unit", "quantity", "note",
            )
        }
        identity = {
            "source": payload,
            "parser": self.ai.cache_identity(),
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        return "intent:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def _project_row_label(row: dict[str, Any]) -> str:
    for key in ("name", "type_mark", "model", "source_text", "id"):
        value = " ".join(str(row.get(key) or "").split())
        if value:
            return value[:120]
    return "без названия"


def _safe_provider_warning(exc: ValueError) -> str:
    message = " ".join(str(exc).split())
    lowered = message.casefold()
    if not message or len(message) > 240 or any(
        marker in lowered for marker in ("api-key", "authorization", "secret", "token", "password")
    ):
        message = "провайдер не вернул предложения"
    return f"Ошибка поиска: {message}"
