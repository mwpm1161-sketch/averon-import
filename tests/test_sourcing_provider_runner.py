from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from averon_import.services.sourcing.models import MatchDecision, Offer, ProductIntent
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderSearchOutcome,
    ProviderSearchState,
    ProviderSearchRequestIdentity,
    ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import (
    ProviderExecutionLimitError,
    ProviderExecutionScope,
    ProviderRequestCounter,
    ProviderRunner,
    ProviderRunnerConfigurationError,
)


def _offer(provider: str, offer_id: str, *, title: str | None = None) -> Offer:
    return Offer(
        provider=provider,
        offer_id=offer_id,
        source_item_id=f"item-{provider}-{offer_id}",
        title=title or f"{provider} {offer_id}",
    )


def _intent(*, source_text: str = "valve") -> ProductIntent:
    return ProductIntent(source_row_id="row-1", source_text=source_text)


class FakeOutcomeAdapter:
    """In-memory adapter whose counter represents its configured attempts."""

    def __init__(
        self,
        key: str,
        offers: tuple[Offer, ...] = (),
        *,
        state: ProviderSearchState | None = None,
        attempts: int = 1,
        failure_category: ProviderFailureCategory | None = None,
        affinity: ProviderAffinity | None = None,
        error: Exception | None = None,
        raw_outcome: object | None = None,
    ) -> None:
        self.key = key
        self.offers = offers
        self.state = state or (ProviderSearchState.SUCCESS if offers else ProviderSearchState.EMPTY)
        self.attempts = attempts
        self.failure_category = failure_category
        self.affinity = affinity or ProviderAffinity(
            environment="test",
            region_id="north",
            config_revision="cfg-1",
            adapter_revision="adapter-1",
        )
        self.error = error
        self.raw_outcome = raw_outcome
        self.execute_calls = 0
        self.health_calls = 0

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        fingerprint = hashlib.sha256(
            f"{intent.fingerprint}:{limit}".encode("utf-8"),
        ).hexdigest()
        return ProviderSearchRequestIdentity(
            execution_scope_id=execution_scope_id,
            provider_key=self.key,
            request_fingerprint=fingerprint,
            affinity=self.affinity,
            limit=limit,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        self.execute_calls += 1
        for _ in range(self.attempts):
            request_counter.record_outbound_attempt()
        if self.error is not None:
            raise self.error
        if self.raw_outcome is not None:
            return self.raw_outcome  # type: ignore[return-value]
        offers = self.offers[:limit]
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=self.state,
            offers=offers,
            request_count=request_counter.request_count,
            failure_category=self.failure_category,
            affinity=self.affinity,
            catalog_version=f"{self.key}-catalog-7",
        )

    def stats(self):
        self.health_calls += 1
        raise AssertionError("ProviderRunner must not call stats")

    def local_status(self):
        self.health_calls += 1
        raise AssertionError("ProviderRunner must not call local status")

    def probe(self):
        self.health_calls += 1
        raise AssertionError("ProviderRunner must not call connectivity probes")


def _run(
    registry: dict[str, object],
    keys: tuple[str, ...],
    *,
    scope: ProviderExecutionScope | None = None,
    intent: ProductIntent | None = None,
    limit: int = 10,
):
    return ProviderRunner(registry).run(
        selection=ProviderSelection(provider_keys=keys),
        intent=intent or _intent(),
        limit=limit,
        scope=scope or ProviderExecutionScope(execution_scope_id="run-1"),
    )


def test_two_provider_success_is_deterministic_and_uses_composite_identity():
    first = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "same"),))
    second = FakeOutcomeAdapter("vseinstrumenti_openapi", (_offer("vseinstrumenti_openapi", "same"),))

    result = _run(
        {"vseinstrumenti_openapi": second, "etm_ipro": first},
        ("vseinstrumenti_openapi", "etm_ipro"),
    )

    assert result.selection.provider_keys == ("etm_ipro", "vseinstrumenti_openapi")
    assert tuple(item.provider_key for item in result.outcomes) == result.selection.provider_keys
    assert tuple((offer.provider, offer.offer_id) for offer in result.offers) == (
        ("etm_ipro", "same"),
        ("vseinstrumenti_openapi", "same"),
    )
    assert result.total_request_count == 2
    assert result.result_limit == 10


def test_conflicting_duplicate_identity_fails_only_that_provider_closed():
    conflicting = FakeOutcomeAdapter(
        "etm_ipro",
        (_offer("etm_ipro", "same", title="one"), _offer("etm_ipro", "same", title="two")),
    )
    peer = FakeOutcomeAdapter("supplier", (_offer("supplier", "kept"),))

    result = _run({"etm_ipro": conflicting, "supplier": peer}, ("etm_ipro", "supplier"))

    assert result.outcomes[0].state == ProviderSearchState.FAILURE
    assert result.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert result.outcomes[0].offers == ()
    assert tuple(offer.offer_id for offer in result.offers) == ("kept",)


def test_success_and_timeout_failure_are_isolated_with_actual_attempt_counts():
    good = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "usable"),), attempts=2)
    timed_out = FakeOutcomeAdapter(
        "supplier",
        state=ProviderSearchState.FAILURE,
        attempts=3,
        failure_category=ProviderFailureCategory.TIMEOUT,
    )

    result = _run({"etm_ipro": good, "supplier": timed_out}, ("etm_ipro", "supplier"))

    assert tuple(offer.offer_id for offer in result.offers) == ("usable",)
    assert tuple(outcome.request_count for outcome in result.outcomes) == (2, 3)
    assert result.outcomes[1].failure_category == ProviderFailureCategory.TIMEOUT
    assert result.partial_failure is True


def test_empty_and_failure_are_both_preserved_and_not_marketwide_no_result():
    empty = FakeOutcomeAdapter("etm_ipro", state=ProviderSearchState.EMPTY, attempts=1)
    failed = FakeOutcomeAdapter(
        "supplier",
        state=ProviderSearchState.FAILURE,
        attempts=1,
        failure_category=ProviderFailureCategory.UNAVAILABLE,
    )

    result = _run({"supplier": failed, "etm_ipro": empty}, ("supplier", "etm_ipro"))

    assert tuple(outcome.state for outcome in result.outcomes) == (
        ProviderSearchState.EMPTY,
        ProviderSearchState.FAILURE,
    )
    assert result.offers == ()
    assert result.partial_failure is True
    assert result.execution_summary.providers[0].state.value == "empty"


def test_partial_success_keeps_offers_failure_evidence_catalog_and_affinity():
    affinity = ProviderAffinity(
        environment="sandbox",
        region_id="south",
        config_revision="cfg-4",
        adapter_revision="adapter-9",
    )
    partial = FakeOutcomeAdapter(
        "etm_ipro",
        (_offer("etm_ipro", "partial-offer"),),
        state=ProviderSearchState.PARTIAL_SUCCESS,
        failure_category=ProviderFailureCategory.RATE_LIMITED,
        affinity=affinity,
    )
    success = FakeOutcomeAdapter("supplier", (_offer("supplier", "success-offer"),))

    result = _run({"supplier": success, "etm_ipro": partial}, ("supplier", "etm_ipro"))

    assert {offer.offer_id for offer in result.offers} == {"partial-offer", "success-offer"}
    assert result.outcomes[0].failure_category == ProviderFailureCategory.RATE_LIMITED
    assert result.outcomes[0].affinity == affinity
    assert result.outcomes[0].catalog_version == "etm_ipro-catalog-7"


def test_adapter_exception_is_safe_bounded_and_preserves_counter_without_leakage():
    secret_message = "Authorization: Bearer secret-token upstream-body"
    broken = FakeOutcomeAdapter("etm_ipro", attempts=2, error=RuntimeError(secret_message))
    peer = FakeOutcomeAdapter("supplier", (_offer("supplier", "ok"),))

    result = _run({"etm_ipro": broken, "supplier": peer}, ("etm_ipro", "supplier"))
    failed = result.outcomes[0]

    assert failed.state == ProviderSearchState.FAILURE
    assert failed.failure_category == ProviderFailureCategory.UNKNOWN
    assert failed.request_count == 2
    assert secret_message not in repr(result.model_dump(mode="json"))
    assert tuple(offer.offer_id for offer in result.offers) == ("ok",)


def test_registry_insertion_order_cannot_change_provider_or_offer_order():
    a = FakeOutcomeAdapter("a", (_offer("a", "z"), _offer("a", "a")))
    b = FakeOutcomeAdapter("b", (_offer("b", "b"),))
    first = _run({"b": b, "a": a}, ("b", "a"))
    second = _run({"a": a, "b": b}, ("a", "b"))

    assert [o.provider_key for o in first.outcomes] == [o.provider_key for o in second.outcomes]
    assert [(o.provider, o.offer_id) for o in first.offers] == [
        (o.provider, o.offer_id) for o in second.offers
    ]


def test_run_local_dedup_reuses_outcome_without_inventing_another_request():
    adapter = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "cached"),), attempts=2)
    runner = ProviderRunner({"etm_ipro": adapter})
    scope = ProviderExecutionScope(execution_scope_id="same-run")
    selection = ProviderSelection(provider_keys=("etm_ipro",))

    first = runner.run(selection=selection, intent=_intent(), limit=10, scope=scope)
    second = runner.run(selection=selection, intent=_intent(), limit=10, scope=scope)

    assert adapter.execute_calls == 1
    assert first.total_request_count == 2
    assert second.total_request_count == 0
    assert second.reused_provider_keys == ("etm_ipro",)
    assert second.offers == first.offers
    assert "_outcome_cache" not in scope.model_dump()


def test_cache_is_owned_by_scope_instance_and_never_crosses_runs():
    adapter = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "fresh"),))
    runner = ProviderRunner({"etm_ipro": adapter})
    selection = ProviderSelection(provider_keys=("etm_ipro",))

    first_scope = ProviderExecutionScope(execution_scope_id="run-a")
    second_scope = ProviderExecutionScope(execution_scope_id="run-b")
    same_label_new_instance = ProviderExecutionScope(execution_scope_id="run-a")
    for scope in (first_scope, second_scope, same_label_new_instance):
        runner.run(selection=selection, intent=_intent(), limit=10, scope=scope)

    assert adapter.execute_calls == 3


def test_dedup_identity_includes_intent_limit_and_affinity_revisions():
    adapter = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "same"),))
    runner = ProviderRunner({"etm_ipro": adapter})
    scope = ProviderExecutionScope(execution_scope_id="one-run")
    selection = ProviderSelection(provider_keys=("etm_ipro",))

    def run(intent: ProductIntent = _intent(), limit: int = 10) -> None:
        runner.run(selection=selection, intent=intent, limit=limit, scope=scope)

    run()
    run(_intent(source_text="different request"))
    run(limit=9)
    adapter.affinity = ProviderAffinity(environment="test", region_id="south", config_revision="cfg-1", adapter_revision="adapter-1")
    run()
    adapter.affinity = ProviderAffinity(environment="test", region_id="south", config_revision="cfg-2", adapter_revision="adapter-1")
    run()
    adapter.affinity = ProviderAffinity(environment="test", region_id="south", config_revision="cfg-2", adapter_revision="adapter-2")
    run()

    assert adapter.execute_calls == 6


def test_outcome_exceeding_requested_limit_fails_closed_and_peer_survives():
    oversized_outcome = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=(_offer("etm_ipro", "1"), _offer("etm_ipro", "2"), _offer("etm_ipro", "3")),
        request_count=1,
        affinity=ProviderAffinity(
            environment="test",
            region_id="north",
            config_revision="cfg-1",
            adapter_revision="adapter-1",
        ),
    )
    too_many_for_request = FakeOutcomeAdapter(
        "etm_ipro",
        raw_outcome=oversized_outcome,
    )
    peer = FakeOutcomeAdapter("supplier", (_offer("supplier", "kept"),))

    result = _run(
        {"etm_ipro": too_many_for_request, "supplier": peer},
        ("etm_ipro", "supplier"),
        limit=2,
    )

    assert result.outcomes[0].state == ProviderSearchState.FAILURE
    assert result.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert tuple(offer.offer_id for offer in result.offers) == ("kept",)


def test_malformed_per_provider_overflow_fails_closed():
    offers = tuple(_offer("etm_ipro", str(index)) for index in range(101))
    invalid = ProviderSearchOutcome.model_construct(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=offers,
        request_count=1,
        failure_category=None,
        timings=(),
        affinity=ProviderAffinity(),
        catalog_version="oversized",
    )
    adapter = FakeOutcomeAdapter("etm_ipro", raw_outcome=invalid)

    result = _run({"etm_ipro": adapter}, ("etm_ipro",), limit=100)

    assert result.outcomes[0].state == ProviderSearchState.FAILURE
    assert result.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE
    assert result.offers == ()


def test_aggregate_overflow_raises_typed_error_without_truncating():
    registry: dict[str, object] = {}
    keys = tuple(f"p{index}" for index in range(5))
    for key in keys:
        registry[key] = FakeOutcomeAdapter(key, tuple(_offer(key, str(i)) for i in range(100)))

    with pytest.raises(ProviderExecutionLimitError) as error:
        _run(registry, keys, limit=100)

    assert error.value.code == "provider_execution_limit_exceeded"
    assert "bounded result limit" in str(error.value)
    assert sum(adapter.execute_calls for adapter in registry.values()) == 5  # type: ignore[union-attr]


def test_request_count_mismatch_is_an_invalid_response_not_guessed():
    wrong = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=(_offer("etm_ipro", "x"),),
        request_count=0,
    )
    adapter = FakeOutcomeAdapter("etm_ipro", raw_outcome=wrong, attempts=1)

    result = _run({"etm_ipro": adapter}, ("etm_ipro",))

    assert result.outcomes[0].state == ProviderSearchState.FAILURE
    assert result.outcomes[0].request_count == 1
    assert result.outcomes[0].failure_category == ProviderFailureCategory.INVALID_RESPONSE


def test_runner_result_has_no_match_decision_and_does_not_call_health_methods():
    adapter = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "retrieved"),))

    result = _run({"etm_ipro": adapter}, ("etm_ipro",))

    assert MatchDecision.__name__ not in result.model_dump_json()
    assert not hasattr(result, "match_results")
    assert adapter.health_calls == 0


def test_legacy_provider_is_not_accepted_as_execution_capable():
    class LegacyProvider:
        key = "etm_ipro"

        def search(self, intent: ProductIntent, limit: int):
            return []

        def stats(self):
            return {}

    with pytest.raises(ProviderRunnerConfigurationError, match="outcome-native"):
        ProviderRunner({"etm_ipro": LegacyProvider()})  # type: ignore[arg-type]


def test_registry_is_snapshotted_at_runner_construction():
    adapter = FakeOutcomeAdapter("etm_ipro", (_offer("etm_ipro", "original"),))
    registry: dict[str, object] = {"etm_ipro": adapter}
    runner = ProviderRunner(registry)  # type: ignore[arg-type]
    registry.clear()

    result = runner.run(
        selection=ProviderSelection(provider_keys=("etm_ipro",)),
        intent=_intent(),
        limit=10,
        scope=ProviderExecutionScope(execution_scope_id="run-1"),
    )

    assert tuple(offer.offer_id for offer in result.offers) == ("original",)


def test_provider_execution_result_rejects_result_over_the_requested_limit():
    outcome = ProviderSearchOutcome(
        provider_key="etm_ipro",
        state=ProviderSearchState.SUCCESS,
        offers=(_offer("etm_ipro", "one"), _offer("etm_ipro", "two")),
        request_count=1,
    )

    with pytest.raises(ValidationError, match="requested result limit"):
        from averon_import.services.sourcing.providers.execution import ProviderExecutionResult

        ProviderExecutionResult(
            selection=ProviderSelection(provider_keys=("etm_ipro",)),
            result_limit=1,
            outcomes=(outcome,),
            offers=outcome.offers,
            reused_provider_keys=(),
        )
