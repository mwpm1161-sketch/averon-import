"""Internal, behaviour-neutral contracts for future multi-provider sourcing.

These DTOs are deliberately not wired into the current search runtime, API,
durable run format, matcher, or export resolver. Search outcomes are
request-local values; durable tender data continues to use its version-1
allowlisted projection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Annotated, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from averon_import.services.sourcing.models import Offer


MAX_PROVIDER_KEY_LENGTH = 100
MAX_PROVIDER_SELECTION_SIZE = 8
MAX_PROVIDER_OFFER_ID_LENGTH = 180
MAX_PROVIDER_OUTCOME_OFFERS = 100
MAX_PROVIDER_RUN_OFFERS = 500_000
MAX_PROVIDER_REQUESTS = 1_000_000
MAX_PROVIDER_TIMING_STAGES = 8
MAX_PROVIDER_AFFINITY_TEXT = 120

_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_TIMING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

ProviderKey = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_PROVIDER_KEY_LENGTH, pattern=_PROVIDER_KEY_RE.pattern),
]
OfferIdentifier = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_PROVIDER_OFFER_ID_LENGTH),
]
class ProviderContractModel(BaseModel):
    """Strict base for immutable internal provider contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSelection(ProviderContractModel):
    """Explicit provider keys in stable lexical order; never an implicit ALL."""

    provider_keys: tuple[ProviderKey, ...] = Field(
        min_length=1,
        max_length=MAX_PROVIDER_SELECTION_SIZE,
    )

    @field_validator("provider_keys", mode="before")
    @classmethod
    def _reject_duplicates_and_canonicalize(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("provider_keys must be a list or tuple")
        if len(value) > MAX_PROVIDER_SELECTION_SIZE:
            raise ValueError("too many providers selected")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("provider keys must be strings")
        if len(value) != len(set(value)):
            raise ValueError("duplicate provider keys are not allowed")
        return tuple(sorted(value))

    def validate_registry(self, registered_keys: Iterable[str]) -> ProviderSelection:
        registered = set(registered_keys)
        if not registered.issuperset(self.provider_keys):
            raise ValueError("provider selection contains an unknown key")
        return self

    @property
    def fingerprint_value(self) -> tuple[str, ...]:
        """Stable immutable value for a future job fingerprint."""

        return self.provider_keys


def resolve_provider_selection(
    *,
    legacy_provider: str | None,
    provider_keys: Sequence[str] | None,
    default_provider: str | None,
    registered_keys: Iterable[str],
    live_mode: bool = True,
) -> ProviderSelection | None:
    """Design helper for a future request boundary; current APIs do not call it."""

    if not live_mode:
        if provider_keys not in (None, (), []):
            raise ValueError("live providers cannot be selected for a history-only run")
        return None

    if provider_keys is None:
        selected = legacy_provider or default_provider
        if selected is None:
            raise ValueError("a live provider must be selected explicitly")
        requested: Sequence[str] = (selected,)
    else:
        if not provider_keys:
            raise ValueError("an explicit live provider selection cannot be empty")
        requested = provider_keys

    selection = ProviderSelection(provider_keys=tuple(requested))
    if legacy_provider is not None and provider_keys is not None:
        if selection.provider_keys != (legacy_provider,):
            raise ValueError("legacy provider conflicts with provider_keys")
    return selection.validate_registry(registered_keys)


class ProviderOfferReference(ProviderContractModel):
    """Composite identity for a live commercial offer."""

    provider_key: ProviderKey
    offer_id: OfferIdentifier

    @classmethod
    def from_offer(cls, offer: Offer) -> ProviderOfferReference:
        return cls(provider_key=offer.provider, offer_id=offer.offer_id)

    @property
    def ordering_key(self) -> tuple[str, str]:
        return self.provider_key, self.offer_id


def index_offers_by_provider_identity(
    offers: Iterable[Offer],
) -> dict[ProviderOfferReference, Offer]:
    """Return a stable composite index, rejecting conflicting duplicate IDs."""

    indexed: dict[ProviderOfferReference, Offer] = {}
    for offer in offers:
        reference = ProviderOfferReference.from_offer(offer)
        previous = indexed.get(reference)
        if previous is not None and previous != offer:
            raise ValueError("conflicting offers share one provider identity")
        indexed[reference] = offer
    return dict(sorted(indexed.items(), key=lambda item: item[0].ordering_key))


class ProviderAffinity(ProviderContractModel):
    """Bounded non-secret provider context; revisions are opaque identifiers."""

    environment: Annotated[StrictStr, Field(max_length=24)] = ""
    region_id: Annotated[StrictStr, Field(max_length=80)] = ""
    config_revision: Annotated[StrictStr, Field(max_length=MAX_PROVIDER_AFFINITY_TEXT)] = ""
    adapter_revision: Annotated[StrictStr, Field(max_length=MAX_PROVIDER_AFFINITY_TEXT)] = ""


class ProviderSearchState(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    SUPPRESSED = "suppressed"
    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"


class ProviderFailureCategory(str, Enum):
    """Closed safe diagnostics; raw upstream messages never cross this DTO."""

    AUTHENTICATION = "authentication"
    INVALID_RESPONSE = "invalid_response"
    MISCONFIGURED = "misconfigured"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProviderTiming(ProviderContractModel):
    stage: Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=_TIMING_NAME_RE.pattern)]
    milliseconds: Annotated[FiniteFloat, Field(ge=0, le=600_000)]


class ProviderSearchOutcome(ProviderContractModel):
    """One request-local result; request_count counts outbound provider requests."""

    provider_key: ProviderKey
    state: ProviderSearchState
    offers: tuple[Offer, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PROVIDER_OUTCOME_OFFERS,
    )
    request_count: Annotated[
        StrictInt,
        Field(
            ge=0,
            le=MAX_PROVIDER_REQUESTS,
            description="Actual outbound provider requests or HTTP attempts represented by this outcome.",
        ),
    ] = 0
    failure_category: ProviderFailureCategory | None = None
    timings: tuple[ProviderTiming, ...] = Field(
        default_factory=tuple,
        max_length=MAX_PROVIDER_TIMING_STAGES,
    )
    affinity: ProviderAffinity = Field(default_factory=ProviderAffinity)
    catalog_version: Annotated[StrictStr, Field(min_length=1, max_length=MAX_PROVIDER_AFFINITY_TEXT)] | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> ProviderSearchOutcome:
        if len({timing.stage for timing in self.timings}) != len(self.timings):
            raise ValueError("provider timing stages must be unique")
        if any(offer.provider != self.provider_key for offer in self.offers):
            raise ValueError("provider outcomes may contain only offers from that provider")

        if self.state in {ProviderSearchState.SUCCESS, ProviderSearchState.PARTIAL_SUCCESS}:
            if not self.offers:
                raise ValueError("successful provider outcomes must contain offers")
        elif self.offers:
            raise ValueError("non-successful provider outcomes cannot contain offers")

        if self.state in {ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.SUPPRESSED}:
            if self.request_count != 0:
                raise ValueError("unattempted or suppressed outcomes cannot count requests")

        if self.state in {ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS}:
            if self.failure_category is None:
                raise ValueError("failed outcomes require a bounded failure category")
        elif self.failure_category is not None and self.state != ProviderSearchState.SUPPRESSED:
            raise ValueError("only failed or suppressed outcomes may carry a failure category")
        return self

    @property
    def attempted(self) -> bool:
        return self.state not in {ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.SUPPRESSED}

    @property
    def suppressed(self) -> bool:
        return self.state == ProviderSearchState.SUPPRESSED


class ProviderRunState(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    SUPPRESSED = "suppressed"
    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"


class ProviderRunSummaryItem(ProviderContractModel):
    """Bounded summary; request_count counts actual outbound provider requests."""

    provider_key: ProviderKey
    state: ProviderRunState
    request_count: Annotated[
        StrictInt,
        Field(
            ge=0,
            le=MAX_PROVIDER_REQUESTS,
            description="Actual outbound provider requests or HTTP attempts represented by this summary.",
        ),
    ] = 0
    offers_returned: Annotated[StrictInt, Field(ge=0, le=MAX_PROVIDER_RUN_OFFERS)] = 0
    failure_category: ProviderFailureCategory | None = None
    catalog_version: Annotated[StrictStr, Field(min_length=1, max_length=MAX_PROVIDER_AFFINITY_TEXT)] | None = None
    affinity: ProviderAffinity = Field(default_factory=ProviderAffinity)
    elapsed_milliseconds: Annotated[FiniteFloat, Field(ge=0, le=86_400_000)] | None = None

    @model_validator(mode="after")
    def _validate_counts(self) -> ProviderRunSummaryItem:
        if self.state in {ProviderRunState.NOT_ATTEMPTED, ProviderRunState.SUPPRESSED}:
            if self.request_count or self.offers_returned:
                raise ValueError("unattempted or suppressed providers cannot return results")
        if self.state in {ProviderRunState.FAILURE, ProviderRunState.PARTIAL_SUCCESS}:
            if self.failure_category is None:
                raise ValueError("failed provider summaries require a failure category")
        elif self.failure_category is not None and self.state != ProviderRunState.SUPPRESSED:
            raise ValueError("only failed or suppressed summaries may carry a failure category")
        if self.state == ProviderRunState.SUCCESS and self.offers_returned < 1:
            raise ValueError("successful provider summaries require offers")
        if self.state == ProviderRunState.EMPTY and self.offers_returned != 0:
            raise ValueError("empty provider summaries cannot count offers")
        if self.state == ProviderRunState.FAILURE and self.offers_returned != 0:
            raise ValueError("failed provider summaries cannot count usable offers")
        if self.state == ProviderRunState.PARTIAL_SUCCESS and self.offers_returned < 1:
            raise ValueError("partial success must preserve usable offers")
        return self


class ProviderExecutionSummary(ProviderContractModel):
    """Deterministically ordered per-provider outcomes for a future run."""

    providers: tuple[ProviderRunSummaryItem, ...] = Field(
        min_length=1,
        max_length=MAX_PROVIDER_SELECTION_SIZE,
    )

    @field_validator("providers", mode="before")
    @classmethod
    def _canonicalize_provider_summaries(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("providers must be a list or tuple")
        keyed_items: list[tuple[str, object]] = []
        for item in value:
            if isinstance(item, dict):
                key = item.get("provider_key")
            elif isinstance(item, ProviderRunSummaryItem):
                key = item.provider_key
            else:
                raise ValueError("provider summaries must be objects")
            if not isinstance(key, str):
                raise ValueError("provider summaries require a string provider key")
            keyed_items.append((key, item))
        keys = [key for key, _ in keyed_items]
        if len(keys) != len(set(keys)):
            raise ValueError("provider summaries must have unique keys")
        return tuple(item for _, item in sorted(keyed_items, key=lambda pair: pair[0]))

    @model_validator(mode="after")
    def _validate_total_offer_bound(self) -> ProviderExecutionSummary:
        if sum(item.offers_returned for item in self.providers) > MAX_PROVIDER_RUN_OFFERS:
            raise ValueError("provider run summaries exceed the total offer bound")
        return self

    @property
    def partial_failure(self) -> bool:
        has_usable_provider = any(
            item.state in {
                ProviderRunState.SUCCESS,
                ProviderRunState.EMPTY,
                ProviderRunState.PARTIAL_SUCCESS,
            }
            for item in self.providers
        )
        has_unavailable_provider = any(
            item.state in {ProviderRunState.FAILURE, ProviderRunState.SUPPRESSED}
            for item in self.providers
        )
        return has_usable_provider and has_unavailable_provider


class PriceBasis(str, Enum):
    UNKNOWN = "unknown"
    NET = "net"
    GROSS_INCLUDING_VAT = "gross_including_vat"
    GROSS_EXCLUDING_VAT = "gross_excluding_vat"


class TaxBasis(str, Enum):
    UNKNOWN = "unknown"
    INCLUDED = "included"
    EXCLUDED = "excluded"
    NOT_APPLICABLE = "not_applicable"


class CommercialEvidenceContract(ProviderContractModel):
    """Provider facts bound to one exact offer; this contract never authorizes export."""

    offer_reference: ProviderOfferReference
    provider_key: ProviderKey
    source_item_id: Annotated[StrictStr, Field(min_length=1, max_length=180)]
    price_field: Annotated[StrictStr, Field(min_length=1, max_length=80)] | None = None
    price_basis: PriceBasis | None = None
    currency: Annotated[StrictStr, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")] | None = None
    currency_basis: Annotated[StrictStr, Field(min_length=1, max_length=120)] | None = None
    unit: Annotated[StrictStr, Field(min_length=1, max_length=80)] | None = None
    unit_basis: Annotated[StrictStr, Field(min_length=1, max_length=120)] | None = None
    tax_basis_required: StrictBool = True
    tax_basis: TaxBasis | None = None
    affinity_required: StrictBool = False
    environment: Annotated[StrictStr, Field(max_length=24)] = ""
    region_id: Annotated[StrictStr, Field(max_length=80)] = ""
    config_revision: Annotated[StrictStr, Field(max_length=MAX_PROVIDER_AFFINITY_TEXT)] = ""

    @model_validator(mode="after")
    def _validate_offer_provider(self) -> CommercialEvidenceContract:
        if self.provider_key != self.offer_reference.provider_key:
            raise ValueError("commercial evidence provider must match its offer reference")
        return self

    def is_for_offer(self, offer: Offer) -> bool:
        """Prove both the composite offer identity and source item match."""

        return (
            self.provider_key == offer.provider == self.offer_reference.provider_key
            and self.offer_reference.offer_id == offer.offer_id
            and self.source_item_id == offer.source_item_id
        )

    @property
    def has_complete_commercial_basis(self) -> bool:
        return (
            self.price_field is not None
            and self.price_basis not in (None, PriceBasis.UNKNOWN)
            and self.currency is not None
            and self.currency_basis is not None
            and self.unit is not None
            and self.unit_basis is not None
            and (
                not self.affinity_required
                or bool(self.environment and self.region_id and self.config_revision)
            )
            and (
                not self.tax_basis_required
                or self.tax_basis not in (None, TaxBasis.UNKNOWN)
            )
        )


class CommercialEvidenceProvider(Protocol):
    """Provider-specific evidence adapter; never itself authorizes export."""

    key: str

    def commercial_evidence(self, offer: Offer) -> CommercialEvidenceContract:
        """Return source-backed facts bound to this offer; unknown facts stay unknown."""
        ...


class ProviderHealthStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class ProviderHealthSnapshot(ProviderContractModel):
    """Local health view; a remote probe is represented separately."""

    configured: StrictBool
    status: ProviderHealthStatus
    checked_at: Annotated[StrictStr, Field(max_length=40)] = ""
    catalog_version: Annotated[StrictStr, Field(min_length=1, max_length=MAX_PROVIDER_AFFINITY_TEXT)] | None = None
    failure_category: ProviderFailureCategory | None = None

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> ProviderHealthSnapshot:
        if not self.configured and self.status == ProviderHealthStatus.REACHABLE:
            raise ValueError("an unconfigured provider cannot be reachable")
        if self.status == ProviderHealthStatus.NOT_CONFIGURED and self.configured:
            raise ValueError("not_configured status requires configured=False")
        if self.status == ProviderHealthStatus.REACHABLE and self.failure_category is not None:
            raise ValueError("reachable providers cannot carry a failure category")
        return self


class ProviderLocalStatusReader(Protocol):
    """Local/status snapshot boundary; implementations must make no HTTP calls."""

    def local_status(self) -> ProviderHealthSnapshot:
        ...


class ProviderConnectivityProbe(Protocol):
    """Explicit future ADMIN operation; never invoked while reading local status."""

    def probe(self) -> ProviderHealthSnapshot:
        ...


class ProviderSearchRequestIdentity(ProviderContractModel):
    """Request-local dedup identity; never a cross-run cache key."""

    execution_scope_id: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    provider_key: ProviderKey
    request_fingerprint: Annotated[StrictStr, Field(min_length=64, max_length=64, pattern=_FINGERPRINT_RE.pattern)]
    affinity: ProviderAffinity = Field(default_factory=ProviderAffinity)
    limit: Annotated[StrictInt, Field(ge=1, le=100)]

    @property
    def equality_key(self) -> tuple[object, ...]:
        return (
            self.execution_scope_id,
            self.provider_key,
            self.request_fingerprint,
            self.affinity.environment,
            self.affinity.region_id,
            self.affinity.config_revision,
            self.affinity.adapter_revision,
            self.limit,
        )
