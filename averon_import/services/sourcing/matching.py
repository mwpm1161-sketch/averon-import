from __future__ import annotations

from averon_import.services.sourcing.models import MatchDecision, MatchResult, Offer, ProductIntent
from averon_import.services.sourcing.validation import DeterministicValidator, explanation_for


_DECISION_ORDER = {
    MatchDecision.MATCH: 0,
    MatchDecision.LIKELY_MATCH: 1,
    MatchDecision.ALTERNATIVE: 2,
    MatchDecision.REVIEW: 3,
    MatchDecision.REJECT: 4,
}


class OfferMatcher:
    def __init__(self, validator: DeterministicValidator | None = None):
        self.validator = validator or DeterministicValidator()

    def match(self, intent: ProductIntent, offers: list[Offer]) -> list[MatchResult]:
        provisional: list[tuple[int, MatchResult]] = []
        for offer in offers:
            evidence = self.validator.validate(intent, offer)
            if evidence.conflicts:
                decision = MatchDecision.REJECT
            elif evidence.missing:
                decision = MatchDecision.REVIEW
            elif evidence.preferred_differences:
                decision = MatchDecision.ALTERNATIVE
            elif evidence.matched:
                decision = MatchDecision.MATCH
            else:
                decision = MatchDecision.LIKELY_MATCH
            result = MatchResult(
                offer=offer,
                decision=decision,
                rank=1,
                matched_attributes=list(evidence.matched),
                conflicting_attributes=list(evidence.conflicts),
                missing_attributes=list(evidence.missing),
                explanation=explanation_for(evidence),
                ai_evidence={},
                deterministic_evidence={
                    "hard_contradiction": bool(evidence.conflicts),
                    "preferred_differences": list(evidence.preferred_differences),
                },
            )
            provisional.append((
                (
                    _DECISION_ORDER[decision],
                    len(evidence.conflicts),
                    len(evidence.missing),
                    len(evidence.preferred_differences),
                    -len(evidence.matched),
                    result.offer.offer_id,
                ),
                result,
            ))
        provisional.sort(key=lambda item: item[0])
        return [item[1].model_copy(update={"rank": index}) for index, item in enumerate(provisional, 1)]


def recommended_offer(results: list[MatchResult]) -> Offer | None:
    for decision in (
        MatchDecision.MATCH,
        MatchDecision.LIKELY_MATCH,
        MatchDecision.ALTERNATIVE,
    ):
        for result in results:
            if result.decision == decision:
                return result.offer
    return None


_IDENTITY_FIELDS = frozenset({"article", "model", "manufacturer", "brand"})


def review_candidate(results: list[MatchResult]) -> MatchResult | None:
    """Select a strong identity REVIEW candidate without changing decisions."""

    candidates: list[tuple[tuple[int, int, int, int, int], MatchResult]] = []
    for result in results:
        if result.decision != MatchDecision.REVIEW or result.conflicting_attributes:
            continue
        identity = _IDENTITY_FIELDS.intersection(result.matched_attributes)
        has_exact_primary_identity = bool(identity.intersection({"article", "model"}))
        if not has_exact_primary_identity and len(identity) < 2:
            continue
        score = (
            int("article" in identity),
            int("model" in identity),
            len(identity),
            -len(result.missing_attributes),
            -result.rank,
        )
        candidates.append((score, result))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]
