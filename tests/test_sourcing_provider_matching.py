from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import (
    MatchDecision,
    MatchResult,
    Offer,
    ProductIntent,
)
from averon_import.services.sourcing.providers.contracts import (
    ProviderFailureCategory,
    ProviderOfferReference,
    ProviderSearchOutcome,
    ProviderSearchState,
    ProviderSelection,
    index_offers_by_provider_identity,
)
from averon_import.services.sourcing.providers.execution import ProviderExecutionResult
from averon_import.services.sourcing.provider_matching import (
    ProviderMatchEvaluation,
    ProviderMatchEvaluationError,
)
from averon_import.services.sourcing.service import SourcingService


_RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _intent(**updates) -> ProductIntent:
    values = {
        "source_row_id": "row-1",
        "source_text": "Valve K-100",
        "normalized_name": "Valve",
        "model": "K-100",
        "article": "",
        "attributes": {},
        "required_attributes": {},
        "preferred_attributes": {},
    }
    values.update(updates)
    return ProductIntent(**values)


def _offer(
    provider: str,
    offer_id: str = "same",
    *,
    title: str = "Valve K-100",
    article: str = "",
    manufacturer: str = "",
    attributes: dict | None = None,
    price: Decimal | None = Decimal("100"),
    currency: str = "RUB",
    availability: bool | None = True,
    retrieved_at: datetime = _RETRIEVED_AT,
) -> Offer:
    return Offer(
        provider=provider,
        offer_id=offer_id,
        title=title,
        article=article,
        manufacturer=manufacturer,
        attributes=attributes or {},
        price=price,
        currency=currency,
        availability=availability,
        retrieved_at=retrieved_at,
    )


def _execution(
    offers_by_provider: dict[str, list[Offer]],
    *,
    states: dict[str, ProviderSearchState] | None = None,
    selection_input: tuple[str, ...] | None = None,
) -> ProviderExecutionResult:
    states = states or {}
    provider_keys = tuple(selection_input or sorted(set(offers_by_provider) | set(states)))
    selection = ProviderSelection(provider_keys=provider_keys)
    outcomes = []
    for provider_key in selection.provider_keys:
        offers = tuple(offers_by_provider.get(provider_key, ()))
        state = (
            ProviderSearchState.SUCCESS
            if offers
            else states.get(provider_key, ProviderSearchState.EMPTY)
        )
        failed = state == ProviderSearchState.FAILURE
        outcomes.append(ProviderSearchOutcome(
            provider_key=provider_key,
            state=state,
            offers=offers,
            request_count=1 if failed else 0,
            failure_category=ProviderFailureCategory.TRANSPORT if failed else None,
        ))
    indexed = index_offers_by_provider_identity(
        offer for outcome in outcomes for offer in outcome.offers
    )
    return ProviderExecutionResult(
        selection=selection,
        result_limit=20,
        outcomes=tuple(outcomes),
        offers=tuple(indexed.values()),
        reused_provider_keys=(),
    )


def _evaluate(execution: ProviderExecutionResult, intent: ProductIntent | None = None):
    return SourcingService({}).evaluate_provider_execution(intent or _intent(), execution)


def _reference(provider: str, offer_id: str = "same") -> ProviderOfferReference:
    return ProviderOfferReference(provider_key=provider, offer_id=offer_id)


def test_shared_offer_id_is_two_composite_matches_and_provider_is_only_final_order_key():
    etm = _offer("etm_ipro")
    lemana = _offer("lemana_b2b")
    evaluation = _evaluate(_execution({"lemana_b2b": [lemana], "etm_ipro": [etm]}))

    assert [item.offer.offer_id for item in evaluation.matches] == ["same", "same"]
    assert [ProviderOfferReference.from_offer(item.offer) for item in evaluation.matches] == [
        _reference("etm_ipro"),
        _reference("lemana_b2b"),
    ]
    assert len(evaluation.matches) == 2
    assert evaluation.recommended_offer_reference is None
    assert evaluation.matches[0].decision == evaluation.matches[1].decision
    assert evaluation.matches[0].deterministic_evidence == evaluation.matches[1].deterministic_evidence


def test_equal_evidence_is_ambiguous_even_when_prices_differ():
    cheap = _offer("etm_ipro", price=Decimal("100"), currency="RUB", availability=True)
    expensive = _offer("lemana_b2b", price=Decimal("1000"), currency="USD", availability=False)
    evaluation = _evaluate(_execution({"etm_ipro": [cheap], "lemana_b2b": [expensive]}))

    assert evaluation.recommended_offer_reference is None
    assert evaluation.matches[0].deterministic_evidence == evaluation.matches[1].deterministic_evidence


def test_equal_existing_matcher_quality_with_different_signatures_stays_ambiguous():
    offers = [_offer("etm_ipro"), _offer("lemana_b2b")]
    execution = _execution({"etm_ipro": [offers[0]], "lemana_b2b": [offers[1]]})

    class EqualQualityMatcher:
        def match(self, intent, _offers):
            return [
                MatchResult(
                    offer=offers[0],
                    decision=MatchDecision.MATCH,
                    rank=1,
                    matched_attributes=["article"],
                ),
                MatchResult(
                    offer=offers[1],
                    decision=MatchDecision.MATCH,
                    rank=2,
                    matched_attributes=["model"],
                ),
            ]

    evaluation = SourcingService({}, matcher=EqualQualityMatcher()).evaluate_provider_execution(
        _intent(), execution,
    )

    assert evaluation.recommended_offer_reference is None


def test_stronger_match_precedes_weaker_likely_match_even_when_more_expensive():
    intent = _intent(model="K-100")
    stronger = _offer("etm_ipro", price=Decimal("1000"), attributes={"model": "K-100"})
    cheaper = _offer("lemana_b2b", title="Valve K-100", price=Decimal("1"))
    evaluation = _evaluate(
        _execution({"lemana_b2b": [cheaper], "etm_ipro": [stronger]}),
        intent,
    )

    assert {item.decision for item in evaluation.matches} == {
        MatchDecision.MATCH,
        MatchDecision.LIKELY_MATCH,
    }
    assert evaluation.recommended_offer_reference == _reference("etm_ipro")


def test_unique_stronger_evidence_in_same_decision_class_may_be_recommended():
    intent = _intent(model="K-100", required_attributes={"power": 1})
    stronger = _offer(
        "etm_ipro", attributes={"model": "K-100", "power": 1}, price=Decimal("1000"),
    )
    weaker = _offer(
        "lemana_b2b", title="Valve K-100", attributes={"power": 1}, price=Decimal("1"),
    )
    evaluation = _evaluate(_execution({"lemana_b2b": [weaker], "etm_ipro": [stronger]}), intent)

    assert all(item.decision == MatchDecision.MATCH for item in evaluation.matches)
    assert evaluation.recommended_offer_reference == _reference("etm_ipro")


def test_provider_and_offer_input_order_do_not_change_matches_or_recommendation():
    etm = _offer("etm_ipro", "same")
    etm_second = _offer("etm_ipro", "second")
    lemana = _offer("lemana_b2b", "same")
    first = _evaluate(_execution(
        {"etm_ipro": [etm, etm_second], "lemana_b2b": [lemana]},
        selection_input=("etm_ipro", "lemana_b2b"),
    ))
    second = _evaluate(_execution(
        {"lemana_b2b": [lemana], "etm_ipro": [etm_second, etm]},
        selection_input=("lemana_b2b", "etm_ipro"),
    ))

    assert first.selection.provider_keys == second.selection.provider_keys
    assert [ProviderOfferReference.from_offer(item.offer) for item in first.matches] == [
        ProviderOfferReference.from_offer(item.offer) for item in second.matches
    ]
    assert [item.deterministic_evidence for item in first.matches] == [
        item.deterministic_evidence for item in second.matches
    ]
    assert first.recommended_offer_reference == second.recommended_offer_reference is None


def test_success_match_and_partial_provider_failure_remain_separate():
    offer = _offer("etm_ipro")
    execution = _execution(
        {"etm_ipro": [offer]},
        states={"lemana_b2b": ProviderSearchState.FAILURE},
    )
    evaluation = _evaluate(execution)

    assert len(evaluation.matches) == 1
    assert evaluation.matches[0].offer == offer
    assert evaluation.partial_failure is True
    assert [(item.provider_key, item.state) for item in evaluation.outcomes] == [
        ("etm_ipro", ProviderSearchState.SUCCESS),
        ("lemana_b2b", ProviderSearchState.FAILURE),
    ]
    assert not any(item.decision == MatchDecision.REJECT for item in evaluation.matches)


def test_empty_plus_failure_has_no_fake_match_and_retains_both_outcomes():
    execution = _execution(
        {},
        states={
            "etm_ipro": ProviderSearchState.EMPTY,
            "lemana_b2b": ProviderSearchState.FAILURE,
        },
    )
    evaluation = _evaluate(execution)

    assert evaluation.matches == ()
    assert evaluation.recommended_offer_reference is None
    assert evaluation.partial_failure is True
    assert tuple(item.state for item in evaluation.outcomes) == (
        ProviderSearchState.EMPTY,
        ProviderSearchState.FAILURE,
    )


def test_price_currency_availability_and_time_do_not_change_identity_decision():
    base = _offer("etm_ipro", attributes={"model": "K-100"})
    variants = [
        _offer("etm_ipro", attributes={"model": "K-100"}, price=None),
        _offer("etm_ipro", attributes={"model": "K-100"}, price=Decimal("0"), currency="USD"),
        _offer("etm_ipro", attributes={"model": "K-100"}, availability=False),
        _offer(
            "etm_ipro",
            attributes={"model": "K-100"},
            retrieved_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    baseline = _evaluate(_execution({"etm_ipro": [base]}))
    baseline_result = baseline.matches[0]

    for offer in variants:
        result = _evaluate(_execution({"etm_ipro": [offer]})).matches[0]
        assert result.decision == baseline_result.decision
        assert result.deterministic_evidence == baseline_result.deterministic_evidence
        assert result.matched_attributes == baseline_result.matched_attributes
        assert result.supporting_attributes == baseline_result.supporting_attributes


def test_review_tie_has_no_provider_order_winner():
    intent = _intent(article="SKU-42", required_attributes={"power": 1})
    etm = _offer("etm_ipro", article="SKU-42")
    lemana = _offer("lemana_b2b", article="SKU-42")
    evaluation = _evaluate(_execution({"lemana_b2b": [lemana], "etm_ipro": [etm]}), intent)

    assert all(item.decision == MatchDecision.REVIEW for item in evaluation.matches)
    assert evaluation.review_candidate_reference is None


def test_unique_strong_review_candidate_has_exact_composite_reference():
    intent = _intent(article="SKU-42", model="K-100", required_attributes={"power": 1})
    weaker = _offer("etm_ipro", article="SKU-42")
    stronger = _offer(
        "lemana_b2b",
        article="SKU-42",
        attributes={"model": "K-100"},
    )
    evaluation = _evaluate(_execution({"etm_ipro": [weaker], "lemana_b2b": [stronger]}), intent)

    assert all(item.decision == MatchDecision.REVIEW for item in evaluation.matches)
    assert evaluation.review_candidate_reference == _reference("lemana_b2b")
    assert evaluation.review_candidate.offer == stronger


def test_duplicate_composite_execution_identity_fails_closed():
    offer = _offer("etm_ipro")
    execution = _execution({"etm_ipro": [offer]})
    corrupt = execution.model_copy(update={"offers": (offer, offer)})

    with pytest.raises(ProviderMatchEvaluationError, match="execution result failed structural validation"):
        _evaluate(corrupt)


def test_duplicate_composite_identity_inside_outcome_fails_closed():
    offer = _offer("etm_ipro")
    execution = _execution({"etm_ipro": [offer]})
    outcome = execution.outcomes[0].model_copy(update={"offers": (offer, offer)})
    corrupt = execution.model_copy(update={"outcomes": (outcome,)})

    with pytest.raises(ProviderMatchEvaluationError, match="duplicate composite offer identity"):
        _evaluate(corrupt)


def test_match_from_other_provider_with_same_offer_id_fails_closed():
    present = _offer("etm_ipro", "same")
    absent = _offer("lemana_b2b", "same")

    class ForeignMatcher:
        def match(self, intent, offers):
            return [MatchResult(offer=absent, decision=MatchDecision.MATCH, rank=1)]

    service = SourcingService({}, matcher=ForeignMatcher())
    with pytest.raises(ProviderMatchEvaluationError, match="absent from the exact execution result"):
        service.evaluate_provider_execution(_intent(), _execution({"etm_ipro": [present]}))


def test_duplicate_match_and_missing_match_fail_closed():
    etm = _offer("etm_ipro")
    lemana = _offer("lemana_b2b", "second")
    execution = _execution({"etm_ipro": [etm], "lemana_b2b": [lemana]})
    duplicate = MatchResult(offer=etm, decision=MatchDecision.MATCH, rank=1)

    class DuplicateMatcher:
        def match(self, intent, offers):
            return [duplicate, duplicate]

    class EmptyMatcher:
        def match(self, intent, offers):
            return []

    with pytest.raises(ProviderMatchEvaluationError, match="duplicate match for one composite"):
        SourcingService({}, matcher=DuplicateMatcher()).evaluate_provider_execution(_intent(), execution)
    with pytest.raises(ProviderMatchEvaluationError, match="exactly one result per execution offer"):
        SourcingService({}, matcher=EmptyMatcher()).evaluate_provider_execution(_intent(), execution)


def test_malformed_provider_key_fails_closed():
    offer = _offer("etm_ipro")
    execution = _execution({"etm_ipro": [offer]})
    malformed = offer.model_copy(update={"provider": "Bad Provider"})
    corrupt = execution.model_copy(update={"offers": (malformed,)})

    with pytest.raises(ProviderMatchEvaluationError, match="structural validation"):
        _evaluate(corrupt)


def test_evaluation_rejects_recommendation_reference_not_in_its_matches():
    offer = _offer("etm_ipro")
    execution = _execution({"etm_ipro": [offer]})
    match = OfferMatcher().match(_intent(), [offer])[0]

    with pytest.raises(ProviderMatchEvaluationError, match="recommended reference"):
        ProviderMatchEvaluation(
            intent=_intent(),
            execution=execution,
            matches=(match,),
            recommended_offer_reference=_reference("lemana_b2b"),
            review_candidate_reference=None,
        )


def test_service_evaluation_is_request_local_and_does_not_retrieve_rank_cache_or_persist(monkeypatch):
    offer = _offer("etm_ipro")
    execution = _execution({"etm_ipro": [offer]})
    service = SourcingService({})
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("evaluation must not retrieve, rank, cache, or persist")

    service.provider_runner = type("RunnerSpy", (), {"run": unexpected})()
    monkeypatch.setattr(service.ai, "rank_matches", unexpected)
    monkeypatch.setattr(service.cache, "get", unexpected)
    monkeypatch.setattr(service.cache, "set", unexpected)

    evaluation = service.evaluate_provider_execution(_intent(), execution)

    assert calls == []
    assert evaluation.execution is execution
    assert evaluation.selection is execution.selection


def test_single_provider_matcher_ties_keep_legacy_offer_id_order():
    first = _offer("etm_ipro", "a")
    second = _offer("etm_ipro", "z")

    matches = OfferMatcher().match(_intent(), [second, first])

    assert [item.offer.offer_id for item in matches] == ["a", "z"]
