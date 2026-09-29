"""Read-only retrieval and conservative matching for historical 1C purchases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
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
    MatchDecision,
    MatchResult,
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)
from averon_import.services.sourcing.providers.base import SourcingProviderCachePolicy


_MAX_PROVIDER_LIMIT = 50
_MIN_FUZZY_SCORE = 58
_MATCHER_ARTICLE_TRANSLATION = str.maketrans({
    "а": "a", "в": "v", "е": "e", "з": "z", "и": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "х": "x", "э": "e",
})


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
class _IndexedItem:
    item: OneCHistoryItem
    searchable_variants: tuple[OneCHistoryVariant, ...]
    selected_price_event: OneCHistoryEvent | None


@dataclass(frozen=True, slots=True)
class _Projection:
    version: str
    items: tuple[_IndexedItem, ...]


@dataclass(frozen=True, slots=True)
class _RetrievedItem:
    indexed: _IndexedItem
    variant: OneCHistoryVariant
    score: float


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
    intent_model = normalize_product_search_text(intent.model)
    best_variant = indexed.searchable_variants[0]
    best_score = -1.0
    for variant in indexed.searchable_variants:
        name = normalize_product_search_text(variant.item_name)
        characteristic = normalize_product_search_text(variant.characteristic)
        article = normalize_product_search_text(variant.article or indexed.item.article)
        score = 0.0
        if intent_article and article and intent_article == article:
            score = 1000.0
        elif intent_article and article and _matcher_article_key(intent.article) == _matcher_article_key(article):
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
            < (best_variant.first_source_row, best_variant.variant_id)
        ):
            best_variant, best_score = variant, score
    return best_variant, best_score


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
            indexed_items.append(_IndexedItem(
                item=item,
                searchable_variants=variants,
                selected_price_event=_usable_price_event(item.events),
            ))
        return _Projection(snapshot.version, tuple(indexed_items))

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

        # A source-owned article is authoritative: if present, no name-only path
        # may rescue an article mismatch or unresolved strict article match.
        if normalize_product_search_text(source_intent.article):
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

        exact_name_key = normalize_exact_source_name(source_intent.normalized_name)
        source_unit_family = normalize_unit_family(source_intent.unit)
        name_candidates = [
            candidate for candidate in retrieved
            if exact_name_key
            and normalize_exact_source_name(candidate.variant.item_name) == exact_name_key
            and source_unit_family is not None
            and normalize_unit_family(candidate.variant.raw_unit) == source_unit_family
            and candidate.indexed.selected_price_event is not None
            and normalize_unit_family(candidate.indexed.selected_price_event.raw_unit) == source_unit_family
        ]
        strict_item_ids = {
            item.item.item_id
            for item in projection.items
            if any(
                normalize_exact_source_name(variant.item_name) == exact_name_key
                and normalize_unit_family(variant.raw_unit) == source_unit_family
                for variant in item.searchable_variants
            )
        } if exact_name_key and source_unit_family else set()
        loose_name_key = _loose_name_collision_key(source_intent.normalized_name)
        loose_collision = any(
            item.item.item_id not in strict_item_ids
            and any(
                _loose_name_collision_key(variant.item_name) == loose_name_key
                and normalize_unit_family(variant.raw_unit) == source_unit_family
                for variant in item.searchable_variants
            )
            for item in projection.items
        ) if loose_name_key and source_unit_family else False

        if len(name_candidates) != 1 or len(strict_item_ids) != 1 or loose_collision:
            return HistoryLookupResult(
                HistoryMatchOutcome.REVIEW,
                True,
                candidates=tuple(offers),
                match_results=tuple(matches),
                reason_code=(
                    "ambiguous_exact_name_identity"
                    if len(name_candidates) > 1 or len(strict_item_ids) != 1 or loose_collision
                    else "exact_name_not_retrieved"
                ),
                catalog_version=projection.version,
            )
        selected_retrieval = name_candidates[0]
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
            "unit_family": unit_family,
        }
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
            currency=event.currency if event is not None else "",
            price_unit=raw_unit,
            availability=None,
            availability_text="Текущая доступность неизвестна; это историческая закупка.",
            url="",
            attributes=attributes,
            retrieved_at=datetime.now(timezone.utc),
            data_provenance=provenance,
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
        matching_items = 0
        if matcher_key:
            for projected in projection.items:
                item_articles = {projected.item.article}
                item_articles.update(variant.article for variant in projected.searchable_variants)
                if any(_matcher_article_key(article) == matcher_key for article in item_articles if article):
                    matching_items += 1
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
