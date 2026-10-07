"""Inactive, request-local multi-provider execution engine.

Only outcome-native adapters can run here. Legacy ``SourcingProvider``
implementations are intentionally not adapted because their HTTP attempt counts
are not observable through the legacy interface.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Protocol

from pydantic import Field, PrivateAttr, StrictInt, StrictStr, model_validator

from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.providers.contracts import (
    MAX_PROVIDER_OUTCOME_OFFERS,
    MAX_PROVIDER_REQUESTS,
    MAX_PROVIDER_SELECTION_SIZE,
    ProviderAffinity,
    ProviderContractModel,
    ProviderExecutionSummary,
    ProviderFailureCategory,
    ProviderRunState,
    ProviderRunSummaryItem,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
    ProviderSelection,
    index_offers_by_provider_identity,
)


# Request-local safety cap: a five-provider run may not materialize 500 offers
# at once. This is below the existing M0 per-provider bounds and never truncates.
MAX_PROVIDER_EXECUTION_OFFERS = 400
MAX_PROVIDER_EXECUTION_DEDUP_ENTRIES = 4096
MAX_PROVIDER_EXECUTION_SCOPE_LENGTH = 80


class ProviderRunnerConfigurationError(ValueError):
    """The explicit selection or execution-capable registry is invalid."""


class ProviderExecutionLimitError(RuntimeError):
    """The complete result exceeded a runner bound and was not truncated."""

    code = "provider_execution_limit_exceeded"

    def __init__(self) -> None:
        super().__init__("provider execution exceeded a bounded result limit")


class ProviderRequestLimitExceeded(RuntimeError):
    """An adapter tried to exceed the per-provider outbound request bound."""

    def __init__(self) -> None:
        super().__init__("provider request count exceeded its bound")


class ProviderRequestCounter:
    """Adapter-owned accounting for actual outbound provider attempts.

    The adapter must call ``record_outbound_attempt`` exactly once immediately
    before each outbound provider request. Local work must not increment it.
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    @property
    def request_count(self) -> int:
        return self._count

    def record_outbound_attempt(self) -> None:
        if self._count >= MAX_PROVIDER_REQUESTS:
            raise ProviderRequestLimitExceeded()
        self._count += 1


class ProviderExecutionScope(ProviderContractModel):
    """Ephemeral in-memory cache owned by one explicitly identified run."""

    execution_scope_id: Annotated[
        StrictStr,
        Field(min_length=1, max_length=MAX_PROVIDER_EXECUTION_SCOPE_LENGTH),
    ]
    _outcome_cache: dict[tuple[object, ...], ProviderSearchOutcome] = PrivateAttr(
        default_factory=dict,
    )

    def _cached_outcome(
        self,
        identity: ProviderSearchRequestIdentity,
    ) -> ProviderSearchOutcome | None:
        outcome = self._outcome_cache.get(identity.equality_key)
        return outcome.model_copy(deep=True) if outcome is not None else None

    def _remember_outcome(
        self,
        identity: ProviderSearchRequestIdentity,
        outcome: ProviderSearchOutcome,
    ) -> None:
        key = identity.equality_key
        if key in self._outcome_cache or len(self._outcome_cache) < MAX_PROVIDER_EXECUTION_DEDUP_ENTRIES:
            self._outcome_cache[key] = outcome.model_copy(deep=True)


class OutcomeNativeProviderAdapter(Protocol):
    """Execution-only adapter with explicit, truthful outbound accounting.

    ``request_identity`` must be a pure local operation and fingerprint every
    search input. ``execute_search`` returns normalized offers and a
    ``request_count`` equal to the supplied counter's actual attempt count.
    """

    key: str

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        ...

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        ...


class ProviderExecutionResult(ProviderContractModel):
    """Bounded, request-local retrieval result; contains no match decisions."""

    selection: ProviderSelection
    result_limit: Annotated[StrictInt, Field(ge=1, le=MAX_PROVIDER_OUTCOME_OFFERS)]
    outcomes: tuple[ProviderSearchOutcome, ...] = Field(
        min_length=1,
        max_length=MAX_PROVIDER_SELECTION_SIZE,
    )
    offers: tuple[Offer, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    reused_provider_keys: tuple[Annotated[
        StrictStr,
        Field(min_length=1, max_length=100),
    ], ...] = Field(max_length=MAX_PROVIDER_SELECTION_SIZE)

    @model_validator(mode="after")
    def _validate_deterministic_projection(self) -> ProviderExecutionResult:
        outcome_keys = tuple(outcome.provider_key for outcome in self.outcomes)
        if outcome_keys != self.selection.provider_keys:
            raise ValueError("provider outcomes must follow the exact selected provider order")

        reused_set = set(self.reused_provider_keys)
        expected_reused = tuple(key for key in self.selection.provider_keys if key in reused_set)
        if expected_reused != self.reused_provider_keys:
            raise ValueError("reused providers must be unique and follow selection order")
        if any(len(outcome.offers) > self.result_limit for outcome in self.outcomes):
            raise ValueError("provider outcomes exceed the requested result limit")

        indexed = index_offers_by_provider_identity(
            offer for outcome in self.outcomes for offer in outcome.offers
        )
        if len(indexed) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise ValueError("merged provider offers exceed the aggregate execution bound")
        if tuple(indexed.values()) != self.offers:
            raise ValueError("merged offers must be unique and sorted by composite identity")
        return self

    @property
    def execution_summary(self) -> ProviderExecutionSummary:
        return ProviderExecutionSummary(
            providers=tuple(
                ProviderRunSummaryItem(
                    provider_key=outcome.provider_key,
                    state=ProviderRunState(outcome.state.value),
                    request_count=outcome.request_count,
                    offers_returned=len(outcome.offers),
                    failure_category=outcome.failure_category,
                    catalog_version=outcome.catalog_version,
                    affinity=outcome.affinity,
                    elapsed_milliseconds=(
                        sum(timing.milliseconds for timing in outcome.timings)
                        if outcome.timings
                        else None
                    ),
                )
                for outcome in self.outcomes
            ),
        )

    @property
    def partial_failure(self) -> bool:
        return self.execution_summary.partial_failure

    @property
    def total_request_count(self) -> int:
        return sum(outcome.request_count for outcome in self.outcomes)


class ProviderRunner:
    """Sequential runner for an explicit, outcome-native provider registry."""

    def __init__(self, registry: Mapping[str, OutcomeNativeProviderAdapter]) -> None:
        if not isinstance(registry, Mapping):
            raise ProviderRunnerConfigurationError("provider registry must be an explicit mapping")

        copied: dict[str, OutcomeNativeProviderAdapter] = {}
        for key, adapter in registry.items():
            if not isinstance(key, str):
                raise ProviderRunnerConfigurationError("provider registry keys must be valid provider keys")
            try:
                ProviderSelection(provider_keys=(key,))
            except Exception:
                raise ProviderRunnerConfigurationError(
                    "provider registry keys must be valid provider keys",
                ) from None
            if not callable(getattr(adapter, "request_identity", None)) or not callable(
                getattr(adapter, "execute_search", None)
            ):
                raise ProviderRunnerConfigurationError(
                    "registry adapters must implement the outcome-native execution protocol"
                )
            if getattr(adapter, "key", None) != key:
                raise ProviderRunnerConfigurationError(
                    "registry key must match its execution adapter key"
                )
            copied[key] = adapter
        self._registry = MappingProxyType(copied)

    def run(
        self,
        *,
        selection: ProviderSelection,
        intent: ProductIntent,
        limit: int,
        scope: ProviderExecutionScope,
    ) -> ProviderExecutionResult:
        if not isinstance(selection, ProviderSelection):
            raise ProviderRunnerConfigurationError("selection must be a ProviderSelection")
        if not isinstance(intent, ProductIntent):
            raise ProviderRunnerConfigurationError("intent must be a ProductIntent")
        if not isinstance(scope, ProviderExecutionScope):
            raise ProviderRunnerConfigurationError("scope must be a ProviderExecutionScope")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ProviderRunnerConfigurationError("result limit must be between 1 and 100")
        selection.validate_registry(self._registry)

        outcomes: list[ProviderSearchOutcome] = []
        reused_provider_keys: list[str] = []
        for provider_key in selection.provider_keys:
            outcome, reused = self._run_provider(
                provider_key=provider_key,
                adapter=self._registry[provider_key],
                intent=intent,
                limit=limit,
                scope=scope,
            )
            outcomes.append(outcome)
            if reused:
                reused_provider_keys.append(provider_key)

        merged = index_offers_by_provider_identity(
            offer for outcome in outcomes for offer in outcome.offers
        )
        if len(merged) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise ProviderExecutionLimitError()

        return ProviderExecutionResult(
            selection=selection,
            result_limit=limit,
            outcomes=tuple(outcomes),
            offers=tuple(merged.values()),
            reused_provider_keys=tuple(reused_provider_keys),
        )

    def _run_provider(
        self,
        *,
        provider_key: str,
        adapter: OutcomeNativeProviderAdapter,
        intent: ProductIntent,
        limit: int,
        scope: ProviderExecutionScope,
    ) -> tuple[ProviderSearchOutcome, bool]:
        try:
            raw_identity = adapter.request_identity(
                intent,
                limit=limit,
                execution_scope_id=scope.execution_scope_id,
            )
        except Exception:
            return self._failure(
                provider_key,
                ProviderFailureCategory.UNKNOWN,
                request_count=0,
            ), False

        if not isinstance(raw_identity, ProviderSearchRequestIdentity):
            return self._failure(
                provider_key,
                ProviderFailureCategory.INVALID_RESPONSE,
                request_count=0,
            ), False

        try:
            identity = ProviderSearchRequestIdentity.model_validate(
                raw_identity.model_dump(mode="python"),
            )
        except Exception:
            return self._failure(
                provider_key,
                ProviderFailureCategory.INVALID_RESPONSE,
                request_count=0,
            ), False

        if (
            identity.provider_key != provider_key
            or identity.execution_scope_id != scope.execution_scope_id
            or identity.limit != limit
        ):
            return self._failure(
                provider_key,
                ProviderFailureCategory.MISCONFIGURED,
                request_count=0,
                affinity=identity.affinity,
            ), False

        cached = scope._cached_outcome(identity)
        if cached is not None:
            payload = cached.model_dump(mode="python")
            payload["request_count"] = 0
            payload["timings"] = ()
            return ProviderSearchOutcome.model_validate(payload), True

        request_counter = ProviderRequestCounter()
        try:
            raw_outcome = adapter.execute_search(
                intent,
                limit=limit,
                request_counter=request_counter,
            )
        except Exception:
            outcome = self._failure(
                provider_key,
                ProviderFailureCategory.UNKNOWN,
                request_count=request_counter.request_count,
                affinity=identity.affinity,
            )
            scope._remember_outcome(identity, outcome)
            return outcome, False

        try:
            if not isinstance(raw_outcome, ProviderSearchOutcome):
                raise TypeError("adapter returned a non-contract provider outcome")
            outcome = ProviderSearchOutcome.model_validate(
                raw_outcome.model_dump(mode="python"),
            )
        except Exception:
            outcome = self._failure(
                provider_key,
                ProviderFailureCategory.INVALID_RESPONSE,
                request_count=request_counter.request_count,
                affinity=identity.affinity,
            )
            scope._remember_outcome(identity, outcome)
            return outcome, False

        if (
            outcome.provider_key != provider_key
            or outcome.request_count != request_counter.request_count
            or outcome.affinity != identity.affinity
            or len(outcome.offers) > limit
        ):
            outcome = self._failure(
                provider_key,
                ProviderFailureCategory.INVALID_RESPONSE,
                request_count=request_counter.request_count,
                affinity=identity.affinity,
            )
            scope._remember_outcome(identity, outcome)
            return outcome, False

        try:
            index_offers_by_provider_identity(outcome.offers)
        except ValueError:
            outcome = self._failure(
                provider_key,
                ProviderFailureCategory.INVALID_RESPONSE,
                request_count=request_counter.request_count,
                affinity=identity.affinity,
                catalog_version=outcome.catalog_version,
            )

        scope._remember_outcome(identity, outcome)
        return outcome, False

    @staticmethod
    def _failure(
        provider_key: str,
        category: ProviderFailureCategory,
        *,
        request_count: int,
        affinity: ProviderAffinity | None = None,
        catalog_version: str | None = None,
    ) -> ProviderSearchOutcome:
        return ProviderSearchOutcome(
            provider_key=provider_key,
            state=ProviderSearchState.FAILURE,
            failure_category=category,
            request_count=request_count,
            affinity=affinity or ProviderAffinity(),
            catalog_version=catalog_version,
        )
