from __future__ import annotations

import time
from decimal import Decimal
import hashlib
import json
from typing import Any, Callable

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.matching import (
    OfferMatcher,
    recommended_offer,
    review_candidate,
)
from averon_import.services.sourcing.models import (
    MatchDecision,
    ProductIntent,
    ProductUnderstandingResult,
    ProjectSourcingResult,
    SourcingRankingResult,
    SourcingNotice,
    SourcingProviderRuntimeState,
    SourcingResult,
    dedupe_sourcing_notices,
)
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.providers.base import (
    SourcingProvider,
    SourcingProviderError,
    get_provider_capabilities,
    normalize_provider_runtime_state,
)


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
            notices=list(understanding.notices),
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
        notices: list[SourcingNotice] | None = None,
        understanding: ProductUnderstandingResult | None = None,
        ai_rerank: bool = True,
        catalog_version: str | None = None,
    ) -> SourcingResult:
        provider = self.provider(provider_key)
        if catalog_version is None:
            try:
                stats = provider.stats() if hasattr(provider, "stats") else {}
                runtime_state = normalize_provider_runtime_state(stats)
                if runtime_state.reachable is False:
                    raise SourcingProviderError(
                        runtime_state.error or "Поставщик недоступен",
                        category="health_error",
                    )
                catalog_version = runtime_state.catalog_version
            except (SourcingProviderError, ValueError) as exc:
                return self._provider_failure_result(intent, understanding, warnings, notices, exc)
            except Exception as exc:
                return self._provider_failure_result(
                    intent,
                    understanding,
                    warnings,
                    notices,
                    _as_provider_error(exc),
                )
        cache_key = f"search:{provider.key}:{catalog_version}:{intent.fingerprint}:{max(1, min(int(limit), 100))}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return self._compose_cached_result(cached, understanding, warnings, notices)
        started = time.perf_counter()
        try:
            offers = provider.search(intent, limit=limit)
        except (SourcingProviderError, ValueError) as exc:
            return self._provider_failure_result(intent, understanding, warnings, notices, exc)
        except Exception as exc:
            return self._provider_failure_result(
                intent,
                understanding,
                warnings,
                notices,
                _as_provider_error(exc),
            )
        retrieval_time = time.perf_counter() - started
        matching_started = time.perf_counter()
        match_results = self.matcher.match(intent, offers)
        matching_time = time.perf_counter() - matching_started
        ranking_warnings: list[str] = []
        ranking_notices: list[SourcingNotice] = []
        ranking_time = 0.0
        if ai_rerank:
            ranking_started = time.perf_counter()
            ranking_result = self.ai.rank_matches(intent, match_results)
            if isinstance(ranking_result, SourcingRankingResult):
                match_results = ranking_result.matches
                ranking_warnings = ranking_result.warnings
                ranking_notices = ranking_result.notices
            else:
                # Compatibility for injected legacy AI adapters that still
                # return the original two-value tuple contract.
                match_results, ranking_warnings, *extra_notices = ranking_result
                ranking_notices = list(extra_notices[0]) if extra_notices else []
            ranking_time = time.perf_counter() - ranking_started
        result = SourcingResult(
            intent=intent,
            understanding=understanding,
            recommended_offer=recommended_offer(match_results),
            review_candidate=review_candidate(match_results),
            offers=offers,
            match_results=match_results,
            warnings=list(dict.fromkeys([*(warnings or []), *ranking_warnings])),
            notices=dedupe_sourcing_notices([*(notices or []), *ranking_notices]),
            timings={
                "retrieval_s": round(retrieval_time, 6),
                "matching_s": round(matching_time, 6),
                "ai_rank_s": round(ranking_time, 6),
                "search_cache_hit": 0.0,
            },
            ai_mode=self._current_ai_mode(understanding),
        )
        # Catalog offers and deterministic match facts are reusable. Audit
        # Provenance, parser warnings and notices belong to the current response only.
        self.cache.set(
            cache_key,
            result.model_copy(update={
                "understanding": None,
                "warnings": [],
                "notices": [],
                "ai_mode": "fallback",
            }),
        )
        return result

    @staticmethod
    def _current_ai_mode(understanding: ProductUnderstandingResult | None) -> str:
        return understanding.mode if understanding is not None else "fallback"

    def _provider_failure_result(
        self,
        intent: ProductIntent,
        understanding: ProductUnderstandingResult | None,
        warnings: list[str] | None,
        notices: list[SourcingNotice] | None,
        exc: ValueError,
    ) -> SourcingResult:
        return SourcingResult(
            intent=intent,
            understanding=understanding,
            warnings=list(dict.fromkeys([_safe_provider_warning(exc), *(warnings or [])])),
            notices=dedupe_sourcing_notices([
                *(notices or []),
                _provider_notice(exc),
            ]),
            timings={"provider_error": 1.0, "search_cache_hit": 0.0},
            ai_mode=self._current_ai_mode(understanding),
        )

    def _compose_cached_result(
        self,
        cached: SourcingResult,
        understanding: ProductUnderstandingResult | None,
        warnings: list[str] | None,
        notices: list[SourcingNotice] | None,
    ) -> SourcingResult:
        # Deliberately overwrite any legacy cached understanding/ai_mode too:
        # old cache entries must not leak stale parser provenance.
        merged_warnings = list(dict.fromkeys([
            *(warnings or []),
            "Результат взят из актуального cache каталога",
        ]))
        merged_notices = dedupe_sourcing_notices([
            *(notices or []),
            SourcingNotice(
                code="CATALOG_CACHE_HIT",
                severity="info",
                message="Результат взят из актуального cache каталога",
                user_visible=False,
            ),
        ])
        return cached.model_copy(update={
            "understanding": understanding,
            "ai_mode": self._current_ai_mode(understanding),
            "warnings": merged_warnings,
            "notices": merged_notices,
            "timings": {**cached.timings, "search_cache_hit": 1.0},
        })

    def search_project(
        self,
        rows: list[dict[str, Any]],
        *,
        provider_key: str | None = None,
        limit: int = 20,
        progress: Callable[[int, int, str], None] | None = None,
        telemetry: Callable[[dict[str, Any]], None] | None = None,
        catalog_version: str | None = None,
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
        catalog_version = catalog_version or self._project_catalog_version(provider)
        if progress:
            progress(0, total, "Проверяем каталог предложений")
        results: list[SourcingResult] = []
        warnings: list[str] = []
        notices: list[SourcingNotice] = []
        confirmed_totals: dict[str, Decimal] = {}
        alternative_totals: dict[str, Decimal] = {}
        matched = alternatives = review = without = 0
        matched_unpriced = alternative_unpriced = unresolved = 0
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
                    notices=list(understanding.notices),
                    understanding=understanding,
                    ai_rerank=ai_rerank,
                    catalog_version=catalog_version,
                )
            except ValueError as exc:
                result = self._provider_failure_result(
                    understanding.resolved_intent,
                    understanding,
                    list(understanding.warnings),
                    list(understanding.notices),
                    exc,
                )
            results.append(result)
            warnings.extend(result.warnings)
            notices.extend(result.notices)
            if result.timings.get("search_cache_hit"):
                search_cache_hits += 1
            else:
                retrieval_time += result.timings.get("retrieval_s", 0.0)
                matching_time += result.timings.get("matching_s", 0.0)
                ranking_time += result.timings.get("ai_rank_s", 0.0)
            best = result.recommended_offer
            decision = "WITHOUT_OFFERS"
            decision_match = None
            if not result.offers:
                without += 1
                unresolved += 1
            elif best is None:
                decision = "REVIEW"
                review += 1
                unresolved += 1
            else:
                decision_match = next(
                    (item.decision for item in result.match_results if item.offer.offer_id == best.offer_id),
                    MatchDecision.REVIEW,
                )
                decision = decision_match.value
                if decision_match == MatchDecision.ALTERNATIVE:
                    alternatives += 1
                elif decision_match in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}:
                    matched += 1
                else:
                    review += 1
                    unresolved += 1
                quantity = _trusted_quantity(row)
                if quantity is None:
                    warnings.append(f"{result.intent.source_row_id}: quantity requires confirmation")
                    notices.append(SourcingNotice(
                        code="QUANTITY_REQUIRES_CONFIRMATION",
                        message="Для одной или нескольких позиций требуется подтвердить количество.",
                    ))
                elif best.price is not None and best.currency:
                    target = (
                        confirmed_totals
                        if decision_match in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}
                        else alternative_totals
                        if decision_match == MatchDecision.ALTERNATIVE
                        else None
                    )
                    if target is not None:
                        target[best.currency] = target.get(best.currency, Decimal("0")) + best.price * quantity
                elif decision_match in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}:
                    matched_unpriced += 1
                elif decision_match == MatchDecision.ALTERNATIVE:
                    alternative_unpriced += 1
            if telemetry:
                telemetry(_row_telemetry(
                    row,
                    understanding,
                    understanding_cache_hit,
                    result,
                    decision,
                    decision_match,
                    current_ai_mode=str(self.ai.cache_identity().get("mode") or "fallback"),
                ))
            if progress:
                progress(index + 1, total, f"Готово {index + 1} из {total}: {label}")
        confirmed_currencies = sorted(confirmed_totals)
        alternative_currencies = sorted(alternative_totals)
        if len(confirmed_currencies) > 1 or len(alternative_currencies) > 1:
            warnings.append("В проекте несколько валют; итог не суммировался в одну сумму")
            notices.append(SourcingNotice(
                code="MULTIPLE_CURRENCIES",
                message="В проекте несколько валют; итоговые суммы показаны раздельно и не суммируются.",
            ))
        confirmed_total = (
            confirmed_totals[confirmed_currencies[0]]
            if len(confirmed_currencies) == 1
            else None
        )
        alternative_total = (
            alternative_totals[alternative_currencies[0]]
            if len(alternative_currencies) == 1
            else None
        )
        return ProjectSourcingResult(
            positions_total=len(eligible),
            positions_processed=len(results),
            positions_matched=matched,
            positions_alternatives=alternatives,
            positions_review=review,
            positions_without_offers=without,
            confirmed_total=confirmed_total,
            confirmed_totals=confirmed_totals,
            confirmed_currency=(confirmed_currencies[0] if len(confirmed_currencies) == 1 else None),
            alternative_total=alternative_total,
            alternative_totals=alternative_totals,
            alternative_currency=(alternative_currencies[0] if len(alternative_currencies) == 1 else None),
            matched_unpriced_count=matched_unpriced,
            alternative_unpriced_count=alternative_unpriced,
            unresolved_count=unresolved,
            # Compatibility: estimated_total is the confirmed subtotal only.
            estimated_total=confirmed_total,
            currency=(confirmed_currencies[0] if len(confirmed_currencies) == 1 else None),
            estimated_totals=confirmed_totals,
            warnings=list(dict.fromkeys(warnings)),
            notices=dedupe_sourcing_notices(notices),
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
            provider_key=provider.key,
            provider_label=provider.label,
            catalog_version=catalog_version,
        )

    def _project_catalog_version(self, provider: SourcingProvider) -> str:
        try:
            stats = provider.stats() if hasattr(provider, "stats") else {}
        except SourcingProviderError:
            raise
        except Exception as exc:
            raise SourcingProviderError(
                "Проверка каталога поставщика не выполнена",
                category="health_error",
            ) from exc
        runtime_state = normalize_provider_runtime_state(stats)
        if runtime_state.reachable is False:
            raise SourcingProviderError(
                runtime_state.error or "Поставщик недоступен",
                category="health_error",
            )
        return runtime_state.catalog_version

    def public_config(self) -> dict[str, Any]:
        active = self.provider()
        ai = self.ai.public_config()
        provider_rows = []
        stats_by_key: dict[str, SourcingProviderRuntimeState] = {}
        for provider in self.providers.values():
            provider_state = self._safe_stats(provider)
            stats_by_key[provider.key] = provider_state
            provider_rows.append(
                {
                    "key": provider.key,
                    "label": provider.label,
                    "catalog_item_count": provider_state.item_count,
                    "configured": provider_state.configured,
                    "capabilities": get_provider_capabilities(provider).model_dump(mode="json"),
                    "reachable": provider_state.reachable,
                    **(
                        {"error": provider_state.error}
                        if provider_state.error
                        else {}
                    ),
                }
            )
        return {
            "provider": {
                "key": active.key,
                "label": active.label,
                "capabilities": get_provider_capabilities(active).model_dump(mode="json"),
            },
            "providers": provider_rows,
            "catalog_item_count": stats_by_key.get(
                active.key,
                SourcingProviderRuntimeState(),
            ).item_count,
            "ai_available": ai["available"],
            "ai": ai,
        }

    @staticmethod
    def _safe_stats(provider: SourcingProvider) -> SourcingProviderRuntimeState:
        try:
            stats_method = getattr(provider, "stats", None)
            if not callable(stats_method):
                return SourcingProviderRuntimeState()
            stats = stats_method()
            return normalize_provider_runtime_state(stats)
        except Exception:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                catalog_version="unavailable",
                error="Проверка каталога поставщика не выполнена",
            )

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
    message = " ".join(str(getattr(exc, "public_message", "") or str(exc)).split())
    lowered = message.casefold()
    if not message or len(message) > 240 or any(
        marker in lowered for marker in ("api-key", "authorization", "secret", "token", "password")
    ):
        message = "провайдер не вернул предложения"
    return f"Ошибка поиска: {message}"


def _as_provider_error(exc: Exception) -> SourcingProviderError:
    if isinstance(exc, SourcingProviderError):
        return exc
    return SourcingProviderError(
        "Поставщик не вернул предложения",
        category="provider_error",
    )


def _provider_notice(_exc: ValueError) -> SourcingNotice:
    """Return a safe actionable notice without exposing provider internals."""

    return SourcingNotice(
        code="PROVIDER_ERROR",
        message=(
            "Не удалось получить предложения от поставщика. "
            "Проверьте доступность каталога или повторите поиск."
        ),
    )


def _row_telemetry(
    row: dict[str, Any],
    understanding: ProductUnderstandingResult,
    understanding_cache_hit: bool,
    result: SourcingResult,
    decision: str,
    decision_match: MatchDecision | None,
    *,
    current_ai_mode: str,
) -> dict[str, Any]:
    match = None
    if decision_match in {
        MatchDecision.MATCH,
        MatchDecision.LIKELY_MATCH,
        MatchDecision.ALTERNATIVE,
    } and result.recommended_offer is not None:
        match = next(
            (
                item for item in result.match_results
                if item.offer.offer_id == result.recommended_offer.offer_id
            ),
            None,
        )
    elif decision == MatchDecision.REVIEW.value:
        match = result.review_candidate
    kind = _understanding_provenance_kind(
        understanding,
        understanding_cache_hit,
        current_ai_mode=current_ai_mode,
    )
    provenance = understanding.provenance
    deterministic_evidence = match.deterministic_evidence if match else {}
    preferred = deterministic_evidence.get("preferred_differences", [])
    if not isinstance(preferred, (list, tuple, set)):
        preferred = []
    return {
        "source_row_id": understanding.resolved_intent.source_row_id,
        "source_page": row.get("page"),
        "source_row": row.get("source_row") if row.get("source_row") not in (None, "") else row.get("source_row_index"),
        "intent_fingerprint": understanding.resolved_intent.fingerprint,
        "ai_mode": understanding.mode,
        "understanding_cache_hit": understanding_cache_hit,
        "understanding_provenance": {
            "kind": kind,
            "provider": provenance.provider,
            "model": provenance.model,
            "parser_revision": provenance.parser_revision,
            "latency_ms": provenance.latency_ms,
        },
        "search_cache_hit": bool(result.timings.get("search_cache_hit")),
        "decision": decision,
        "recommended_offer_id": result.recommended_offer.offer_id if result.recommended_offer else None,
        "review_candidate_offer_id": result.review_candidate.offer.offer_id if result.review_candidate else None,
        "matched_attributes": list(match.matched_attributes) if match else [],
        "missing_attributes": list(match.missing_attributes) if match else [],
        "conflicting_attributes": list(match.conflicting_attributes) if match else [],
        "preferred_differences": preferred,
    }


def _understanding_provenance_kind(
    understanding: ProductUnderstandingResult,
    cache_hit: bool,
    *,
    current_ai_mode: str,
) -> str:
    if cache_hit:
        return "cached_qwen" if understanding.mode == "qwen" else "cached_fallback"
    if understanding.mode == "qwen":
        return "fresh_qwen"
    if current_ai_mode == "qwen":
        return "fresh_fallback_after_qwen_failure"
    return "offline_deterministic_fallback"
