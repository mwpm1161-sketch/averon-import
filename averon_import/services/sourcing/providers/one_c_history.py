"""Read-only retrieval and conservative matching for historical 1C purchases."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
import re
import threading
import unicodedata

from rapidfuzz import fuzz

from averon_import.core.unit_normalization import normalize_unit_family
from averon_import.services.one_c_history.read_model import (
    OneCHistoryCatalogSnapshot,
    OneCHistoryEvent,
    OneCHistoryItem,
    OneCHistoryReadError,
    OneCHistoryVariant,
)
from averon_import.services.one_c_history.repository import OneCHistoryRepository
from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import (
    HistorySafeMatchBasis,
    HistoryRetrievalClassification,
    MatchDecision,
    MatchResult,
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)
from averon_import.services.sourcing.history_identity import (
    HISTORY_IDENTITY_NORMALIZER_REVISION,
    history_name_signature,
    history_name_signature_digest,
)
from averon_import.services.sourcing.history_policy import MAX_FUZZY_RETRIEVAL_RANK
from averon_import.services.sourcing.providers.base import SourcingProviderCachePolicy


_MAX_PROVIDER_LIMIT = MAX_FUZZY_RETRIEVAL_RANK
_MIN_FUZZY_SCORE = 58
_COMPANY_DEFAULT_HISTORY_CURRENCY = "RUB"
_MATCHER_ARTICLE_TRANSLATION = str.maketrans({
    "а": "a", "в": "v", "е": "e", "з": "z", "и": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "х": "x", "э": "e",
})


def _effective_history_currency(source_currency: str | None) -> tuple[str, str]:
    """Resolve currency for the sourcing offer without mutating source history."""
    if source_currency is not None and source_currency.strip():
        return source_currency, "source"
    return _COMPANY_DEFAULT_HISTORY_CURRENCY, "company_default"


class HistoryMatchOutcome(str, Enum):
    SAFE_MATCH = "SAFE_MATCH"
    REVIEW = "REVIEW"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True, slots=True)
class HistoryLookupResult:
    outcome: HistoryMatchOutcome
    available: bool
    selected_offer: Offer | None = None
    candidates: tuple[Offer, ...] = ()
    match_results: tuple[MatchResult, ...] = ()
    reason_code: str = ""
    safe_basis: HistorySafeMatchBasis | None = None
    catalog_version: str = ""


@dataclass(frozen=True, slots=True)
class _IndexedVariant:
    variant: OneCHistoryVariant
    exact_name_key: str
    name_key: str
    characteristic_key: str
    article_key: str
    unit_family: str | None
    matcher_article_key: str
    normalized_name_signature: str | None
    normalized_unit_family: str | None


@dataclass(frozen=True, slots=True)
class _IndexedItem:
    item: OneCHistoryItem
    searchable_variants: tuple[OneCHistoryVariant, ...]
    indexed_variants: tuple[_IndexedVariant, ...]
    selected_price_event: OneCHistoryEvent | None


@dataclass(frozen=True, slots=True)
class _Projection:
    version: str
    items: tuple[_IndexedItem, ...]
    exact_article_index: Mapping[str, tuple[tuple[int, int], ...]]
    matcher_article_item_ids: Mapping[str, frozenset[str]]
    exact_name_unit_index: Mapping[tuple[str, str], tuple[tuple[int, int], ...]]
    loose_name_unit_item_ids: Mapping[tuple[str, str], frozenset[str]]
    structured_identity_index: Mapping[tuple[str, str, str], tuple[tuple[int, int], ...]]
    normalized_name_unit_index: Mapping[tuple[str, str], tuple[tuple[int, int], ...]]


@dataclass(frozen=True, slots=True)
class _RetrievedItem:
    indexed: _IndexedItem
    variant: OneCHistoryVariant
    score: float
    classification: HistoryRetrievalClassification = HistoryRetrievalClassification.FUZZY


def normalize_product_search_text(value: object) -> str:
    """Conservatively normalize product text without discarding identifiers."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(text.replace("×", "x").replace("х", "x").split())


def normalize_exact_source_name(value: object) -> str:
    """Exact source-name key; punctuation, token order, and model characters stay significant."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(text.replace("\u00a0", " ").split())


def _loose_name_collision_key(value: object) -> str:
    """Negative-only diagnostic key used to reject punctuation/spacing collisions."""

    return re.sub(r"[\W_]+", "", normalize_exact_source_name(value))


_SPECIFICATION_TOKEN_RE = re.compile(
    r"(?:\b(?:dn|pn|ip)\s*\d+[a-zа-я0-9-]*\b"
    r"|\b[a-zа-я]-\d+[a-zа-я0-9-]*\b"
    r"|\b[a-z]{2,4}-\d+(?:-\d+){1,3}[a-z0-9-]*\b"
    r"|\b\d{2,4}-\d{2,4}(?:-\d{2,4})*\b"
    r"|\b\d+(?:[xх×]\d+)+(?:\s*(?:мм|см|м|в|а|квт|кг))?\b)",
    re.IGNORECASE,
)


def _valid_purchase_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed <= datetime.now(timezone.utc).date() else None


def _matcher_article_key(value: object) -> str:
    """Detect article equivalences understood by the existing matcher for ambiguity checks."""

    normalized = normalize_product_search_text(value).translate(_MATCHER_ARTICLE_TRANSLATION)
    return re.sub(r"[^\w]+", "", normalized)


def _bounded_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return 20
    return max(0, min(limit, _MAX_PROVIDER_LIMIT))


def _event_sort_key(event: OneCHistoryEvent) -> tuple[str, int, str]:
    return event.document_date, event.source_row, event.event_id


def _usable_price_event(events: tuple[OneCHistoryEvent, ...]) -> OneCHistoryEvent | None:
    usable = [
        event for event in events
        if event.price_usable
        and event.provenance_valid
        and event.numeric_values_valid
        and event.effective_unit_price_gross is not None
        and event.effective_unit_price_gross.is_finite()
        and event.effective_unit_price_gross >= 0
    ]
    return max(usable, key=_event_sort_key, default=None)


def _intent_queries(intent: ProductIntent) -> tuple[str, ...]:
    values = [intent.normalized_name, intent.model, intent.article, *intent.search_queries]
    normalized = []
    for value in values:
        text = normalize_product_search_text(str(value or "")[:300])
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized[:8])


def _best_variant(
    indexed: _IndexedItem,
    intent: ProductIntent,
    queries: tuple[str, ...],
) -> tuple[OneCHistoryVariant, float]:
    intent_name = normalize_product_search_text(intent.normalized_name)
    intent_article = normalize_product_search_text(intent.article)
    intent_matcher_article = _matcher_article_key(intent.article)
    intent_model = normalize_product_search_text(intent.model)
    best_variant = indexed.indexed_variants[0]
    best_score = -1.0
    for cached in indexed.indexed_variants:
        variant = cached.variant
        name = cached.name_key
        characteristic = cached.characteristic_key
        article = cached.article_key or normalize_product_search_text(indexed.item.article)
        score = 0.0
        if intent_article and article and intent_article == article:
            score = 1000.0
        elif intent_article and article and intent_matcher_article == cached.matcher_article_key:
            # Broader matcher-equivalent identifiers are retrieval signals only.
            score = 990.0
        if intent_name and name and intent_name == name:
            score = max(score, 900.0)
        if intent_model and characteristic and intent_model == characteristic:
            score = max(score, 950.0)
        if intent_model and name and intent_model in name:
            score = max(score, 850.0)
        search_fields = tuple(value for value in (name, characteristic, article) if value)
        for query in queries:
            for field in search_fields:
                score = max(
                    score,
                    float(fuzz.WRatio(query, field)),
                    float(fuzz.token_set_ratio(query, field)) * 0.92,
                )
        if score > best_score or (
            score == best_score
            and (variant.first_source_row, variant.variant_id)
            < (best_variant.variant.first_source_row, best_variant.variant.variant_id)
        ):
            best_variant, best_score = cached, score
    return best_variant.variant, best_score


class OneCHistoryProvider:
    """A non-live provider adapter backed by the active OneC history snapshot."""

    key = "one_c_history"
    label = "История закупок 1С"
    cache_policy = SourcingProviderCachePolicy(cache_search_results=False)
    capabilities = SourcingProviderCapabilities(
        supports_price=True,
        supports_availability=False,
        supports_product_url=False,
        supports_article_search=True,
        supports_model_search=True,
        supports_catalog_version=True,
    )

    def __init__(self, repository: OneCHistoryRepository, *, matcher: OfferMatcher | None = None):
        self.repository = repository
        self.matcher = matcher or OfferMatcher()
        self._cache_lock = threading.RLock()
        self._projection: _Projection | None = None
        self._projection_build_count = 0

    def _load_projection(self) -> _Projection | None:
        with self._cache_lock:
            try:
                active_version = self.repository.catalog_version()
            except OneCHistoryReadError:
                self._projection = None
                raise
            if active_version is None:
                self._projection = None
                return None
            if self._projection is not None and self._projection.version == active_version:
                return self._projection
            snapshot = self.repository.read_catalog_snapshot()
            if snapshot is None:
                self._projection = None
                return None
            projection = self._build_projection(snapshot)
            self._projection = projection
            self._projection_build_count += 1
            return projection

    @staticmethod
    def _build_projection(snapshot: OneCHistoryCatalogSnapshot) -> _Projection:
        indexed_items: list[_IndexedItem] = []
        exact_article_index: dict[str, list[tuple[int, int]]] = defaultdict(list)
        matcher_article_item_ids: dict[str, set[str]] = defaultdict(set)
        exact_name_unit_index: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        loose_name_unit_item_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        structured_identity_index: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(list)
        normalized_name_unit_index: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for item in snapshot.items:
            variants = item.variants or (
                OneCHistoryVariant(
                    variant_id=f"{item.item_id}:canonical",
                    item_name=item.display_name,
                    raw_unit=item.raw_unit,
                    unit_family=item.unit_family,
                    article=item.article,
                    manufacturer=item.manufacturer,
                    characteristic=item.characteristic,
                    first_source_row=0,
                    # A canonical summary is useful for REVIEW retrieval, but
                    # it cannot replace missing descriptive-variant provenance.
                    provenance_valid=False,
                ),
            )
            variants = tuple(sorted(variants, key=lambda variant: (variant.first_source_row, variant.variant_id)))
            indexed_variants: list[_IndexedVariant] = []
            item_index = len(indexed_items)
            for variant_index, variant in enumerate(variants):
                article = variant.article or item.article
                manufacturer = variant.manufacturer or item.manufacturer
                cached = _IndexedVariant(
                    variant=variant,
                    exact_name_key=normalize_exact_source_name(variant.item_name),
                    name_key=normalize_product_search_text(variant.item_name),
                    characteristic_key=normalize_product_search_text(variant.characteristic),
                    article_key=normalize_product_search_text(article),
                    unit_family=normalize_unit_family(variant.raw_unit),
                    matcher_article_key=_matcher_article_key(article),
                    normalized_name_signature=history_name_signature(variant.item_name),
                    normalized_unit_family=_trusted_identity_unit_family(variant.raw_unit),
                )
                indexed_variants.append(cached)
                ref = (item_index, variant_index)
                if cached.article_key:
                    exact_article_index[cached.article_key].append(ref)
                canonical_matcher_article = _matcher_article_key(item.article)
                if canonical_matcher_article:
                    matcher_article_item_ids[canonical_matcher_article].add(item.item_id)
                if cached.matcher_article_key:
                    matcher_article_item_ids[cached.matcher_article_key].add(item.item_id)
                if cached.exact_name_key and cached.unit_family:
                    exact_name_unit_index[(cached.exact_name_key, cached.unit_family)].append(ref)
                    loose_key = _loose_name_collision_key(variant.item_name)
                    if loose_key:
                        loose_name_unit_item_ids[(loose_key, cached.unit_family)].add(item.item_id)
                model_key = normalize_exact_source_name(variant.characteristic)
                manufacturer_key = normalize_exact_source_name(manufacturer)
                if model_key and manufacturer_key and cached.unit_family:
                    structured_identity_index[(manufacturer_key, model_key, cached.unit_family)].append(ref)
                if (
                    cached.normalized_name_signature
                    and cached.normalized_unit_family
                    and item.provenance_valid
                    and not item.integrity_conflicts
                    and variant.provenance_valid
                ):
                    normalized_name_unit_index[(cached.normalized_name_signature, cached.normalized_unit_family)].append(ref)
            indexed_items.append(_IndexedItem(
                item=item,
                searchable_variants=variants,
                indexed_variants=tuple(indexed_variants),
                selected_price_event=_usable_price_event(item.events),
            ))
        item_sort_key = lambda ref: (
            indexed_items[ref[0]].item.item_id,
            indexed_items[ref[0]].indexed_variants[ref[1]].variant.first_source_row,
            indexed_items[ref[0]].indexed_variants[ref[1]].variant.variant_id,
        )
        return _Projection(
            snapshot.version,
            tuple(indexed_items),
            MappingProxyType({key: tuple(sorted(refs, key=item_sort_key)) for key, refs in exact_article_index.items()}),
            MappingProxyType({key: frozenset(item_ids) for key, item_ids in matcher_article_item_ids.items()}),
            MappingProxyType({key: tuple(sorted(refs, key=item_sort_key)) for key, refs in exact_name_unit_index.items()}),
            MappingProxyType({key: frozenset(item_ids) for key, item_ids in loose_name_unit_item_ids.items()}),
            MappingProxyType({key: tuple(sorted(refs, key=item_sort_key)) for key, refs in structured_identity_index.items()}),
            MappingProxyType({key: tuple(sorted(refs, key=item_sort_key)) for key, refs in normalized_name_unit_index.items()}),
        )

    def stats(self) -> SourcingProviderRuntimeState:
        try:
            projection = self._load_projection()
        except OneCHistoryReadError:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=0,
                catalog_version="unavailable",
                error="Активная история закупок недоступна",
            )
        if projection is None:
            return SourcingProviderRuntimeState(
                configured=True,
                reachable=False,
                item_count=0,
                catalog_version="unavailable",
                error="Активная история закупок отсутствует",
            )
        return SourcingProviderRuntimeState(
            configured=True,
            reachable=True,
            item_count=len(projection.items),
            catalog_version=projection.version,
        )

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        bounded = _bounded_limit(limit)
        if bounded == 0:
            return []
        try:
            projection = self._load_projection()
        except OneCHistoryReadError:
            return []
        if projection is None:
            return []
        offers, _, _ = self._retrieve(projection, intent, bounded)
        return offers

    def lookup(
        self,
        intent: ProductIntent,
        *,
        source_intent: ProductIntent | None = None,
        limit: int = 20,
    ) -> HistoryLookupResult:
        """Retrieve with the resolved intent and prove SAFE only from source-owned evidence."""

        source_intent = source_intent or intent
        bounded = _bounded_limit(limit)
        if bounded == 0:
            return HistoryLookupResult(HistoryMatchOutcome.NO_MATCH, True, reason_code="limit_zero")
        try:
            projection = self._load_projection()
        except OneCHistoryReadError:
            return HistoryLookupResult(
                HistoryMatchOutcome.NO_MATCH, False, reason_code="history_unavailable",
            )
        if projection is None:
            return HistoryLookupResult(
                HistoryMatchOutcome.NO_MATCH, False, reason_code="history_unavailable",
            )
        source_article_key = normalize_product_search_text(source_intent.article)
        exact_name_key = normalize_exact_source_name(source_intent.normalized_name)
        source_unit_family = normalize_unit_family(source_intent.unit)
        retrieval_classification = HistoryRetrievalClassification.FUZZY
        indexed_refs: tuple[tuple[int, int], ...] = ()
        if source_article_key:
            indexed_refs = projection.exact_article_index.get(source_article_key, ())
            if indexed_refs:
                retrieval_classification = HistoryRetrievalClassification.EXACT_ARTICLE
        elif exact_name_key and source_unit_family:
            indexed_refs = projection.exact_name_unit_index.get((exact_name_key, source_unit_family), ())
            if indexed_refs:
                retrieval_classification = HistoryRetrievalClassification.EXACT_NAME_UNIT
            else:
                source_identity_family = _trusted_identity_unit_family(source_intent.unit)
                source_signature = history_name_signature(source_intent.normalized_name)
                if source_signature and source_identity_family:
                    indexed_refs = projection.normalized_name_unit_index.get(
                        (source_signature, source_identity_family),
                    )
                    if indexed_refs:
                        retrieval_classification = HistoryRetrievalClassification.NORMALIZED_NAME_UNIT
                if not indexed_refs:
                    manufacturer_key = normalize_exact_source_name(source_intent.manufacturer)
                    model_key = normalize_exact_source_name(source_intent.model)
                    if manufacturer_key and model_key:
                        indexed_refs = projection.structured_identity_index.get(
                            (manufacturer_key, model_key, source_unit_family),
                        )
                        if indexed_refs:
                            retrieval_classification = HistoryRetrievalClassification.STRUCTURED

        if indexed_refs:
            retrieved, top_score_tie_count = self._retrieve_indexed(
                projection,
                indexed_refs,
                retrieval_classification,
                bounded,
            )
            offers = [self._to_offer(projection, candidate) for candidate in retrieved]
        else:
            offers, retrieved, top_score_tie_count = self._retrieve(projection, intent, bounded)
        if not offers:
            return HistoryLookupResult(
                HistoryMatchOutcome.NO_MATCH, True, reason_code="no_candidates",
                catalog_version=projection.version,
            )
        matches = self.matcher.match(intent, offers)
        source_matches = self.matcher.match(source_intent, offers)
        match_by_offer = {match.offer.offer_id: match for match in matches}
        source_match_by_offer = {match.offer.offer_id: match for match in source_matches}

        if retrieval_classification == HistoryRetrievalClassification.NORMALIZED_NAME_UNIT:
            # This class grants review evidence only.  Its label and ordering
            # depend on source-owned fields, never on an AI-resolved intent.
            matches = source_matches
            return HistoryLookupResult(
                HistoryMatchOutcome.REVIEW,
                True,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code=(
                    "ambiguous_normalized_name_identity"
                    if top_score_tie_count > 1
                    else "normalized_name_unit_requires_review"
                ),
                catalog_version=projection.version,
            )

        # A source-owned article is authoritative: if present, no name-only path
        # may rescue an article mismatch or unresolved strict article match.
        if source_article_key:
            if retrieval_classification != HistoryRetrievalClassification.EXACT_ARTICLE:
                return HistoryLookupResult(
                    HistoryMatchOutcome.REVIEW,
                    True,
                    candidates=tuple(offers),
                    match_results=tuple(matches),
                    reason_code="source_article_not_found",
                    catalog_version=projection.version,
                )
            deterministic_matches = [match for match in matches if match.decision == MatchDecision.MATCH]
            if len(deterministic_matches) != 1:
                return HistoryLookupResult(
                    HistoryMatchOutcome.REVIEW,
                    True,
                    candidates=tuple(offers),
                    match_results=tuple(matches),
                    reason_code="ambiguous_or_non_strict_evidence",
                    catalog_version=projection.version,
                )
            selected = deterministic_matches[0]
            selected_retrieval = next(
                (candidate for candidate in retrieved if candidate.indexed.item.item_id == selected.offer.source_item_id),
                None,
            )
            source_match = source_match_by_offer.get(selected.offer.offer_id)
            if (
                source_match is None
                or not self._safe_match(
                    source_intent,
                    source_match,
                    selected_retrieval,
                    top_score_tie_count=top_score_tie_count,
                    projection=projection,
                )
                or not self._safe_match(
                    intent,
                    selected,
                    selected_retrieval,
                    top_score_tie_count=top_score_tie_count,
                    projection=projection,
                )
            ):
                return HistoryLookupResult(
                    HistoryMatchOutcome.REVIEW,
                    True,
                    candidates=tuple(offers),
                    match_results=tuple(matches),
                    reason_code="history_requires_review",
                    catalog_version=projection.version,
                )
            return HistoryLookupResult(
                HistoryMatchOutcome.SAFE_MATCH,
                True,
                selected_offer=selected.offer,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code="unique_deterministic_historical_evidence",
                safe_basis=HistorySafeMatchBasis.EXACT_ARTICLE,
                catalog_version=projection.version,
            )

        if retrieval_classification != HistoryRetrievalClassification.EXACT_NAME_UNIT:
            reason = (
                "structured_identity_requires_review"
                if retrieval_classification == HistoryRetrievalClassification.STRUCTURED
                else "fuzzy_candidates_require_review"
            )
            return HistoryLookupResult(
                HistoryMatchOutcome.REVIEW,
                True,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code=reason,
                catalog_version=projection.version,
            )

        strict_refs = projection.exact_name_unit_index.get((exact_name_key, source_unit_family), ())
        strict_item_ids = {
            projection.items[item_index].item.item_id
            for item_index, _variant_index in strict_refs
        }
        loose_name_key = _loose_name_collision_key(source_intent.normalized_name)
        loose_ids = projection.loose_name_unit_item_ids.get((loose_name_key, source_unit_family), frozenset())
        loose_collision = bool(loose_ids - strict_item_ids) if loose_name_key and source_unit_family else False

        if len(strict_item_ids) != 1 or loose_collision:
            return HistoryLookupResult(
                HistoryMatchOutcome.REVIEW,
                True,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code=(
                    "ambiguous_exact_name_identity"
                    if len(strict_item_ids) != 1
                    else "loose_name_collision_requires_review"
                ),
                catalog_version=projection.version,
            )
        selected_retrieval = next(
            candidate for candidate in retrieved
            if candidate.indexed.item.item_id in strict_item_ids
        )
        selected = match_by_offer.get(f"one_c_history:{selected_retrieval.indexed.item.item_id}")
        source_match = source_match_by_offer.get(f"one_c_history:{selected_retrieval.indexed.item.item_id}")
        if (
            selected is None
            or source_match is None
            or selected.decision not in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}
            or source_match.decision not in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}
            or not self._safe_name_match(
                source_intent,
                intent,
                selected,
                source_match,
                selected_retrieval,
                top_score_tie_count=top_score_tie_count,
                projection=projection,
                strict_item_ids=strict_item_ids,
                loose_collision=loose_collision,
            )
        ):
            return HistoryLookupResult(
                HistoryMatchOutcome.REVIEW,
                True,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code="history_requires_review",
                catalog_version=projection.version,
            )
        return HistoryLookupResult(
            HistoryMatchOutcome.SAFE_MATCH,
            True,
            selected_offer=selected.offer,
            candidates=tuple(offers),
            match_results=tuple(matches),
            reason_code="unique_exact_source_name_unit_evidence",
            safe_basis=HistorySafeMatchBasis.EXACT_SOURCE_NAME_UNIT,
            catalog_version=projection.version,
        )

    @staticmethod
    def _retrieve_indexed(
        projection: _Projection,
        refs: tuple[tuple[int, int], ...],
        classification: HistoryRetrievalClassification,
        limit: int,
    ) -> tuple[list[_RetrievedItem], int]:
        by_item: dict[str, _RetrievedItem] = {}
        for item_index, variant_index in refs:
            indexed = projection.items[item_index]
            variant = indexed.indexed_variants[variant_index].variant
            by_item.setdefault(
                indexed.item.item_id,
                _RetrievedItem(indexed, variant, 1000.0, classification),
            )
        candidates = sorted(
            by_item.values(),
            key=lambda candidate: (
                candidate.indexed.item.item_id,
                candidate.variant.first_source_row,
                candidate.variant.variant_id,
            ),
        )
        return candidates[:limit], len(candidates)

    def _retrieve(
        self,
        projection: _Projection,
        intent: ProductIntent,
        limit: int,
    ) -> tuple[list[Offer], list[_RetrievedItem], int]:
        queries = _intent_queries(intent)
        if not queries or limit <= 0:
            return [], [], 0
        retrieved: list[_RetrievedItem] = []
        top_score = -1.0
        top_score_tie_count = 0
        for indexed in projection.items:
            variant, score = _best_variant(indexed, intent, queries)
            if score < _MIN_FUZZY_SCORE:
                continue
            if score > top_score:
                top_score = score
                top_score_tie_count = 1
            elif score == top_score:
                top_score_tie_count += 1
            candidate = _RetrievedItem(indexed, variant, score)
            candidate_key = (
                -candidate.score,
                candidate.indexed.item.item_id,
                candidate.variant.first_source_row,
                candidate.variant.variant_id,
            )
            if len(retrieved) < limit:
                retrieved.append(candidate)
                retrieved.sort(key=lambda item: (
                    -item.score,
                    item.indexed.item.item_id,
                    item.variant.first_source_row,
                    item.variant.variant_id,
                ))
            elif candidate_key < (
                -retrieved[-1].score,
                retrieved[-1].indexed.item.item_id,
                retrieved[-1].variant.first_source_row,
                retrieved[-1].variant.variant_id,
            ):
                retrieved[-1] = candidate
                retrieved.sort(key=lambda item: (
                    -item.score,
                    item.indexed.item.item_id,
                    item.variant.first_source_row,
                    item.variant.variant_id,
                ))
        return [self._to_offer(projection, candidate) for candidate in retrieved], retrieved, top_score_tie_count

    @staticmethod
    def _to_offer(projection: _Projection, candidate: _RetrievedItem) -> Offer:
        item = candidate.indexed.item
        variant = candidate.variant
        event = candidate.indexed.selected_price_event
        article = variant.article or item.article
        manufacturer = variant.manufacturer or item.manufacturer
        characteristic = variant.characteristic or item.characteristic
        raw_unit = event.raw_unit if event is not None else (variant.raw_unit or item.raw_unit)
        unit_family = normalize_unit_family(raw_unit)
        selected_price = event.effective_unit_price_gross if event is not None else None
        source_currency = event.currency if event is not None else None
        currency, currency_basis = _effective_history_currency(source_currency)
        provenance = {
            "source": "one_c_history",
            "source_kind": "historical_purchase",
            "snapshot_version": projection.version,
            "history_item_id": item.item_id,
            "source_item_code": item.source_item_code or None,
            "identity_quality": item.identity_quality,
            "group_number": item.group_number,
            "selected_event_id": event.event_id if event is not None else None,
            "purchase_date": event.document_date if event is not None else None,
            "document_type": event.document_type if event is not None else "",
            "counterparty": event.counterparty if event is not None else "",
            "purchased_quantity": event.quantity if event is not None else None,
            "price_basis": event.price_basis if event is not None else "",
            "reported_unit_price_gross": event.reported_unit_price_gross if event is not None else None,
            "effective_unit_price_gross": selected_price,
            "currency_basis": currency_basis,
            "unit_family": unit_family,
        }
        if candidate.classification == HistoryRetrievalClassification.NORMALIZED_NAME_UNIT:
            provenance["normalizer_revision"] = HISTORY_IDENTITY_NORMALIZER_REVISION
            provenance["normalized_name_signature"] = history_name_signature_digest(variant.item_name)
        attributes = {}
        if characteristic:
            attributes["characteristic"] = characteristic
        if unit_family:
            attributes["unit_family"] = unit_family
        return Offer(
            offer_id=f"one_c_history:{item.item_id}",
            provider="one_c_history",
            source_item_id=item.item_id,
            title=variant.item_name,
            article=article,
            manufacturer=manufacturer,
            brand="",
            price=selected_price,
            currency=currency,
            price_unit=raw_unit,
            availability=None,
            availability_text="Текущая доступность неизвестна; это историческая закупка.",
            url="",
            attributes=attributes,
            retrieved_at=datetime.now(timezone.utc),
            data_provenance=provenance,
            history_retrieval_classification=candidate.classification,
        )

    @staticmethod
    def _safe_match(
        intent: ProductIntent,
        match: MatchResult,
        retrieved: _RetrievedItem | None,
        *,
        top_score_tie_count: int,
        projection: _Projection,
    ) -> bool:
        if retrieved is None or match.decision != MatchDecision.MATCH:
            return False
        if match.conflicting_attributes or match.missing_attributes:
            return False
        offer = match.offer
        provenance = offer.data_provenance
        item = retrieved.indexed.item
        event = retrieved.indexed.selected_price_event
        if item.integrity_conflicts:
            return False
        expected_provenance = {
            "source": "one_c_history",
            "source_kind": "historical_purchase",
            "snapshot_version": projection.version,
            "history_item_id": item.item_id,
        }
        if any(provenance.get(key) != value for key, value in expected_provenance.items()):
            return False
        if (
            event is None
            or not item.provenance_valid
            or not retrieved.variant.provenance_valid
            or not event.provenance_valid
        ):
            return False
        if provenance.get("selected_event_id") != event.event_id or event.item_id != item.item_id:
            return False
        if provenance.get("effective_unit_price_gross") != event.effective_unit_price_gross:
            return False
        if not event.price_usable or not event.numeric_values_valid or offer.price is None or offer.price <= 0:
            return False
        if _valid_purchase_date(event.document_date) is None:
            return False
        intent_family = normalize_unit_family(intent.unit)
        variant_family = normalize_unit_family(retrieved.variant.raw_unit)
        history_family = normalize_unit_family(event.raw_unit)
        if (
            intent_family is None
            or variant_family is None
            or history_family is None
            or len({intent_family, variant_family, history_family}) != 1
        ):
            return False
        intent_article = normalize_product_search_text(intent.article)
        offer_article = normalize_product_search_text(offer.article)
        if not intent_article or intent_article != offer_article:
            return False
        # The existing matcher intentionally normalizes punctuation in articles.
        # Treat its broader equivalences as ambiguity, never as a source merge.
        matcher_key = _matcher_article_key(intent.article)
        matching_items = len(projection.matcher_article_item_ids.get(matcher_key, ())) if matcher_key else 0
        if matching_items != 1 or top_score_tie_count != 1:
            return False
        if not ("article" in match.matched_attributes):
            return False
        return True

    @staticmethod
    def _safe_name_match(
        source_intent: ProductIntent,
        resolved_intent: ProductIntent,
        resolved_match: MatchResult,
        source_match: MatchResult,
        retrieved: _RetrievedItem,
        *,
        top_score_tie_count: int,
        projection: _Projection,
        strict_item_ids: set[str],
        loose_collision: bool,
    ) -> bool:
        if top_score_tie_count != 1 or loose_collision or len(strict_item_ids) != 1:
            return False
        if resolved_match.conflicting_attributes or resolved_match.missing_attributes:
            return False
        if source_match.conflicting_attributes or source_match.missing_attributes:
            return False
        item = retrieved.indexed.item
        variant = retrieved.variant
        event = retrieved.indexed.selected_price_event
        if (
            item.integrity_conflicts
            or not item.provenance_valid
            or not variant.provenance_valid
            or event is None
            or not event.provenance_valid
            or not event.numeric_values_valid
            or not event.price_usable
            or event.effective_unit_price_gross is None
            or not event.effective_unit_price_gross.is_finite()
            or event.effective_unit_price_gross <= 0
            or resolved_match.offer.price is None
            or resolved_match.offer.price != event.effective_unit_price_gross
            or resolved_match.offer.provider != "one_c_history"
            or resolved_match.offer.source_item_id != item.item_id
            or normalize_exact_source_name(resolved_match.offer.title)
            != normalize_exact_source_name(source_intent.normalized_name)
            or event.item_id != item.item_id
            or _valid_purchase_date(event.document_date) is None
        ):
            return False
        expected_provenance = {
            "source": "one_c_history",
            "source_kind": "historical_purchase",
            "snapshot_version": projection.version,
            "history_item_id": item.item_id,
            "selected_event_id": event.event_id,
            "effective_unit_price_gross": event.effective_unit_price_gross,
            "unit_family": normalize_unit_family(event.raw_unit),
        }
        if any(resolved_match.offer.data_provenance.get(key) != value for key, value in expected_provenance.items()):
            return False
        source_family = normalize_unit_family(source_intent.unit)
        variant_family = normalize_unit_family(variant.raw_unit)
        event_family = normalize_unit_family(event.raw_unit)
        if (
            source_family is None
            or variant_family is None
            or event_family is None
            or len({source_family, variant_family, event_family}) != 1
        ):
            return False

        # No source-owned required technical fact may be unresolved or AI-only.
        origins = source_intent.evidence.get("attribute_origins", {})
        if not isinstance(origins, dict):
            origins = {}
        if any(str(origins.get(key) or "").casefold() == "ai_inferred" for key in source_intent.required_attributes):
            return False

        if not OneCHistoryProvider._has_source_specificity(
            source_intent,
            source_match,
            variant,
        ):
            return False
        return True

    @staticmethod
    def _has_source_specificity(
        source_intent: ProductIntent,
        source_match: MatchResult,
        variant: OneCHistoryVariant,
    ) -> bool:
        source_model = normalize_exact_source_name(source_intent.model)
        candidate_models = {
            normalize_exact_source_name(value)
            for value in (variant.characteristic, variant.item_name, variant.article)
            if normalize_exact_source_name(value)
        }
        if source_model and source_model in candidate_models:
            return True
        source_manufacturer = normalize_exact_source_name(source_intent.manufacturer)
        if source_manufacturer and source_manufacturer == normalize_exact_source_name(variant.manufacturer):
            return True
        origins = source_intent.evidence.get("attribute_origins", {})
        if not isinstance(origins, dict):
            origins = {}
        source_required = {
            key for key in source_intent.required_attributes
            if str(origins.get(key) or "").casefold() != "ai_inferred"
        }
        if source_required and source_required.issubset(set(source_match.matched_attributes)):
            return True
        return bool(_SPECIFICATION_TOKEN_RE.search(normalize_exact_source_name(source_intent.normalized_name)))


__all__ = [
    "HistoryLookupResult",
    "HistoryMatchOutcome",
    "OneCHistoryProvider",
    "normalize_product_search_text",
    "normalize_exact_source_name",
]


def _trusted_identity_unit_family(raw_unit: object) -> str | None:
    # Import locally to keep the general 1C read model independent from the
    # tender parser while applying the same strict conversion contract.
    from averon_import.services.manual_tenders.parser import parse_unit_basis

    basis = parse_unit_basis(raw_unit)
    if basis.get("trusted") is not True or basis.get("dimension") in {"package", "set"}:
        return None
    return normalize_unit_family(raw_unit)
