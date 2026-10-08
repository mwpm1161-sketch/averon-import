from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import averon_import.services.sourcing.provider_commercial as commercial_module
from averon_import.core.unit_normalization import normalize_sourcing_unit_family
from averon_import.services.sourcing.models import MatchDecision, MatchResult, Offer, ProductIntent
from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState as EvidenceState,
    CommercialIssueCode as Issue,
    CommercialOfferEvidence,
    ProviderCommercialEvaluation,
    VatBasis,
)
from averon_import.services.sourcing.provider_commercial_selection import (
    CommercialSelectionBasis as Basis,
    CommercialSelectionError,
    CommercialSelectionReason as Reason,
    CommercialSelectionState as State,
    ProviderCommercialSelection,
    _SelectionDecision,
    select_provider_commercial_winner,
)
from averon_import.services.sourcing.provider_matching import (
    ProviderMatchEvaluation,
    _unique_identity_recommendation,
    _unique_reference,
    _unique_review_candidate,
)
from averon_import.services.sourcing.providers.contracts import (
    ProviderFailureCategory,
    ProviderOfferReference,
    ProviderSearchOutcome,
    ProviderSearchState,
    ProviderSelection,
    index_offers_by_provider_identity,
)
from averon_import.services.sourcing.providers.execution import MAX_PROVIDER_EXECUTION_OFFERS, ProviderExecutionResult
from averon_import.services.sourcing.service import SourcingService


def _reference(provider="fixture_a", offer_id="same"):
    return ProviderOfferReference(provider_key=provider, offer_id=offer_id)


def _offer(provider="fixture_a", offer_id="same", **updates):
    provenance = {
        "etm_ipro": {"source": provider, "source_item_id": "sku", "price_field": "pricewnds"},
        "lemana_b2b": {"source": provider, "product_item": "sku", "mirror_revision": "a" * 64, "region_id": 1},
    }.get(provider, {"source": provider})
    values = dict(
        provider=provider, offer_id=offer_id, source_item_id="sku", title="Valve", article="A-1",
        price=Decimal("100"), currency="RUB", price_unit="шт.", availability=True,
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attributes={"stock": 1, "nested": {"values": [1]}}, data_provenance=provenance,
    )
    values.update(updates)
    return Offer(**values)


def _match(offer, decision=MatchDecision.MATCH, **updates):
    values = dict(
        offer=offer, decision=decision, rank=1, matched_attributes=["article"],
        deterministic_evidence={"preferred_differences": [], "nested": {"values": [1]}},
    )
    values.update(updates)
    return MatchResult(**values)


class _CompleteFixtureResolver:
    """Test-only commercial contracts; production resolver registry is unchanged."""

    def resolve(self, offer, outcome=None):
        amount, currency, unit, issues = commercial_module._facts(offer)
        vat = VatBasis(offer.data_provenance.get("fixture_vat", VatBasis.GROSS_INCLUDING_VAT))
        if offer.data_provenance.get("fixture_state") == "INVALID":
            issues.add(Issue.PROVENANCE_MISMATCH)
        if vat == VatBasis.UNKNOWN:
            issues.add(Issue.VAT_BASIS_UNKNOWN)
        state = EvidenceState.INVALID if Issue.PROVENANCE_MISMATCH in issues else (
            EvidenceState.INCOMPLETE if issues else EvidenceState.COMPLETE
        )
        return CommercialOfferEvidence(
            offer_reference=ProviderOfferReference.from_offer(offer), amount=amount,
            currency=currency, vat_basis=vat, price_unit=unit,
            unit_family=normalize_sourcing_unit_family(unit), evidence_state=state,
            issue_codes=tuple(issues), basis_revision="m2a-provider-unproven-v1",
        )


@pytest.fixture
def commercial_factory(monkeypatch):
    registry = dict(commercial_module.COMMERCIAL_EVIDENCE_RESOLVERS)
    registry.update({key: _CompleteFixtureResolver() for key in ("fixture_a", "fixture_b", "fixture_c", "fixture_d")})
    monkeypatch.setattr(commercial_module, "COMMERCIAL_EVIDENCE_RESOLVERS", MappingProxyType(registry))

    def build(offers, *, matches=None, states=None, reverse_selection=False, reverse_evidence=False):
        states = states or {}
        keys = sorted({item.provider for item in offers} | set(states), reverse=reverse_selection)
        selection = ProviderSelection(provider_keys=tuple(keys))
        outcomes = []
        for key in selection.provider_keys:
            items = tuple(item for item in offers if item.provider == key)
            state = states.get(key, ProviderSearchState.SUCCESS if items else ProviderSearchState.EMPTY)
            failed = state in {ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS}
            outcomes.append(ProviderSearchOutcome(
                provider_key=key, state=state, offers=items, request_count=int(failed),
                failure_category=ProviderFailureCategory.TRANSPORT if failed else None,
            ))
        execution = ProviderExecutionResult(
            selection=selection, result_limit=100, outcomes=tuple(outcomes),
            offers=tuple(index_offers_by_provider_identity(offers).values()), reused_provider_keys=(),
        )
        matches = tuple(matches if matches is not None else (_match(item) for item in offers))
        matching = ProviderMatchEvaluation(
            intent=ProductIntent(source_row_id="row", source_text="Valve", article="A-1"),
            execution=execution, matches=matches,
            recommended_offer_reference=_unique_reference(_unique_identity_recommendation(matches)),
            review_candidate_reference=_unique_reference(_unique_review_candidate(matches)),
        )
        evidence = tuple(commercial_module.resolve_commercial_evidence(offer, next(
            item for item in outcomes if item.provider_key == offer.provider
        )) for offer in offers)
        if reverse_evidence:
            evidence = tuple(reversed(evidence))
        return ProviderCommercialEvaluation(matching, evidence)

    return build


def _fields(selection):
    return {name: getattr(selection, name) for name in (
        "state", "selected_reference", "candidate_references", "reason_codes", "selection_basis",
    )}


def _construct(evaluation, **updates):
    values = _fields(select_provider_commercial_winner(evaluation))
    values.update(updates)
    return ProviderCommercialSelection(evaluation, **values)


@pytest.mark.parametrize("strong,weak", [
    (MatchDecision.MATCH, MatchDecision.LIKELY_MATCH),
    (MatchDecision.MATCH, MatchDecision.ALTERNATIVE),
    (MatchDecision.LIKELY_MATCH, MatchDecision.ALTERNATIVE),
])
def test_cheaper_weaker_identity_cannot_win(commercial_factory, strong, weak):
    expensive, cheap = _offer("etm_ipro", price=1000), _offer("fixture_b", price=1)
    evaluation = commercial_factory([cheap, expensive], matches=[_match(cheap, weak), _match(expensive, strong)])
    selection = select_provider_commercial_winner(evaluation)
    assert selection.state == State.SELECTED
    assert selection.selected_reference == _reference("etm_ipro")
    assert selection.candidate_references == (_reference("etm_ipro"),)
    assert selection.selection_basis == Basis.SOLE_STRONGEST_IDENTITY


@pytest.mark.parametrize("weaker", [
    {"conflicting_attributes": ["brand"]}, {"missing_attributes": ["power"]},
    {"deterministic_evidence": {"preferred_differences": ["brand"]}},
    {"matched_attributes": []}, {"supporting_attributes": []},
])
def test_all_existing_m1d_quality_dimensions_precede_price(commercial_factory, weaker):
    expensive, cheap = _offer("fixture_a", price=1000), _offer("fixture_b", price=1)
    strong = _match(expensive, supporting_attributes=["brand"])
    weak_values = {"supporting_attributes": ["brand"], **weaker}
    evaluation = commercial_factory([expensive, cheap], matches=[strong, _match(cheap, **weak_values)])
    selection = select_provider_commercial_winner(evaluation)
    assert selection.selected_reference == _reference("fixture_a")
    assert selection.selection_basis == Basis.SOLE_STRONGEST_IDENTITY


@pytest.mark.parametrize("decision", [MatchDecision.REVIEW, MatchDecision.REJECT])
def test_review_and_reject_never_win(commercial_factory, decision):
    offer = _offer()
    selection = select_provider_commercial_winner(commercial_factory([offer], matches=[_match(offer, decision)]))
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.candidate_references == ()
    assert selection.reason_codes == (Reason.NO_IDENTITY_CANDIDATE,)


@pytest.mark.parametrize("decision", [MatchDecision.MATCH, MatchDecision.LIKELY_MATCH, MatchDecision.ALTERNATIVE])
def test_first_eligible_decision_can_supply_a_sole_candidate(commercial_factory, decision):
    offer = _offer()
    selection = select_provider_commercial_winner(commercial_factory([offer], matches=[_match(offer, decision)]))
    assert selection.state == State.SELECTED and selection.selection_basis == Basis.SOLE_STRONGEST_IDENTITY


@pytest.mark.parametrize("reverse", [False, True])
def test_unique_lowest_equal_identity_price_is_independent_of_every_input_order(commercial_factory, reverse):
    cheap, expensive = _offer("fixture_b", price=100), _offer("fixture_a", price=120)
    offers = [cheap, expensive][::(-1 if reverse else 1)]
    evaluation = commercial_factory(offers, reverse_selection=reverse, reverse_evidence=reverse)
    selection = select_provider_commercial_winner(evaluation)
    assert selection.state == State.SELECTED
    assert selection.selected_reference == _reference("fixture_b")
    assert selection.selection_basis == Basis.LOWEST_COMPARABLE_PRICE
    assert selection.candidate_references == (_reference("fixture_a"), _reference("fixture_b"))
    assert selection.reason_codes == ()
    other = commercial_factory(list(reversed(offers)), reverse_selection=not reverse, reverse_evidence=not reverse)
    assert selection == select_provider_commercial_winner(other)


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_minimum_is_ambiguous_without_provider_or_offer_tie_break(commercial_factory, reverse):
    offers = [_offer("fixture_a", "z", price="100.00"), _offer("fixture_b", "a", price="100.0"), _offer("fixture_c", price=120)]
    selection = select_provider_commercial_winner(commercial_factory(offers[::(-1 if reverse else 1)]))
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.selected_reference is None and selection.selection_basis is None
    assert selection.reason_codes == (Reason.LOWEST_PRICE_TIED,)
    assert len(selection.candidate_references) == 3


def test_exact_decimal_prices_use_no_epsilon_or_float(commercial_factory):
    cheap = _offer("fixture_b", price="100.0000000000000000000000000000000000000001")
    expensive = _offer("fixture_a", price="100.0000000000000000000000000000000000000002")
    selection = select_provider_commercial_winner(commercial_factory([expensive, cheap]))
    assert selection.selected_reference == _reference("fixture_b")
    assert selection.selection_basis == Basis.LOWEST_COMPARABLE_PRICE


def test_incomplete_lemana_top_candidate_blocks_complete_etm(commercial_factory):
    selection = select_provider_commercial_winner(commercial_factory([_offer("etm_ipro"), _offer("lemana_b2b", price=10000)]))
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.reason_codes == (Reason.COMMERCIAL_EVIDENCE_INCOMPLETE,)
    assert selection.candidate_references == (_reference("etm_ipro"), _reference("lemana_b2b"))


def test_invalid_equal_top_candidate_is_not_removed(commercial_factory):
    invalid = _offer("etm_ipro", data_provenance={"source": "wrong"})
    selection = select_provider_commercial_winner(commercial_factory([invalid, _offer("fixture_a")]))
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.reason_codes == (Reason.COMMERCIAL_EVIDENCE_INVALID,)
    assert len(selection.candidate_references) == 2


def test_both_invalid_and_incomplete_top_candidates_keep_both_reasons(commercial_factory):
    evaluation = commercial_factory([
        _offer("etm_ipro", data_provenance={}), _offer("lemana_b2b"), _offer("fixture_a"),
    ])
    selection = select_provider_commercial_winner(evaluation)
    assert selection.reason_codes == (Reason.COMMERCIAL_EVIDENCE_INCOMPLETE, Reason.COMMERCIAL_EVIDENCE_INVALID)
    assert len(selection.candidate_references) == 3 and selection.selected_reference is None


@pytest.mark.parametrize("updates", [
    {"currency": "USD"}, {"data_provenance": {"fixture_vat": VatBasis.NET_EXCLUDING_VAT}},
    {"price_unit": "кг"},
])
def test_noncomparable_basis_blocks_whole_top_cohort(commercial_factory, updates):
    evaluation = commercial_factory([_offer("fixture_a"), _offer("fixture_b", price=120, **updates)])
    assert all(item.evidence_state == EvidenceState.COMPLETE for item in evaluation.evidence)
    selection = select_provider_commercial_winner(evaluation)
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.reason_codes == (Reason.COMMERCIAL_BASIS_NOT_COMPARABLE,)


def test_comparable_subset_cannot_replace_whole_top_cohort(commercial_factory):
    evaluation = commercial_factory([_offer("fixture_a"), _offer("fixture_b", price=120), _offer("fixture_c", currency="USD", price=1)])
    selection = select_provider_commercial_winner(evaluation)
    assert selection.state == State.NO_SAFE_WINNER and len(selection.candidate_references) == 3
    assert selection.reason_codes == (Reason.COMMERCIAL_BASIS_NOT_COMPARABLE,)


def test_every_pair_uses_approved_m2a_comparability(commercial_factory, monkeypatch):
    import averon_import.services.sourcing.provider_commercial_selection as module

    evaluation = commercial_factory([_offer("fixture_a", price=100), _offer("fixture_b", price=120), _offer("fixture_c", price=130)])
    compare = Mock(wraps=commercial_module.compare_commercial_evidence)
    monkeypatch.setattr(module, "compare_commercial_evidence", compare)
    assert select_provider_commercial_winner(evaluation).state == State.SELECTED
    assert compare.call_count == 3


@pytest.mark.parametrize("price,unit", [(None, "шт."), (0, "шт."), (100, ""), (100, "уп."), (100, "компл.")])
def test_sole_candidate_still_requires_complete_evidence(commercial_factory, price, unit):
    selection = select_provider_commercial_winner(commercial_factory([_offer("etm_ipro", price=price, price_unit=unit)]))
    assert selection.state == State.NO_SAFE_WINNER
    assert selection.reason_codes == (Reason.COMMERCIAL_EVIDENCE_INCOMPLETE,)


def test_weaker_incomplete_offer_does_not_block_stronger_complete_identity(commercial_factory):
    strong, weak = _offer("etm_ipro"), _offer("lemana_b2b", price=1)
    selection = select_provider_commercial_winner(commercial_factory([strong, weak], matches=[_match(strong), _match(weak, MatchDecision.LIKELY_MATCH)]))
    assert selection.selected_reference == _reference("etm_ipro")
    assert selection.candidate_references == (_reference("etm_ipro"),)


def test_partial_failure_selects_only_actual_returned_candidate(commercial_factory):
    evaluation = commercial_factory([_offer("etm_ipro")], states={"lemana_b2b": ProviderSearchState.FAILURE})
    selection = select_provider_commercial_winner(evaluation)
    assert selection.selected_reference == _reference("etm_ipro")
    assert selection.selection_basis == Basis.SOLE_STRONGEST_IDENTITY
    assert selection.partial_failure is True
    assert selection.commercial_evaluation.outcomes == evaluation.outcomes
    assert selection.candidate_references == (_reference("etm_ipro"),)


def test_no_returned_candidates_produces_no_identity_winner(commercial_factory):
    selection = select_provider_commercial_winner(commercial_factory([], states={"etm_ipro": ProviderSearchState.FAILURE}))
    assert selection.state == State.NO_SAFE_WINNER and selection.candidate_references == ()
    assert selection.reason_codes == (Reason.NO_IDENTITY_CANDIDATE,)


@pytest.mark.parametrize("availability_a,availability_b", [(True, False), (False, True), (None, None)])
def test_availability_stock_rank_and_retrieval_time_never_choose_winner(commercial_factory, availability_a, availability_b):
    cheap = _offer("fixture_b", price=100, availability=availability_b, attributes={"stock": 0, "warehouse": "b"})
    expensive = _offer("fixture_a", price=120, availability=availability_a, attributes={"stock": 99999, "delivery": "today"},
                       retrieved_at=datetime(2035, 1, 1, tzinfo=timezone.utc))
    selection = select_provider_commercial_winner(commercial_factory([expensive, cheap], matches=[_match(expensive, rank=1), _match(cheap, rank=999)]))
    assert selection.selected_reference == _reference("fixture_b")


@pytest.mark.parametrize("default_provider", ["fixture_a", "fixture_b", "etm_ipro"])
def test_service_default_provider_is_not_winner_policy(commercial_factory, default_provider):
    evaluation = commercial_factory([_offer("fixture_a", price=120), _offer("fixture_b", price=100)])
    service = SourcingService({}, default_provider=default_provider)
    assert service.select_provider_commercial_winner(evaluation).selected_reference == _reference("fixture_b")


def test_duplicate_offer_id_from_different_providers_remains_distinct(commercial_factory):
    selection = select_provider_commercial_winner(commercial_factory([_offer("fixture_a", price=120), _offer("fixture_b", price=100)]))
    assert len(selection.candidate_references) == 2
    assert {item.offer_id for item in selection.candidate_references} == {"same"}
    assert selection.selected_reference == _reference("fixture_b")


@pytest.mark.parametrize("updates", [
    {"selected_reference": None}, {"selected_reference": _reference("absent")},
    {"selected_reference": _reference("fixture_b")}, {"state": State.NO_SAFE_WINNER},
    {"candidate_references": (_reference("fixture_a"), _reference("fixture_a"))},
    {"candidate_references": ()}, {"selection_basis": Basis.SOLE_STRONGEST_IDENTITY},
    {"reason_codes": (Reason.LOWEST_PRICE_TIED,)}, {"reason_codes": ("raw_error_secret",)},
    {"state": "BAD"}, {"selection_basis": "CHEAPEST_ANY_PROVIDER"},
    {"candidate_references": [_reference()]},
])
def test_constructed_selection_rejects_contradictions(commercial_factory, updates):
    evaluation = commercial_factory([_offer("fixture_a", price=100), _offer("fixture_b", price=120)])
    with pytest.raises(CommercialSelectionError):
        _construct(evaluation, **updates)


@pytest.mark.parametrize("provider,offer_id", [("Bad Provider", "a"), ("", "a"), ("a", ""), ("a", " "), ("a" * 101, "a"), ("a", "a" * 181)])
def test_selection_rejects_malformed_composite_references(commercial_factory, provider, offer_id):
    evaluation = commercial_factory([_offer()])
    with pytest.raises(CommercialSelectionError):
        _construct(evaluation, selected_reference={"provider_key": provider, "offer_id": offer_id})


@pytest.mark.parametrize("kind", ["incomplete", "invalid", "tied", "weaker"])
def test_explicit_selection_cannot_override_blocking_or_identity_rules(commercial_factory, kind):
    left, right = _offer("etm_ipro"), _offer("fixture_b")
    matches = None
    if kind == "incomplete":
        right = _offer("lemana_b2b")
    elif kind == "invalid":
        right = _offer("fixture_b", data_provenance={"fixture_state": "INVALID"})
    elif kind == "weaker":
        matches = [_match(left), _match(right, MatchDecision.LIKELY_MATCH)]
    evaluation = commercial_factory([left, right], matches=matches)
    with pytest.raises(CommercialSelectionError):
        _construct(evaluation, state=State.SELECTED, selected_reference=ProviderOfferReference.from_offer(right),
                   candidate_references=(_reference("etm_ipro"), ProviderOfferReference.from_offer(right)),
                   reason_codes=(), selection_basis=Basis.LOWEST_COMPARABLE_PRICE)


def test_explicit_valid_decision_is_accepted_and_order_canonicalizes(commercial_factory):
    evaluation = commercial_factory([_offer("fixture_a", price=100), _offer("fixture_b", price=120)])
    expected = select_provider_commercial_winner(evaluation)
    actual = _construct(evaluation, candidate_references=tuple(reversed(expected.candidate_references)))
    assert actual == expected


@pytest.mark.parametrize("corruption", ["amount", "currency", "unit", "reference", "missing", "duplicate", "nonfinite", "float"])
def test_corrupted_m2a_model_copy_evidence_is_rejected_without_resolution(commercial_factory, corruption):
    evaluation = commercial_factory([_offer(), _offer("fixture_b", price=120)])
    records = evaluation.evidence
    updates = {
        "amount": {"amount": Decimal("1")}, "currency": {"currency": "USD"},
        "unit": {"price_unit": "кг", "unit_family": "kilogram"},
        "reference": {"offer_reference": _reference("absent")},
        "nonfinite": {"amount": Decimal("NaN")}, "float": {"amount": 100.0},
    }
    if corruption == "missing":
        records = records[1:]
    elif corruption == "duplicate":
        records = (records[0], records[0])
    else:
        records = (records[0].model_copy(update=updates[corruption]), records[1])
    object.__setattr__(evaluation, "evidence", records)
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(evaluation)


@pytest.mark.parametrize("target", ["execution", "match", "intent", "provenance"])
def test_mutated_retained_matching_state_is_rejected(commercial_factory, target):
    evaluation = commercial_factory([_offer()])
    matching = evaluation._matching
    if target == "execution":
        object.__setattr__(matching.execution.offers[0], "price", Decimal("1"))
    elif target == "match":
        matching._matches[0].matched_attributes.clear()
    elif target == "intent":
        matching.intent.attributes["mutation"] = True
    else:
        matching.execution.offers[0].data_provenance["source"] = "wrong"
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(evaluation)


def test_model_construct_and_missing_validation_witness_fail_closed(commercial_factory):
    evaluation = commercial_factory([_offer()])
    forged = object.__new__(ProviderCommercialEvaluation)
    object.__setattr__(forged, "_matching", evaluation.matching_evaluation)
    object.__setattr__(forged, "evidence", evaluation.evidence)
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(forged)
    raw = CommercialOfferEvidence.model_construct(offer_reference=_reference(), amount=Decimal("NaN"))
    object.__setattr__(evaluation, "evidence", (raw,))
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(evaluation)


def test_corrupt_constructed_or_copied_scalar_decision_is_revalidated(commercial_factory):
    evaluation = commercial_factory([_offer()])
    original = _SelectionDecision(**_fields(select_provider_commercial_winner(evaluation)))
    for forged in [original.model_copy(update={"selected_reference": _reference("absent")}),
                   _SelectionDecision.model_construct(**{**original.model_dump(mode="python"), "state": State.NO_SAFE_WINNER})]:
        with pytest.raises(CommercialSelectionError):
            ProviderCommercialSelection(evaluation, **forged.model_dump(mode="python", warnings=False))


def test_selection_retains_detached_snapshots_and_frozen_public_fields(commercial_factory):
    offer = _offer()
    evaluation = commercial_factory([offer])
    selection = select_provider_commercial_winner(evaluation)
    expected = _fields(selection)
    offer.attributes["nested"]["values"].append(2)
    evaluation._matching._matches[0].decision = MatchDecision.REJECT
    evaluation._matching.execution.offers[0].data_provenance.clear()
    object.__setattr__(evaluation, "evidence", ())
    exposed = selection.commercial_evaluation
    exposed._matching._matches[0].decision = MatchDecision.REJECT
    exposed._matching.intent.attributes["changed"] = True
    exposed._matching.execution.offers[0].data_provenance.clear()
    object.__setattr__(exposed, "evidence", ())
    assert _fields(selection) == expected
    assert selection.commercial_evaluation.matching_evaluation.matches[0].decision == MatchDecision.MATCH
    assert selection.commercial_evaluation.matching_evaluation.intent.attributes == {}
    assert selection.commercial_evaluation.execution.offers[0].attributes["nested"]["values"] == [1]
    assert len(selection.commercial_evaluation.evidence) == 1
    for name, value in [("state", State.NO_SAFE_WINNER), ("selected_reference", None), ("candidate_references", ()),
                        ("reason_codes", (Reason.LOWEST_PRICE_TIED,)), ("selection_basis", None)]:
        with pytest.raises(FrozenInstanceError):
            setattr(selection, name, value)
    with pytest.raises(ValidationError):
        selection.selected_reference.offer_id = "changed"


def test_candidate_and_reason_collections_are_bounded(commercial_factory):
    evaluation = commercial_factory([_offer()])
    with pytest.raises(CommercialSelectionError):
        _construct(evaluation, candidate_references=(_reference(),) * (MAX_PROVIDER_EXECUTION_OFFERS + 1))
    with pytest.raises(CommercialSelectionError):
        _construct(evaluation, reason_codes=(Reason.LOWEST_PRICE_TIED,) * 6)
    with pytest.raises(CommercialSelectionError):
        ProviderCommercialSelection(evaluation, selected_reference=_reference())
    with pytest.raises(TypeError):
        _construct(evaluation, raw_payload={"secret": "x"})


def test_maximum_execution_cohort_is_bounded_and_remains_ambiguous(commercial_factory):
    offers = [_offer(provider, str(index), price=100 + index)
              for provider in ("fixture_a", "fixture_b", "fixture_c", "fixture_d") for index in range(100)]
    selection = select_provider_commercial_winner(commercial_factory(offers))
    assert len(selection.candidate_references) == MAX_PROVIDER_EXECUTION_OFFERS
    assert selection.state == State.NO_SAFE_WINNER and selection.reason_codes == (Reason.LOWEST_PRICE_TIED,)


def test_removing_explicit_unit_proof_from_retained_offer_is_rejected(commercial_factory):
    evaluation = commercial_factory([_offer("etm_ipro")])
    evaluation._matching.execution.offers[0].__pydantic_fields_set__.remove("price_unit")
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(evaluation)


def test_model_copy_float_execution_price_is_rejected(commercial_factory):
    evaluation = commercial_factory([_offer()])
    object.__setattr__(evaluation._matching.execution.offers[0], "price", 100.0)
    with pytest.raises(CommercialSelectionError):
        select_provider_commercial_winner(evaluation)


def test_local_and_unknown_providers_remain_unproven(commercial_factory):
    for provider in ("local_catalog", "future_provider"):
        selection = select_provider_commercial_winner(commercial_factory([_offer(provider)]))
        assert selection.reason_codes == (Reason.COMMERCIAL_EVIDENCE_INCOMPLETE,)


def test_service_selection_calls_no_resolution_matching_runtime_ai_cache_or_io(commercial_factory, monkeypatch):
    evaluation = commercial_factory([_offer("fixture_a", price=100), _offer("fixture_b", price=120)])
    poison = Mock(side_effect=AssertionError("M2B invoked forbidden runtime work"))
    provider = Mock(search=poison, stats=poison, health=poison, check_access=poison)
    service = SourcingService({"fixture_a": provider}, provider_runner=Mock(run=poison), matcher=Mock(match=poison))
    service.ai = Mock(understand=poison, rank=poison)
    service.cache = Mock(get=poison, set=poison)
    for name in ("resolve_commercial_evidence", "_resolve_snapshot", "evaluate_provider_commercial_evidence"):
        monkeypatch.setattr(commercial_module, name, poison)
    monkeypatch.setattr(commercial_module, "COMMERCIAL_EVIDENCE_RESOLVERS", {})
    monkeypatch.setattr(ProviderCommercialEvaluation, "__init__", poison)
    for name in ("search_intent", "search_project", "execute_provider_selection", "evaluate_provider_execution", "evaluate_provider_commercial_evidence"):
        monkeypatch.setattr(service, name, poison)
    monkeypatch.setattr(Path, "open", poison)
    monkeypatch.setattr("builtins.open", poison)
    monkeypatch.setattr("socket.create_connection", poison)
    monkeypatch.setattr("socket.socket", poison)
    assert service.select_provider_commercial_winner(evaluation).selected_reference == _reference("fixture_a")
    poison.assert_not_called()
    assert provider.mock_calls == [] and service.ai.mock_calls == [] and service.cache.mock_calls == []


def test_selection_is_not_automatically_activated_by_prior_phases(commercial_factory, monkeypatch):
    evaluation = commercial_factory([_offer("etm_ipro")])
    service = SourcingService({}, provider_runner=Mock(run=Mock(return_value=evaluation.execution)))
    poison = Mock(side_effect=AssertionError("M2B was implicitly activated"))
    monkeypatch.setattr(service, "select_provider_commercial_winner", poison)
    from averon_import.services.sourcing.providers.execution import ProviderExecutionScope

    matching = evaluation.matching_evaluation
    execution = service.execute_provider_selection(matching.intent, selection=matching.selection, limit=20,
                                                  execution_scope=ProviderExecutionScope(execution_scope_id="test"))
    matching = service.evaluate_provider_execution(matching.intent, execution)
    service.evaluate_provider_commercial_evidence(matching)
    poison.assert_not_called()


def test_m2b_preserves_m1d_references_and_m2a_evidence(commercial_factory):
    evaluation = commercial_factory([_offer("fixture_a", price=100), _offer("fixture_b", price=120)])
    before = deepcopy(evaluation)
    selection = select_provider_commercial_winner(evaluation)
    assert evaluation == before
    assert selection.commercial_evaluation == before
    matching = selection.commercial_evaluation.matching_evaluation
    assert matching.recommended_offer_reference is None and matching.review_candidate_reference is None
    assert selection.selected_reference == _reference("fixture_a")
