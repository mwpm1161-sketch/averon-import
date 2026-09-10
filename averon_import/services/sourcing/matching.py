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
            provisional.append((_DECISION_ORDER[decision], result))
        provisional.sort(key=lambda item: (item[0], item[1].offer.offer_id))
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
