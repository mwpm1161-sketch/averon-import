from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from averon_import.services.sourcing.models import MatchDecision, Offer, ProductIntent
from averon_import.services.sourcing.provider_commercial import (
    COMMERCIAL_EVIDENCE_RESOLVERS,
    CommercialComparability,
    CommercialComparabilityReason as Reason,
    CommercialEvaluationError,
    CommercialEvidenceState as State,
    CommercialIssueCode as Issue,
    CommercialOfferEvidence,
    ProviderCommercialEvaluation,
    VatBasis,
    compare_commercial_evidence,
    resolve_commercial_evidence,
)
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderOfferReference,
    ProviderSearchOutcome,
    ProviderSearchState,
    ProviderSelection,
    index_offers_by_provider_identity,
)
from averon_import.services.sourcing.providers.execution import (
    MAX_PROVIDER_EXECUTION_OFFERS,
    ProviderExecutionResult,
)
from averon_import.services.sourcing.service import SourcingService


_REVISION = "a" * 64
_RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _offer(provider="etm_ipro", offer_id="same", **updates):
    provenance = {
        "etm_ipro": {"source": provider, "source_item_id": "sku-1", "price_field": "pricewnds"},
        "lemana_b2b": {
            "source": provider, "product_item": "sku-1", "mirror_revision": _REVISION, "region_id": 1,
        },
    }.get(provider, {})
    values = dict(
        provider=provider, offer_id=offer_id, source_item_id="sku-1", title="Valve A-1",
        article="A-1", price=Decimal("100"), currency="RUB", price_unit="шт.",
        data_provenance=provenance, attributes={"nested": {"values": [1]}},
        retrieved_at=_RETRIEVED_AT,
    )
    values.update(updates)
    return Offer(**values)


def _matching(offers, *, states=None, affinities=None, versions=None):
    states, affinities, versions = states or {}, affinities or {}, versions or {}
    keys = sorted({item.provider for item in offers} | set(states))
    outcomes = []
    for key in keys:
        items = tuple(item for item in offers if item.provider == key)
        state = ProviderSearchState.SUCCESS if items else states[key]
        if items and key in states:
            state = states[key]
        outcomes.append(ProviderSearchOutcome(
            provider_key=key, state=state, offers=items,
            request_count=int(state in {ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS}),
            failure_category=ProviderFailureCategory.TRANSPORT if state in {
                ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS,
            } else None,
            affinity=affinities.get(key, ProviderAffinity()), catalog_version=versions.get(key),
        ))
    execution = ProviderExecutionResult(
        selection=ProviderSelection(provider_keys=tuple(keys)), result_limit=100,
        outcomes=tuple(outcomes), offers=tuple(index_offers_by_provider_identity(offers).values()),
        reused_provider_keys=(),
    )
    return SourcingService({}).evaluate_provider_execution(
        ProductIntent(source_row_id="row-1", source_text="Valve A-1", article="A-1"), execution,
    )


def _commercial(offers, **kwargs):
    return SourcingService({}).evaluate_provider_commercial_evidence(_matching(offers, **kwargs))


def _fixture(**updates):
    values = resolve_commercial_evidence(_offer()).model_dump(mode="python")
    values.update(updates)
    return CommercialOfferEvidence(**values)


def test_etm_exact_pricewnds_contract_is_complete_gross():
    result = _commercial([_offer()]).evidence[0]
    assert result.evidence_state == State.COMPLETE
    assert result.vat_basis == VatBasis.GROSS_INCLUDING_VAT
    assert (result.amount, result.currency, result.price_unit, result.unit_family) == (
        Decimal("100"), "RUB", "шт.", "piece",
    )
    assert result.issue_codes == ()


@pytest.mark.parametrize("field", ["price", "price_tarif", "", None, {}, "PRICEWNDS"])
def test_etm_other_price_fields_never_prove_vat(field):
    evidence = resolve_commercial_evidence(_offer(data_provenance={
        "source": "etm_ipro", "source_item_id": "sku-1", "price_field": field,
    }))
    assert evidence.evidence_state == State.INCOMPLETE
    assert evidence.vat_basis == VatBasis.UNKNOWN
    assert Issue.PROVIDER_PRICE_BASIS_UNPROVEN in evidence.issue_codes


@pytest.mark.parametrize("provenance", [
    {}, {"source": "lemana_b2b", "source_item_id": "sku-1", "price_field": "pricewnds"},
    {"source": "etm_ipro", "source_item_id": "other", "price_field": "pricewnds"},
    {"source": "etm_ipro", "source_item_id": 123, "price_field": "pricewnds"},
    {"source": "etm_ipro", "source_item_id": "sku-1", "price_field": "pricewnds", "price_status": "unknown"},
    {"source": "etm_ipro", "source_item_id": "sku-1", "price_field": "pricewnds", "currency": "USD"},
    {"source": "etm_ipro", "source_item_id": "sku-1", "price_field": "pricewnds", "price_unit": "кг"},
    {"source": "etm_ipro", "source_item_id": "sku-1", "price_field": "pricewnds", "vat_basis": "NET_EXCLUDING_VAT"},
])
def test_etm_contradictory_provenance_is_invalid(provenance):
    evidence = _commercial([_offer(data_provenance=provenance)]).evidence[0]
    assert evidence.evidence_state == State.INVALID
    assert Issue.PROVENANCE_MISMATCH in evidence.issue_codes
    assert not compare_commercial_evidence(evidence, _fixture()).comparable


@pytest.mark.parametrize("source_item_id", ["", " ", "x" * 181])
def test_etm_source_item_must_be_nonempty_bounded(source_item_id):
    evidence = resolve_commercial_evidence(_offer(source_item_id=source_item_id))
    assert evidence.evidence_state == State.INVALID


@pytest.mark.parametrize("updates,issue", [
    ({"price": None}, Issue.PRICE_MISSING), ({"price": Decimal("0")}, Issue.PRICE_NON_POSITIVE),
    ({"currency": ""}, Issue.CURRENCY_UNKNOWN), ({"currency": "x" * 200}, Issue.CURRENCY_UNKNOWN),
    ({"price_unit": ""}, Issue.PRICE_UNIT_UNKNOWN), ({"price_unit": "unknown"}, Issue.PRICE_UNIT_UNTRUSTED),
    ({"price_unit": "x" * 81}, Issue.PRICE_UNIT_UNKNOWN),
])
def test_missing_critical_etm_facts_are_incomplete(updates, issue):
    evidence = _commercial([_offer(**updates)]).evidence[0]
    assert evidence.evidence_state == State.INCOMPLETE
    assert issue in evidence.issue_codes
    assert not compare_commercial_evidence(evidence, _fixture()).comparable


def test_legacy_default_piece_is_not_explicit_unit_proof():
    offer = Offer(provider="etm_ipro", offer_id="a", source_item_id="sku-1", title="a",
                  price=100, currency="RUB", data_provenance=_offer().data_provenance)
    evidence = _commercial([offer]).evidence[0]
    assert evidence.price_unit == "шт."
    assert Issue.PRICE_UNIT_UNTRUSTED in evidence.issue_codes
    assert evidence.evidence_state == State.INCOMPLETE


def test_etm_preserves_actual_explicit_currency_without_rub_inference():
    evidence = _commercial([_offer(currency=" usd ")]).evidence[0]
    assert evidence.currency == "USD"
    assert evidence.evidence_state == State.COMPLETE
    assert _commercial([_offer(currency="")]).evidence[0].currency == ""


def test_lemana_preserves_facts_with_unknown_vat():
    evidence = _commercial([_offer("lemana_b2b", price=Decimal("9.75"), price_unit="кг")]).evidence[0]
    assert (evidence.amount, evidence.currency, evidence.price_unit, evidence.unit_family) == (
        Decimal("9.75"), "RUB", "кг", "kilogram",
    )
    assert evidence.vat_basis == VatBasis.UNKNOWN
    assert evidence.evidence_state == State.INCOMPLETE
    assert set(evidence.issue_codes) == {Issue.VAT_BASIS_UNKNOWN, Issue.PROVIDER_PRICE_BASIS_UNPROVEN}
    assert not compare_commercial_evidence(evidence, _fixture()).comparable


@pytest.mark.parametrize("claim", [basis.value for basis in VatBasis])
def test_lemana_raw_vat_claim_does_not_grant_authority(claim):
    provenance = {**_offer("lemana_b2b").data_provenance, "vat_basis": claim}
    evidence = _commercial([_offer("lemana_b2b", data_provenance=provenance)]).evidence[0]
    assert evidence.vat_basis == VatBasis.UNKNOWN
    assert evidence.evidence_state == State.INCOMPLETE


@pytest.mark.parametrize("field,value", [
    ("source", "etm_ipro"), ("product_item", "other"), ("product_item", None),
    ("mirror_revision", ""), ("mirror_revision", "unknown"), ("mirror_revision", "x" * 121),
    ("mirror_revision", {"token": "secret"}), ("region_id", None), ("region_id", 0),
    ("region_id", True), ("region_id", "1"), ("region_id", 2**63),
])
def test_lemana_invalid_provenance_is_invalid(field, value):
    provenance = {**_offer("lemana_b2b").data_provenance, field: value}
    evidence = _commercial([_offer("lemana_b2b", data_provenance=provenance)]).evidence[0]
    assert evidence.evidence_state == State.INVALID
    assert Issue.PROVENANCE_MISMATCH in evidence.issue_codes


@pytest.mark.parametrize("region,version,invalid", [("1", _REVISION, False), ("2", _REVISION, True), ("1", "b" * 64, True)])
def test_lemana_region_and_revision_correlate_to_execution(region, version, invalid):
    evidence = _commercial(
        [_offer("lemana_b2b")], affinities={"lemana_b2b": ProviderAffinity(region_id=region)},
        versions={"lemana_b2b": version},
    ).evidence[0]
    assert (evidence.evidence_state == State.INVALID) == invalid


def test_metadata_and_secrets_are_not_retained_in_public_evidence():
    provenance = {**_offer("lemana_b2b").data_provenance, "token": "super-secret", "raw_payload": {"a": "x" * 5000}}
    evidence = _commercial([_offer("lemana_b2b", data_provenance=provenance)]).evidence[0]
    serialized = evidence.model_dump_json()
    assert "super-secret" not in serialized
    assert "mirror_revision" not in serialized and "region_id" not in serialized
    assert evidence.basis_revision == "m2a-lemana-unproven-v1"
    assert all(len(item) <= 180 for item in [evidence.price_unit, evidence.currency, evidence.basis_revision])


@pytest.mark.parametrize("provider", ["local_catalog", "new_provider", "demo_store_http"])
def test_local_and_unknown_provider_price_is_not_commercial_authority(provider):
    evidence = _commercial([_offer(provider, data_provenance={"vat_basis": "GROSS_INCLUDING_VAT"})]).evidence[0]
    assert evidence.amount == 100 and evidence.currency == "RUB"
    assert evidence.evidence_state == State.INCOMPLETE
    assert evidence.vat_basis == VatBasis.UNKNOWN
    assert Issue.PROVIDER_PRICE_BASIS_UNPROVEN in evidence.issue_codes
    assert not compare_commercial_evidence(evidence, _fixture()).comparable
    assert _matching([_offer(provider)]).matches[0].decision == MatchDecision.MATCH


def test_resolver_registry_is_explicit_independent_and_read_only():
    assert tuple(COMMERCIAL_EVIDENCE_RESOLVERS) == ("etm_ipro", "lemana_b2b", "local_catalog")
    with pytest.raises(TypeError):
        COMMERCIAL_EVIDENCE_RESOLVERS["new_provider"] = object()


@pytest.mark.parametrize("unit,family", [
    ("шт.", "piece"), ("ШТУКА", "piece"), ("кг", "kilogram"), ("kg", "kilogram"),
    ("т", "tonne"), ("tonne", "tonne"), ("м", "meter"), ("m²", "square_meter"),
    ("м³", "cubic_meter"), ("л", "litre"),
])
def test_direct_unit_families_use_shared_aliases(unit, family):
    evidence = resolve_commercial_evidence(_offer(price_unit=unit))
    assert evidence.unit_family == family and evidence.evidence_state == State.COMPLETE
    assert compare_commercial_evidence(evidence, evidence).comparable


@pytest.mark.parametrize("unit", ["уп.", "упаковка", "компл.", "комплект"])
def test_packages_never_compare_without_proven_quantity_factor(unit):
    evidence = resolve_commercial_evidence(_offer(price_unit=unit, attributes={"pack_quantity": 1}))
    assert evidence.evidence_state == State.INCOMPLETE
    assert Issue.PACKAGING_BASIS_UNPROVEN in evidence.issue_codes
    result = compare_commercial_evidence(evidence, evidence)
    assert not result.comparable and Reason.PACKAGING_BASIS_UNPROVEN in result.reason_codes


@pytest.mark.parametrize("unit", ["г", "мл", "пар", "пог. м", "боб"])
def test_other_unit_families_do_not_expand_sourcing_trust(unit):
    evidence = resolve_commercial_evidence(_offer(price_unit=unit))
    assert evidence.unit_family is None
    assert Issue.PRICE_UNIT_UNTRUSTED in evidence.issue_codes


@pytest.mark.parametrize("left,right", [("кг", "т"), ("л", "мл"), ("шт.", "кг")])
def test_no_physical_unit_conversion(left, right):
    result = compare_commercial_evidence(
        resolve_commercial_evidence(_offer(price_unit=left)), resolve_commercial_evidence(_offer(price_unit=right)),
    )
    assert not result.comparable


def test_complete_fixture_can_compare_without_price_or_provider_preference():
    left = _fixture(amount=Decimal("1000"))
    right = _fixture(offer_reference=ProviderOfferReference(provider_key="fixture", offer_id="same"), amount=Decimal("1"))
    result = compare_commercial_evidence(left, right)
    assert result.comparable and result.reason_codes == ()
    assert set(type(result).model_fields) == {"left_reference", "right_reference", "comparable", "reason_codes"}


@pytest.mark.parametrize("updates,reason", [
    ({"currency": "USD"}, Reason.CURRENCY_MISMATCH),
    ({"vat_basis": VatBasis.NET_EXCLUDING_VAT}, Reason.VAT_BASIS_MISMATCH),
    ({"vat_basis": VatBasis.UNKNOWN, "evidence_state": State.INCOMPLETE, "issue_codes": (Issue.VAT_BASIS_UNKNOWN,)}, Reason.VAT_BASIS_UNKNOWN),
    ({"currency": "", "evidence_state": State.INCOMPLETE, "issue_codes": (Issue.CURRENCY_UNKNOWN,)}, Reason.CURRENCY_UNKNOWN),
    ({"price_unit": "кг", "unit_family": "kilogram"}, Reason.PRICE_UNIT_MISMATCH),
    ({"amount": Decimal("0"), "evidence_state": State.INCOMPLETE, "issue_codes": (Issue.PRICE_NON_POSITIVE,)}, Reason.PRICE_UNUSABLE),
    ({"evidence_state": State.INVALID, "issue_codes": (Issue.PROVENANCE_MISMATCH,)}, Reason.PROVENANCE_MISMATCH),
])
def test_comparability_failures_are_symmetric(updates, reason):
    left = _fixture()
    right = _fixture(offer_reference=ProviderOfferReference(provider_key="fixture", offer_id="other"), **updates)
    forward, reverse = compare_commercial_evidence(left, right), compare_commercial_evidence(right, left)
    assert not forward.comparable
    assert reason in forward.reason_codes
    assert forward.comparable == reverse.comparable and forward.reason_codes == reverse.reason_codes
    assert forward.left_reference == reverse.right_reference
    assert forward.right_reference == reverse.left_reference


def test_piece_aliases_compare_and_amount_changes_never_change_comparability():
    left = resolve_commercial_evidence(_offer(price_unit="шт.", price=1000000))
    right = resolve_commercial_evidence(_offer(offer_id="other", price_unit="штука", price=1))
    assert compare_commercial_evidence(left, right).comparable
    assert compare_commercial_evidence(right, left).comparable
    assert compare_commercial_evidence(_fixture(amount=1), _fixture(amount=1000000)).comparable


def test_comparison_has_no_cross_offer_amount_ordering_or_selection_fields():
    import averon_import.services.sourcing.provider_commercial as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops):
            # Amount checks only compare against scalar validity bounds, never another offer.
            operands = [node.left, *node.comparators]
            if any(isinstance(item, ast.Attribute) and item.attr == "amount" for item in operands):
                assert len(operands) == 2 and isinstance(operands[1], ast.Constant)
    prohibited = {"winner", "cheaper_reference", "price_delta", "savings", "rank", "preferred_provider"}
    assert not prohibited.intersection(CommercialComparability.model_fields)
    assert not prohibited.intersection(CommercialOfferEvidence.model_fields)
    assert not prohibited.intersection(ProviderCommercialEvaluation.__dataclass_fields__)


def test_ordering_and_same_offer_id_use_composite_identity():
    offers = [_offer("lemana_b2b"), _offer("etm_ipro"), _offer("local_catalog", "z"), _offer("local_catalog", "a")]
    first, second = _commercial(offers), _commercial(list(reversed(offers)))
    assert first.evidence == second.evidence
    assert [item.offer_reference.ordering_key for item in first.evidence] == [
        ("etm_ipro", "same"), ("lemana_b2b", "same"), ("local_catalog", "a"), ("local_catalog", "z"),
    ]


@pytest.mark.parametrize("kind", ["duplicate", "missing", "foreign", "wrong_amount", "wrong_vat", "wrong_provider"])
def test_correlation_rejects_missing_duplicate_foreign_or_forged_facts(kind):
    matching = _matching([_offer(), _offer("lemana_b2b")])
    records = SourcingService({}).evaluate_provider_commercial_evidence(matching).evidence
    if kind == "duplicate":
        records = (records[0], records[0])
    elif kind == "missing":
        records = records[:1]
    else:
        values = records[0].model_dump(mode="python")
        if kind in {"foreign", "wrong_provider"}:
            values["offer_reference"] = ProviderOfferReference(
                provider_key="other" if kind == "wrong_provider" else "etm_ipro", offer_id="absent",
            )
        elif kind == "wrong_amount":
            values["amount"] = Decimal("99")
        elif kind == "wrong_vat":
            values["vat_basis"] = VatBasis.NET_EXCLUDING_VAT
        records = (CommercialOfferEvidence(**values), records[1])
    with pytest.raises(CommercialEvaluationError):
        ProviderCommercialEvaluation(matching, records)


def test_reversed_records_canonicalize_at_aggregate_boundary():
    matching = _matching([_offer(), _offer("lemana_b2b")])
    expected = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    assert ProviderCommercialEvaluation(matching, tuple(reversed(expected.evidence))).evidence == expected.evidence


@pytest.mark.parametrize("provider,offer_id", [("Bad Provider", "a"), ("", "a"), ("a", ""), ("a", " "), ("a" * 101, "a"), ("a", "a" * 181)])
def test_evidence_rejects_malformed_composite_identity(provider, offer_id):
    with pytest.raises(ValidationError):
        _fixture(offer_reference={"provider_key": provider, "offer_id": offer_id})


@pytest.mark.parametrize("value", ["malformed", "NaN", "Infinity", "-Infinity", "-1", True, {}, "1e1000", "1" * 121])
def test_dto_rejects_invalid_prices_and_resolver_fails_closed(value):
    with pytest.raises(ValidationError):
        _fixture(amount=value)
    evidence = resolve_commercial_evidence(_offer().model_copy(update={"price": value}))
    assert evidence.evidence_state == State.INVALID
    assert evidence.amount is None and Issue.PRICE_INVALID in evidence.issue_codes


@pytest.mark.parametrize("value", [Decimal("1.25"), "1.25", 1, 1.25])
def test_decimal_compatible_amounts_are_preserved(value):
    assert _fixture(amount=value).amount == Decimal(str(value))


@pytest.mark.parametrize("updates", [
    {"amount": None}, {"amount": 0}, {"vat_basis": VatBasis.UNKNOWN},
    {"unit_family": "kilogram"}, {"price_unit": "up", "unit_family": None},
    {"currency": "rub"}, {"basis_revision": "token=secret"},
    {"issue_codes": (Issue.PRICE_MISSING, Issue.PRICE_MISSING)},
    {"issue_codes": ("raw-error",)}, {"issue_codes": []}, {"raw_payload": {}},
])
def test_evidence_state_bounds_and_closed_types_cannot_be_forged(updates):
    with pytest.raises(ValidationError):
        _fixture(**updates)


def test_bypass_constructed_reference_and_evidence_are_revalidated():
    bad_reference = ProviderOfferReference.model_construct(provider_key="INVALID KEY", offer_id="")
    with pytest.raises(ValidationError):
        _fixture(offer_reference=bad_reference)
    bad = _fixture().model_copy(update={"amount": Decimal("NaN")})
    with pytest.raises(ValidationError):
        compare_commercial_evidence(bad, _fixture())
    with pytest.raises(CommercialEvaluationError):
        ProviderCommercialEvaluation(_matching([_offer()]), (bad,))


def test_original_mutable_objects_and_all_public_views_are_detached():
    offer = _offer()
    matching = _matching([offer])
    commercial = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    expected = commercial.evidence
    offer.data_provenance["price_field"] = "price"
    offer.attributes["nested"]["values"].append(9)
    matching.intent.attributes["mutation"] = True
    matching._matches[0].offer.data_provenance["source"] = "mutation"
    for view in [commercial.execution, commercial.matching_evaluation.execution]:
        view.offers[0].data_provenance["source"] = "changed"
        view.offers[0].attributes["nested"]["values"].append(8)
    commercial.outcomes[0].offers[0].data_provenance.clear()
    view = commercial.matching_evaluation
    view.intent.attributes["mutation"] = True
    view._matches[0].decision = MatchDecision.REJECT
    assert commercial.evidence == expected
    assert commercial.execution.offers[0].data_provenance["price_field"] == "pricewnds"
    assert commercial.execution.offers[0].attributes["nested"]["values"] == [1]
    assert commercial.matching_evaluation.intent.attributes == {}
    assert commercial.matching_evaluation.matches[0].decision == MatchDecision.MATCH
    with pytest.raises(FrozenInstanceError):
        commercial.evidence = ()
    with pytest.raises(ValidationError):
        commercial.evidence[0].currency = "USD"
    with pytest.raises(ValidationError):
        commercial.evidence[0].offer_reference.offer_id = "changed"


def test_mutated_matching_execution_fails_revalidation():
    matching = _matching([_offer()])
    matching.execution.offers[0].data_provenance["source"] = "mutation"
    with pytest.raises(CommercialEvaluationError):
        SourcingService({}).evaluate_provider_commercial_evidence(matching)


def test_etm_incoherent_source_does_not_grant_currency_authority():
    evidence = resolve_commercial_evidence(_offer(data_provenance={"source": "other"}))
    assert evidence.currency == "" and Issue.CURRENCY_UNKNOWN in evidence.issue_codes


@pytest.mark.parametrize("version", [{"token": "secret"}, "x" * 121, ""])
def test_etm_catalog_metadata_must_be_structurally_bounded(version):
    provenance = {**_offer().data_provenance, "catalog_version": version}
    evidence = _commercial([_offer(data_provenance=provenance)]).evidence[0]
    assert evidence.evidence_state == State.INVALID


def test_etm_retained_catalog_revision_cannot_contradict_outcome():
    provenance = {**_offer().data_provenance, "catalog_version": "rev-1"}
    evidence = _commercial([_offer(data_provenance=provenance)], versions={"etm_ipro": "rev-2"}).evidence[0]
    assert evidence.evidence_state == State.INVALID


def test_incomplete_commercial_evidence_preserves_unique_identity_recommendation():
    matching = _matching([_offer("lemana_b2b")])
    assert matching.recommended_offer_reference == ProviderOfferReference(provider_key="lemana_b2b", offer_id="same")
    commercial = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    assert commercial.evidence[0].evidence_state == State.INCOMPLETE
    assert commercial.matching_evaluation.recommended_offer_reference == matching.recommended_offer_reference


def test_invalid_legacy_execution_price_is_rejected_at_aggregate_boundary():
    matching = _matching([_offer()])
    object.__setattr__(matching.execution.offers[0], "price", Decimal("NaN"))
    with pytest.raises(CommercialEvaluationError):
        SourcingService({}).evaluate_provider_commercial_evidence(matching)


@pytest.mark.parametrize("state", [
    ProviderSearchState.FAILURE, ProviderSearchState.EMPTY, ProviderSearchState.SUPPRESSED,
    ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.PARTIAL_SUCCESS,
])
def test_provider_outcomes_are_separate_from_actual_offer_evidence(state):
    offers = [_offer()]
    if state == ProviderSearchState.PARTIAL_SUCCESS:
        offers.append(_offer("lemana_b2b"))
    matching = _matching(offers, states={"lemana_b2b": state})
    commercial = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    assert len(commercial.evidence) == len(offers)
    assert commercial.outcomes == matching.outcomes
    assert commercial.partial_failure == matching.partial_failure
    assert not any(Issue.PRICE_MISSING in item.issue_codes for item in commercial.evidence)


def test_all_failed_or_empty_execution_has_no_fabricated_evidence():
    commercial = _commercial([], states={"etm_ipro": ProviderSearchState.FAILURE, "lemana_b2b": ProviderSearchState.EMPTY})
    assert commercial.evidence == () and len(commercial.outcomes) == 2


def test_aggregate_respects_existing_400_offer_bound():
    offers = [_offer(provider, str(i)) for provider in ("etm_ipro", "lemana_b2b", "local_catalog", "future") for i in range(100)]
    matching = _matching(offers)
    commercial = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    assert len(commercial.evidence) == MAX_PROVIDER_EXECUTION_OFFERS
    with pytest.raises(CommercialEvaluationError):
        ProviderCommercialEvaluation(matching, commercial.evidence + commercial.evidence[:1])


def test_identity_decisions_and_candidate_references_are_unchanged():
    matching = _matching([_offer(), _offer("lemana_b2b")])
    before = [(item.offer.provider, item.decision, item.deterministic_evidence) for item in matching.matches]
    commercial = SourcingService({}).evaluate_provider_commercial_evidence(matching)
    after = commercial.matching_evaluation
    assert before == [(item.offer.provider, item.decision, item.deterministic_evidence) for item in after.matches]
    assert after.recommended_offer_reference == matching.recommended_offer_reference
    assert after.review_candidate_reference == matching.review_candidate_reference
    assert {item.decision for item in after.matches} == {MatchDecision.MATCH}
    assert [item.evidence_state for item in commercial.evidence] == [State.COMPLETE, State.INCOMPLETE]
    assert matching.recommended_offer_reference is None


def test_commercial_service_boundary_has_no_retrieval_ai_cache_or_io(monkeypatch):
    matching = _matching([_offer()])
    poison = Mock(side_effect=AssertionError("commercial boundary performed runtime work"))
    provider = Mock()
    provider.search = provider.stats = provider.health = provider.check_access = poison
    service = SourcingService({"etm_ipro": provider}, provider_runner=Mock(run=poison))
    service.matcher.match = poison
    service.ai = Mock(understand=poison, rank=poison)
    service.cache = Mock(get=poison, set=poison)
    monkeypatch.setattr(service, "execute_provider_selection", poison)
    monkeypatch.setattr(service, "search_intent", poison)
    monkeypatch.setattr(service, "search_project", poison)
    monkeypatch.setattr("socket.create_connection", poison)
    monkeypatch.setattr(Path, "open", poison)
    assert service.evaluate_provider_commercial_evidence(matching).evidence[0].evidence_state == State.COMPLETE
    poison.assert_not_called()
    assert service.ai.mock_calls == [] and service.cache.mock_calls == [] and provider.mock_calls == []


def test_commercial_phase_is_not_automatically_called_by_existing_phases(monkeypatch):
    matching = _matching([_offer()])
    service = SourcingService({}, provider_runner=Mock(run=Mock(return_value=matching.execution)))
    poison = Mock(side_effect=AssertionError("commercial phase was implicitly activated"))
    monkeypatch.setattr(service, "evaluate_provider_commercial_evidence", poison)
    from averon_import.services.sourcing.providers.execution import ProviderExecutionScope

    execution = service.execute_provider_selection(
        matching.intent, selection=matching.selection, limit=20,
        execution_scope=ProviderExecutionScope(execution_scope_id="test"),
    )
    service.evaluate_provider_execution(matching.intent, execution)
    poison.assert_not_called()


def test_comparability_dto_rejects_contradictory_and_unbounded_results():
    values = dict(left_reference=_fixture().offer_reference, right_reference=_fixture().offer_reference)
    with pytest.raises(ValidationError):
        CommercialComparability(**values, comparable=True, reason_codes=(Reason.CURRENCY_MISMATCH,))
    with pytest.raises(ValidationError):
        CommercialComparability(**values, comparable=False, reason_codes=())
    with pytest.raises(ValidationError):
        CommercialComparability(**values, comparable=False, reason_codes=("secret",))
    with pytest.raises(ValidationError):
        CommercialComparability(**values, comparable=False, reason_codes=(Reason.CURRENCY_UNKNOWN,) * 50)
