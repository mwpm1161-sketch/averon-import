from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from averon_import.services.manual_tenders.history_decisions import (
    MAX_HISTORY_DECISION_EVENTS_PER_RUN,
    MAX_HISTORY_DECISION_LEDGER_BYTES,
)
from averon_import.services.manual_tenders.repository import (
    MAX_TENDER_RUN_BYTES,
    MAX_TENDER_RUNS_PER_WORKSPACE,
    MAX_TENDER_STORAGE_BYTES,
)
from averon_import.services.sourcing.models import Offer
from averon_import.services.sourcing.providers.contracts import (
    CommercialEvidenceContract,
    PriceBasis,
    ProviderAffinity,
    ProviderExecutionSummary,
    ProviderHealthSnapshot,
    ProviderHealthStatus,
    ProviderOfferReference,
    ProviderRunState,
    ProviderRunSummaryItem,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
    ProviderSelection,
    TaxBasis,
    index_offers_by_provider_identity,
    resolve_provider_selection,
)


def _offer(
    provider: str,
    offer_id: str,
    *,
    title: str | None = None,
    source_item_id: str | None = None,
) -> Offer:
    return Offer(
        provider=provider,
        offer_id=offer_id,
        title=title or f"Product from {provider}",
        source_item_id=source_item_id if source_item_id is not None else f"item-{provider}",
        # Stable duplicate fixtures should differ only in the fields under test.
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_provider_selection_is_immutable_and_canonical():
    selection = ProviderSelection(provider_keys=["etm_ipro", "future_supplier"])

    assert selection.provider_keys == ("etm_ipro", "future_supplier")
    assert selection.fingerprint_value == ("etm_ipro", "future_supplier")
    assert selection.model_dump(mode="json") == {
        "provider_keys": ["etm_ipro", "future_supplier"],
    }
    with pytest.raises(ValidationError):
        selection.provider_keys = ("etm_ipro",)


@pytest.mark.parametrize("keys", [[], ("etm_ipro", "etm_ipro"), ("",), ("Upper",), ([],)])
def test_provider_selection_rejects_empty_duplicate_and_invalid_keys(keys):
    with pytest.raises(ValidationError):
        ProviderSelection(provider_keys=keys)


def test_provider_selection_rejects_unknown_registry_keys():
    selection = ProviderSelection(provider_keys=["vseinstrumenti_openapi"])

    with pytest.raises(ValueError, match="unknown key"):
        selection.validate_registry({"etm_ipro"})


def test_legacy_provider_resolves_as_a_singleton():
    selection = resolve_provider_selection(
        legacy_provider="etm_ipro",
        provider_keys=None,
        default_provider="local_catalog",
        registered_keys={"local_catalog", "etm_ipro"},
    )

    assert selection == ProviderSelection(provider_keys=("etm_ipro",))


def test_legacy_default_does_not_expand_when_registry_grows():
    before = resolve_provider_selection(
        legacy_provider=None,
        provider_keys=None,
        default_provider="etm_ipro",
        registered_keys={"etm_ipro"},
    )
    after = resolve_provider_selection(
        legacy_provider=None,
        provider_keys=None,
        default_provider="etm_ipro",
        registered_keys={"etm_ipro", "lemana_b2b", "vseinstrumenti_openapi"},
    )

    assert before == after == ProviderSelection(provider_keys=("etm_ipro",))


def test_explicit_selection_is_canonical_and_never_expands_to_all():
    selection = resolve_provider_selection(
        legacy_provider=None,
        provider_keys=["vseinstrumenti_openapi", "etm_ipro"],
        default_provider="local_catalog",
        registered_keys={"local_catalog", "etm_ipro", "vseinstrumenti_openapi"},
    )

    assert selection == ProviderSelection(provider_keys=("etm_ipro", "vseinstrumenti_openapi"))


def test_conflicting_legacy_and_explicit_provider_fields_fail_closed():
    with pytest.raises(ValueError, match="conflicts"):
        resolve_provider_selection(
            legacy_provider="etm_ipro",
            provider_keys=["lemana_b2b"],
            default_provider="local_catalog",
            registered_keys={"etm_ipro", "lemana_b2b"},
        )


def test_identical_legacy_and_explicit_singleton_remain_compatible():
    selection = resolve_provider_selection(
        legacy_provider="etm_ipro",
        provider_keys=["etm_ipro"],
        default_provider="local_catalog",
        registered_keys={"etm_ipro", "local_catalog"},
    )

    assert selection == ProviderSelection(provider_keys=("etm_ipro",))


def test_explicit_empty_live_selection_fails_and_history_only_selects_none():
    with pytest.raises(ValueError, match="cannot be empty"):
        resolve_provider_selection(
            legacy_provider=None,
            provider_keys=[],
            default_provider="etm_ipro",
            registered_keys={"etm_ipro"},
        )

    assert resolve_provider_selection(
        legacy_provider="etm_ipro",
        provider_keys=None,
        default_provider="etm_ipro",
        registered_keys={"etm_ipro"},
        live_mode=False,
    ) is None


def test_composite_offer_reference_keeps_equal_supplier_ids_separate():
    etm = _offer("etm_ipro", "same-id")
    vi = _offer("vseinstrumenti_openapi", "same-id")

    assert etm.offer_id == vi.offer_id == "same-id"
    assert ProviderOfferReference.from_offer(etm) != ProviderOfferReference.from_offer(vi)
    indexed = index_offers_by_provider_identity([vi, etm])
    assert len(indexed) == 2
    assert list(indexed) == sorted(indexed, key=lambda ref: ref.ordering_key)
    assert indexed[ProviderOfferReference(provider_key="etm_ipro", offer_id="same-id")] is etm
    assert indexed[ProviderOfferReference(provider_key="vseinstrumenti_openapi", offer_id="same-id")] is vi


def test_composite_reference_serializes_stably_without_rewriting_offer_id():
    offer = _offer("etm_ipro", "etm_ipro:sku-001")
    reference = ProviderOfferReference.from_offer(offer)

    assert reference.model_dump(mode="json") == {
        "provider_key": "etm_ipro",
        "offer_id": "etm_ipro:sku-001",
    }
    assert offer.offer_id == "etm_ipro:sku-001"
    assert ProviderOfferReference.model_validate(reference.model_dump()) == reference


def test_same_provider_and_offer_id_is_one_composite_reference():
    first = ProviderOfferReference.from_offer(_offer("etm_ipro", "shared"))
    second = ProviderOfferReference.from_offer(_offer("etm_ipro", "shared"))

    assert first == second
    assert len({first, second}) == 1


def test_offer_index_is_idempotent_for_identical_identity_and_rejects_conflict():
    first = _offer("etm_ipro", "shared")
    equal = _offer("etm_ipro", "shared")
    conflict = _offer("etm_ipro", "shared", title="Different item")

    assert len(index_offers_by_provider_identity([first, equal])) == 1
    with pytest.raises(ValueError, match="conflicting offers"):
        index_offers_by_provider_identity([first, conflict])


def test_provider_reference_bounds_both_identity_parts():
    with pytest.raises(ValidationError):
        ProviderOfferReference(provider_key="", offer_id="sku")
    with pytest.raises(ValidationError):
        ProviderOfferReference(provider_key="etm_ipro", offer_id="x" * 181)


def test_successful_provider_outcome_contains_only_normalized_offers():
    outcome = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=(_offer("etm_ipro", "one"),),
        request_count=2,
        catalog_version="catalog-rev-1",
        timings=({"stage": "search", "milliseconds": 42.5},),
    )

    assert outcome.attempted is True
    assert outcome.suppressed is False
    assert outcome.offers[0].provider == "etm_ipro"
    assert outcome.catalog_version == "catalog-rev-1"
    assert "match" not in outcome.model_dump(mode="json")


def test_outcome_distinguishes_empty_failure_suppressed_and_not_attempted():
    empty = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.EMPTY,
        request_count=1,
    )
    failure = ProviderSearchOutcome(
        provider_key="vseinstrumenti_openapi",
        state=ProviderSearchState.FAILURE,
        failure_category="timeout",
        request_count=1,
    )
    suppressed = ProviderSearchOutcome(
        provider_key="vseinstrumenti_openapi",
        state=ProviderSearchState.SUPPRESSED,
        failure_category="authentication",
    )
    skipped = ProviderSearchOutcome(
        provider_key="vseinstrumenti_openapi",
        state=ProviderSearchState.NOT_ATTEMPTED,
    )

    assert empty.attempted and not empty.offers
    assert failure.attempted and failure.failure_category == "timeout"
    assert suppressed.suppressed and not suppressed.attempted
    assert not skipped.suppressed and not skipped.attempted


def test_request_count_is_outbound_traffic_and_may_be_zero_for_local_execution():
    local_offer = _offer("local_catalog", "local-1")
    local_success = ProviderSearchOutcome(
        provider_key="local_catalog",
        state=ProviderSearchState.SUCCESS,
        offers=(local_offer,),
        request_count=0,
    )
    local_failure = ProviderSearchOutcome(
        provider_key="vseinstrumenti_openapi",
        state=ProviderSearchState.FAILURE,
        failure_category="misconfigured",
        request_count=0,
    )
    network_success = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=(_offer("etm_ipro", "network-1"),),
        request_count=2,
    )

    assert local_success.attempted and local_success.request_count == 0
    assert local_failure.attempted and local_failure.request_count == 0
    assert network_success.request_count == 2

    assert ProviderRunSummaryItem(
        provider_key="local_catalog",
        state="success",
        request_count=0,
        offers_returned=1,
    ).request_count == 0
    assert ProviderRunSummaryItem(
        provider_key="vseinstrumenti_openapi",
        state="failure",
        failure_category="misconfigured",
        request_count=0,
    ).request_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"provider_key": "etm_ipro", "state": "success", "request_count": 1},
        {"provider_key": "etm_ipro", "state": "empty", "request_count": 0, "failure_category": "timeout"},
        {"provider_key": "etm_ipro", "state": "failure", "request_count": 1},
        {"provider_key": "etm_ipro", "state": "suppressed", "offers": [_offer("etm_ipro", "one")]},
        {"provider_key": "etm_ipro", "state": "not_attempted", "request_count": 1},
    ],
)
def test_provider_outcome_rejects_inconsistent_states(payload):
    with pytest.raises(ValidationError):
        ProviderSearchOutcome.model_validate(payload)


def test_provider_outcome_cannot_contain_another_providers_offers():
    with pytest.raises(ValidationError, match="only offers from that provider"):
        ProviderSearchOutcome(
            provider_key="etm_ipro",
            state="success",
            offers=(_offer("vseinstrumenti_openapi", "foreign-id"),),
            request_count=1,
        )


def test_provider_outcome_rejects_arbitrary_messages_and_bounds_fields():
    with pytest.raises(ValidationError):
        ProviderSearchOutcome(
            provider_key="etm_ipro",
            state="failure",
            failure_category="transport",
            request_count=1,
            public_message="Bearer secret must never be returned",
        )
    with pytest.raises(ValidationError):
        ProviderSearchOutcome(
            provider_key="etm_ipro",
            state="failure",
            failure_category="UPSTREAM_ERROR",
            request_count=1,
        )
    with pytest.raises(ValidationError):
        ProviderSearchOutcome(
            provider_key="etm_ipro",
            state="success",
            offers=tuple(_offer("etm_ipro", str(index)) for index in range(101)),
            request_count=1,
        )
    with pytest.raises(ValidationError):
        ProviderSearchOutcome(
            provider_key="etm_ipro",
            state="success",
            offers=(_offer("etm_ipro", "one"),),
            request_count=1,
            timings=tuple({"stage": f"stage_{index}", "milliseconds": 1} for index in range(9)),
        )


def test_provider_outcome_allows_partial_provider_success_without_match_claims():
    outcome = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state="partial_success",
        offers=(_offer("etm_ipro", "usable"),),
        failure_category="timeout",
        request_count=3,
    )

    assert outcome.attempted
    assert len(outcome.offers) == 1
    assert "decision" not in outcome.model_dump(mode="json")


def test_provider_execution_summary_preserves_success_when_peer_fails():
    summary = ProviderExecutionSummary(providers=(
        ProviderRunSummaryItem(
            provider_key="vseinstrumenti_openapi",
            state=ProviderRunState.FAILURE,
            failure_category="timeout",
            request_count=1,
        ),
        ProviderRunSummaryItem(
            provider_key="etm_ipro",
            state=ProviderRunState.SUCCESS,
            request_count=4,
            offers_returned=3,
        ),
    ))

    assert summary.partial_failure
    assert [item.provider_key for item in summary.providers] == ["etm_ipro", "vseinstrumenti_openapi"]
    assert summary.providers[0].offers_returned == 3


def test_provider_execution_summary_rejects_duplicate_keys_and_invalid_counts():
    item = ProviderRunSummaryItem(provider_key="etm_ipro", state="empty", request_count=1)
    with pytest.raises(ValidationError):
        ProviderExecutionSummary(providers=(item, item))
    with pytest.raises(ValidationError):
        ProviderRunSummaryItem(provider_key="etm_ipro", state="success", request_count=1)
    with pytest.raises(ValidationError):
        ProviderRunSummaryItem(provider_key="etm_ipro", state="failure", request_count=1)

    with pytest.raises(ValidationError):
        ProviderExecutionSummary(providers=({"provider_key": [], "state": "empty"},))

    too_many_offers = (
        ProviderRunSummaryItem(
            provider_key="etm_ipro",
            state="success",
            request_count=1,
            offers_returned=300_000,
        ),
        ProviderRunSummaryItem(
            provider_key="vseinstrumenti_openapi",
            state="success",
            request_count=1,
            offers_returned=300_000,
        ),
    )
    with pytest.raises(ValidationError, match="total offer bound"):
        ProviderExecutionSummary(providers=too_many_offers)


@pytest.mark.parametrize(
    ("state", "offers_returned", "failure_category"),
    [
        (ProviderRunState.NOT_ATTEMPTED, 1, None),
        (ProviderRunState.SUPPRESSED, 1, "timeout"),
        (ProviderRunState.EMPTY, 1, None),
        (ProviderRunState.FAILURE, 1, "timeout"),
        (ProviderRunState.SUCCESS, 0, None),
        (ProviderRunState.PARTIAL_SUCCESS, 0, "timeout"),
    ],
)
def test_provider_run_summary_enforces_offer_count_for_each_state(
    state,
    offers_returned,
    failure_category,
):
    with pytest.raises(ValidationError):
        ProviderRunSummaryItem(
            provider_key="etm_ipro",
            state=state,
            request_count=0,
            offers_returned=offers_returned,
            failure_category=failure_category,
        )


@pytest.mark.parametrize("state", [ProviderRunState.SUCCESS, ProviderRunState.EMPTY])
def test_success_and_empty_summary_states_cannot_carry_failure_category(state):
    kwargs = {"offers_returned": 1} if state == ProviderRunState.SUCCESS else {}
    with pytest.raises(ValidationError):
        ProviderRunSummaryItem(
            provider_key="etm_ipro",
            state=state,
            request_count=0,
            failure_category="timeout",
            **kwargs,
        )


def test_commercial_evidence_stays_unverified_until_each_basis_is_supplied():
    unknown = CommercialEvidenceContract(
        offer_reference=ProviderOfferReference(
            provider_key="vseinstrumenti_openapi",
            offer_id="vi-offer-unknown",
        ),
        provider_key="vseinstrumenti_openapi",
        source_item_id="sku-001",
        price_field="prices.price",
        price_basis=PriceBasis.UNKNOWN,
        currency=None,
        unit="шт",
        unit_basis="response.unit",
        tax_basis_required=True,
        tax_basis=TaxBasis.UNKNOWN,
        environment="prod",
        region_id="region-fixture",
    )
    complete = CommercialEvidenceContract(
        offer_reference=ProviderOfferReference(provider_key="fixture_supplier", offer_id="offer-1"),
        provider_key="fixture_supplier",
        source_item_id="item-1",
        price_field="prices.total",
        price_basis=PriceBasis.GROSS_INCLUDING_VAT,
        currency="RUB",
        currency_basis="supplier-approved contract revision 1",
        unit="шт",
        unit_basis="response.unit",
        tax_basis=TaxBasis.INCLUDED,
        affinity_required=True,
        environment="prod",
        region_id="region-1",
        config_revision="settings-1",
    )

    assert not unknown.has_complete_commercial_basis
    assert complete.has_complete_commercial_basis
    assert complete.tax_basis_required

    missing_required_affinity = CommercialEvidenceContract(
        offer_reference=ProviderOfferReference(provider_key="fixture_supplier", offer_id="offer-2"),
        provider_key="fixture_supplier",
        source_item_id="item-2",
        price_field="prices.total",
        price_basis=PriceBasis.NET,
        currency="RUB",
        currency_basis="contract revision 1",
        unit="шт",
        unit_basis="response.unit",
        tax_basis_required=False,
        affinity_required=True,
    )
    assert not missing_required_affinity.has_complete_commercial_basis


def test_commercial_evidence_never_defaults_currency_unit_or_tax_basis():
    evidence = CommercialEvidenceContract(
        offer_reference=ProviderOfferReference(
            provider_key="vseinstrumenti_openapi",
            offer_id="vi-offer-2",
        ),
        provider_key="vseinstrumenti_openapi",
        source_item_id="sku-001",
        price_field="prices.price",
    )

    assert evidence.currency is None
    assert evidence.unit is None
    assert evidence.tax_basis is None
    assert not evidence.has_complete_commercial_basis
    with pytest.raises(ValidationError):
        CommercialEvidenceContract(
            offer_reference=ProviderOfferReference(provider_key="fixture_supplier", offer_id="offer-3"),
            provider_key="fixture_supplier",
            source_item_id="item-1",
            currency="руб",
        )


def test_commercial_evidence_is_bound_to_the_exact_offer():
    source_offer = _offer("fixture_supplier", "offer-1", source_item_id="source-1")
    another_offer_same_item = _offer("fixture_supplier", "offer-2", source_item_id="source-1")
    same_offer_id_other_provider = _offer("vseinstrumenti_openapi", "offer-1", source_item_id="source-1")

    class FixtureEvidenceProvider:
        def commercial_evidence(self, offer: Offer) -> CommercialEvidenceContract:
            return CommercialEvidenceContract(
                offer_reference=ProviderOfferReference.from_offer(offer),
                provider_key=offer.provider,
                source_item_id=offer.source_item_id,
            )

    evidence = FixtureEvidenceProvider().commercial_evidence(source_offer)
    other_provider_evidence = CommercialEvidenceContract(
        offer_reference=ProviderOfferReference.from_offer(same_offer_id_other_provider),
        provider_key=same_offer_id_other_provider.provider,
        source_item_id=same_offer_id_other_provider.source_item_id,
    )

    assert evidence.is_for_offer(source_offer)
    assert not evidence.is_for_offer(another_offer_same_item)
    assert not evidence.is_for_offer(same_offer_id_other_provider)
    assert evidence.offer_reference != other_provider_evidence.offer_reference
    assert other_provider_evidence.is_for_offer(same_offer_id_other_provider)


def test_commercial_evidence_rejects_provider_mismatch_with_offer_reference():
    with pytest.raises(ValidationError, match="provider must match its offer reference"):
        CommercialEvidenceContract(
            offer_reference=ProviderOfferReference(provider_key="etm_ipro", offer_id="shared-id"),
            provider_key="vseinstrumenti_openapi",
            source_item_id="shared-source-id",
        )


def test_health_snapshot_is_separate_from_explicit_connectivity_probe():
    snapshot = ProviderHealthSnapshot(
        configured=True,
        status=ProviderHealthStatus.UNKNOWN,
    )

    assert snapshot.status == ProviderHealthStatus.UNKNOWN
    assert snapshot.catalog_version is None
    assert "probe" not in snapshot.model_dump(mode="json")


@pytest.mark.parametrize(
    ("configured", "status", "failure_category"),
    [
        (False, ProviderHealthStatus.REACHABLE, None),
        (True, ProviderHealthStatus.NOT_CONFIGURED, None),
        (True, ProviderHealthStatus.REACHABLE, "timeout"),
    ],
)
def test_health_snapshot_rejects_contradictory_configuration_and_status(
    configured,
    status,
    failure_category,
):
    with pytest.raises(ValidationError):
        ProviderHealthSnapshot(
            configured=configured,
            status=status,
            failure_category=failure_category,
        )


def test_unconfigured_health_status_is_consistent():
    snapshot = ProviderHealthSnapshot(
        configured=False,
        status=ProviderHealthStatus.NOT_CONFIGURED,
    )

    assert snapshot.status == ProviderHealthStatus.NOT_CONFIGURED


def test_request_identity_deduplicates_only_inside_matching_execution_scope():
    fingerprint = hashlib.sha256(b"planned-equivalent-provider-request").hexdigest()
    common = {
        "execution_scope_id": "run-local-scope-1",
        "provider_key": "vseinstrumenti_openapi",
        "request_fingerprint": fingerprint,
        "affinity": ProviderAffinity(
            environment="test",
            region_id="region-fixture",
            config_revision="settings-1",
            adapter_revision="adapter-1",
        ),
        "limit": 20,
    }
    same_request = ProviderSearchRequestIdentity(**common)
    duplicate = ProviderSearchRequestIdentity(**common)
    next_run = ProviderSearchRequestIdentity(**{**common, "execution_scope_id": "run-local-scope-2"})
    other_region = ProviderSearchRequestIdentity.model_validate({
        **common,
        "affinity": {**common["affinity"].model_dump(), "region_id": "another-region"},
    })

    assert same_request.equality_key == duplicate.equality_key
    assert same_request.equality_key != next_run.equality_key
    assert same_request.equality_key != other_region.equality_key


def test_request_identity_rejects_unbounded_or_invalid_fingerprints():
    with pytest.raises(ValidationError):
        ProviderSearchRequestIdentity(
            execution_scope_id="scope",
            provider_key="etm_ipro",
            request_fingerprint="not-a-digest",
            limit=20,
        )
    with pytest.raises(ValidationError):
        ProviderSearchRequestIdentity(
            execution_scope_id="scope",
            provider_key="etm_ipro",
            request_fingerprint="a" * 64,
            limit=101,
        )


def test_existing_schema_and_health_contract_limits_are_unchanged():
    assert ProviderHealthStatus.UNKNOWN.value == "unknown"
    assert MAX_TENDER_RUN_BYTES == 1024 * 1024
    assert MAX_TENDER_RUNS_PER_WORKSPACE == 5
    assert MAX_TENDER_STORAGE_BYTES == 500 * 1024 * 1024
    assert MAX_HISTORY_DECISION_EVENTS_PER_RUN == 1000
    assert MAX_HISTORY_DECISION_LEDGER_BYTES == 2 * 1024 * 1024
