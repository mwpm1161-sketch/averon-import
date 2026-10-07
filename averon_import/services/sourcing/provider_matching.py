from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import MatchDecision, MatchResult, Offer, ProductIntent
from averon_import.services.sourcing.providers.contracts import (
    ProviderOfferReference,
    ProviderSearchOutcome,
    ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import (
    MAX_PROVIDER_EXECUTION_OFFERS,
    ProviderExecutionResult,
)


class ProviderMatchEvaluationError(ValueError):
    """Raised when execution offers and deterministic matches do not correlate exactly."""


_IDENTITY_FIELDS = frozenset({"article", "model", "manufacturer", "brand"})
_RECOMMENDABLE_DECISIONS = frozenset({
    MatchDecision.MATCH,
    MatchDecision.LIKELY_MATCH,
    MatchDecision.ALTERNATIVE,
})
_RECOMMENDATION_PRECEDENCE = (
    MatchDecision.MATCH,
    MatchDecision.LIKELY_MATCH,
    MatchDecision.ALTERNATIVE,
)


def _reference_for(offer: Offer) -> ProviderOfferReference:
    try:
        return ProviderOfferReference.from_offer(offer)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProviderMatchEvaluationError("offer has a malformed provider identity") from exc


def _execution_offer_index(
    execution: ProviderExecutionResult,
) -> dict[ProviderOfferReference, Offer]:
    if not isinstance(execution, ProviderExecutionResult):
        raise ProviderMatchEvaluationError("execution must be a ProviderExecutionResult")
    if len(execution.offers) > MAX_PROVIDER_EXECUTION_OFFERS:
        raise ProviderMatchEvaluationError("execution exceeds the bounded offer count")
    try:
        # Re-validate even objects built with Pydantic's model_construct/model_copy
        # escape hatches; this boundary must fail closed on malformed execution.
        ProviderExecutionResult.model_validate(execution.model_dump(mode="python"))
    except (TypeError, ValueError, ValidationError) as exc:
        raise ProviderMatchEvaluationError("execution result failed structural validation") from exc

    indexed: dict[ProviderOfferReference, Offer] = {}
    for offer in execution.offers:
        reference = _reference_for(offer)
        if reference in indexed:
            raise ProviderMatchEvaluationError("execution contains a duplicate composite offer identity")
        indexed[reference] = offer

    outcome_index: dict[ProviderOfferReference, Offer] = {}
    outcome_offer_count = 0
    for outcome in execution.outcomes:
        for offer in outcome.offers:
            outcome_offer_count += 1
            if outcome_offer_count > MAX_PROVIDER_EXECUTION_OFFERS:
                raise ProviderMatchEvaluationError("execution exceeds the bounded offer count")
            reference = _reference_for(offer)
            if reference in outcome_index:
                raise ProviderMatchEvaluationError("execution outcomes contain a duplicate composite offer identity")
            outcome_index[reference] = offer
    if outcome_index != indexed:
        raise ProviderMatchEvaluationError("execution outcome offers do not match the merged offer projection")
    return indexed


def _review_evidence_score(result: MatchResult) -> tuple[int, int, int, int] | None:
    if result.decision != MatchDecision.REVIEW or result.conflicting_attributes:
        return None
    identity = _IDENTITY_FIELDS.intersection(result.matched_attributes)
    if not identity.intersection({"article", "model"}) and len(identity) < 2:
        return None
    # Provider identity, offer identity, rank, price, stock, currency and
    # retrieval timing are deliberately absent from semantic review strength.
    return (
        int("article" in identity),
        int("model" in identity),
        len(identity),
        -len(result.missing_attributes),
    )


def _unique_review_candidate(matches: tuple[MatchResult, ...]) -> MatchResult | None:
    candidates = [
        (score, result)
        for result in matches
        if (score := _review_evidence_score(result)) is not None
    ]
    if not candidates:
        return None
    strongest = max(score for score, _result in candidates)
    winners = [result for score, result in candidates if score == strongest]
    return winners[0] if len(winners) == 1 else None


def _unique_reference(
    match: MatchResult | None,
) -> ProviderOfferReference | None:
    return _reference_for(match.offer) if match is not None else None


def _recommendation_quality(result: MatchResult) -> tuple[int, int, int, int, int]:
    """Existing matcher evidence dimensions, excluding composite identity ordering."""

    preferred_differences = result.deterministic_evidence.get("preferred_differences", [])
    if not isinstance(preferred_differences, (list, tuple, set)):
        raise ProviderMatchEvaluationError("preferred evidence must be a finite sequence")
    return (
        len(result.conflicting_attributes),
        len(result.missing_attributes),
        len(preferred_differences),
        -len(result.matched_attributes),
        -len(result.supporting_attributes),
    )


def _unique_identity_recommendation(matches: tuple[MatchResult, ...]) -> MatchResult | None:
    for decision in _RECOMMENDATION_PRECEDENCE:
        candidates = [item for item in matches if item.decision == decision]
        if not candidates:
            continue
        strongest_quality = min(_recommendation_quality(item) for item in candidates)
        strongest = [
            item for item in candidates
            if _recommendation_quality(item) == strongest_quality
        ]
        return strongest[0] if len(strongest) == 1 else None
    return None


def _copy_match_snapshot(value: MatchResult) -> MatchResult:
    return value.model_copy(deep=True)


@dataclass(frozen=True, init=False)
class ProviderMatchEvaluation:
    """Request-local deterministic matches correlated to one exact execution."""

    intent: ProductIntent
    execution: ProviderExecutionResult
    _matches: tuple[MatchResult, ...] = field(repr=False)
    recommended_offer_reference: ProviderOfferReference | None
    review_candidate_reference: ProviderOfferReference | None

    def __init__(
        self,
        intent: ProductIntent,
        execution: ProviderExecutionResult,
        matches: tuple[MatchResult, ...],
        recommended_offer_reference: ProviderOfferReference | None,
        review_candidate_reference: ProviderOfferReference | None,
    ) -> None:
        if not isinstance(matches, tuple):
            raise ProviderMatchEvaluationError("matches must be an immutable tuple")
        try:
            # Own the mutable legacy MatchResult hierarchy before validation.
            snapshots = tuple(
                _copy_match_snapshot(item) if isinstance(item, MatchResult) else item
                for item in matches
            )
        except Exception as exc:
            raise ProviderMatchEvaluationError("matches could not be snapshotted") from exc
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "execution", execution)
        object.__setattr__(self, "_matches", snapshots)
        object.__setattr__(self, "recommended_offer_reference", recommended_offer_reference)
        object.__setattr__(self, "review_candidate_reference", review_candidate_reference)
        self.__post_init__()

    @property
    def matches(self) -> tuple[MatchResult, ...]:
        """Return detached copies so callers cannot mutate the validated snapshot."""

        return tuple(_copy_match_snapshot(item) for item in self._matches)

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ProductIntent):
            raise ProviderMatchEvaluationError("intent must be a ProductIntent")
        if not isinstance(self._matches, tuple):
            raise ProviderMatchEvaluationError("matches must be an immutable tuple")
        offer_index = _execution_offer_index(self.execution)
        if len(self._matches) != len(offer_index):
            raise ProviderMatchEvaluationError("every execution offer must have exactly one match")

        matched: dict[ProviderOfferReference, MatchResult] = {}
        for result in self._matches:
            if not isinstance(result, MatchResult):
                raise ProviderMatchEvaluationError("matches must contain MatchResult values")
            if result.ai_evidence:
                raise ProviderMatchEvaluationError("provider match evaluation must remain deterministic")
            reference = _reference_for(result.offer)
            expected_offer = offer_index.get(reference)
            if expected_offer is None or result.offer != expected_offer:
                raise ProviderMatchEvaluationError("match offer is absent from the exact execution result")
            if reference in matched:
                raise ProviderMatchEvaluationError("duplicate match for one composite offer identity")
            matched[reference] = result
        if set(matched) != set(offer_index):
            raise ProviderMatchEvaluationError("match/execution composite identities do not correlate exactly")

        expected_recommendation = _unique_reference(_unique_identity_recommendation(self._matches))
        if self.recommended_offer_reference != expected_recommendation:
            raise ProviderMatchEvaluationError("recommended reference does not match deterministic evidence")
        review = _unique_review_candidate(self._matches)
        expected_review = _unique_reference(review)
        if self.review_candidate_reference != expected_review:
            raise ProviderMatchEvaluationError("review reference does not match unique review evidence")

        for reference in (self.recommended_offer_reference, self.review_candidate_reference):
            if reference is not None and sum(item == reference for item in matched) != 1:
                raise ProviderMatchEvaluationError("candidate reference must identify exactly one match")
        if (
            self.recommended_offer_reference is not None
            and matched[self.recommended_offer_reference].decision not in _RECOMMENDABLE_DECISIONS
        ):
            raise ProviderMatchEvaluationError("recommended reference is not an identity candidate")

    @property
    def selection(self) -> ProviderSelection:
        """The exact canonical selection object from the retained execution."""

        return self.execution.selection

    @property
    def outcomes(self) -> tuple[ProviderSearchOutcome, ...]:
        """Provider outcomes remain visible, including empty and failed providers."""

        return self.execution.outcomes

    @property
    def partial_failure(self) -> bool:
        return self.execution.partial_failure

    @property
    def recommended_match(self) -> MatchResult | None:
        reference = self.recommended_offer_reference
        match = next(
            (item for item in self._matches if reference is not None and _reference_for(item.offer) == reference),
            None,
        )
        return _copy_match_snapshot(match) if match is not None else None

    @property
    def review_candidate(self) -> MatchResult | None:
        reference = self.review_candidate_reference
        match = next(
            (item for item in self._matches if reference is not None and _reference_for(item.offer) == reference),
            None,
        )
        return _copy_match_snapshot(match) if match is not None else None


class ProviderMatchEvaluator:
    """Evaluate one bounded execution without retrieval, AI, cache, or persistence."""

    def __init__(self, matcher: OfferMatcher | None = None) -> None:
        self.matcher = matcher or OfferMatcher()

    def evaluate(
        self,
        intent: ProductIntent,
        execution: ProviderExecutionResult,
    ) -> ProviderMatchEvaluation:
        if not isinstance(intent, ProductIntent):
            raise ProviderMatchEvaluationError("intent must be a ProductIntent")
        offer_index = _execution_offer_index(execution)
        try:
            raw_matches = self.matcher.match(intent, list(execution.offers))
        except Exception as exc:
            raise ProviderMatchEvaluationError("deterministic offer matching failed") from exc
        if not isinstance(raw_matches, (list, tuple)):
            raise ProviderMatchEvaluationError("matcher must return a finite sequence of MatchResult values")
        if any(not isinstance(item, MatchResult) for item in raw_matches):
            raise ProviderMatchEvaluationError("matcher must return a finite sequence of MatchResult values")
        try:
            # Snapshot the matcher-owned objects before computing any references.
            matches = tuple(_copy_match_snapshot(item) for item in raw_matches)
        except Exception as exc:
            raise ProviderMatchEvaluationError("matcher results could not be snapshotted") from exc
        if len(matches) != len(offer_index):
            raise ProviderMatchEvaluationError("matcher must return exactly one result per execution offer")
        recommendation = _unique_reference(_unique_identity_recommendation(matches))
        review = _unique_reference(_unique_review_candidate(matches))
        return ProviderMatchEvaluation(
            intent=intent,
            execution=execution,
            matches=matches,
            recommended_offer_reference=recommendation,
            review_candidate_reference=review,
        )
