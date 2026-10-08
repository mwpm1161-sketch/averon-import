"""Request-local commercial facts and basis compatibility; no supplier selection."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from averon_import.core.unit_normalization import normalize_sourcing_unit_family
from averon_import.services.sourcing.models import Offer
from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluation
from averon_import.services.sourcing.providers.contracts import (
    ProviderContractModel,
    ProviderOfferReference,
    ProviderSearchOutcome,
)
from averon_import.services.sourcing.providers.execution import (
    MAX_PROVIDER_EXECUTION_OFFERS,
    ProviderExecutionResult,
)


class CommercialEvaluationError(ValueError):
    """Commercial evidence does not correlate to the exact matching snapshot."""


class VatBasis(str, Enum):
    GROSS_INCLUDING_VAT = "GROSS_INCLUDING_VAT"
    NET_EXCLUDING_VAT = "NET_EXCLUDING_VAT"
    UNKNOWN = "UNKNOWN"


class CommercialEvidenceState(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"


class CommercialIssueCode(str, Enum):
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_NON_POSITIVE = "PRICE_NON_POSITIVE"
    PRICE_INVALID = "PRICE_INVALID"
    CURRENCY_UNKNOWN = "CURRENCY_UNKNOWN"
    VAT_BASIS_UNKNOWN = "VAT_BASIS_UNKNOWN"
    PRICE_UNIT_UNKNOWN = "PRICE_UNIT_UNKNOWN"
    PRICE_UNIT_UNTRUSTED = "PRICE_UNIT_UNTRUSTED"
    PACKAGING_BASIS_UNPROVEN = "PACKAGING_BASIS_UNPROVEN"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    PROVIDER_PRICE_BASIS_UNPROVEN = "PROVIDER_PRICE_BASIS_UNPROVEN"


UnitFamily = Literal[
    "piece", "kilogram", "tonne", "meter", "square_meter", "cubic_meter", "litre",
    "pack", "set",
]
BasisRevision = Literal[
    "m2a-etm-pricewnds-v1", "m2a-lemana-unproven-v1", "m2a-local-unproven-v1",
    "m2a-provider-unproven-v1",
]
_DIRECT_UNIT_FAMILIES = frozenset({
    "piece", "kilogram", "tonne", "meter", "square_meter", "cubic_meter", "litre",
})
_INVALID_ISSUES = frozenset({
    CommercialIssueCode.PRICE_INVALID, CommercialIssueCode.PROVENANCE_MISMATCH,
})


def _amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError("amount must be Decimal-compatible")
    if isinstance(value, str) and len(value) > 120:
        raise ValueError("amount exceeds its textual bound")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be Decimal-compatible") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be finite and non-negative")
    parts = amount.as_tuple()
    if len(parts.digits) > 80 or abs(parts.exponent) > 100:
        raise ValueError("amount exceeds its numeric bound")
    return amount


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) > 12:
        return ""
    value = value.strip().upper()
    return value if re.fullmatch(r"[A-Z]{3}", value) else ""


def _unit(value: object) -> str:
    if not isinstance(value, str) or len(value) > 80:
        return ""
    return " ".join(value.split())


class CommercialOfferEvidence(ProviderContractModel):
    """Bounded immutable scalar facts; arbitrary provider payloads are excluded."""

    offer_reference: ProviderOfferReference
    amount: Decimal | None
    currency: Annotated[StrictStr, Field(max_length=3)]
    vat_basis: VatBasis
    price_unit: Annotated[StrictStr, Field(max_length=80)]
    unit_family: UnitFamily | None
    evidence_state: CommercialEvidenceState
    issue_codes: tuple[CommercialIssueCode, ...] = Field(max_length=len(CommercialIssueCode))
    basis_revision: BasisRevision

    @field_validator("offer_reference", mode="before")
    @classmethod
    def _validate_reference(cls, value: object) -> object:
        if isinstance(value, ProviderOfferReference):
            value = value.model_dump(mode="python")
        reference = ProviderOfferReference.model_validate(value)
        if not reference.offer_id.strip():
            raise ValueError("offer identifier must not be blank")
        return reference

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> Decimal | None:
        return None if value is None else _amount(value)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        if value and _currency(value) != value:
            raise ValueError("currency must be an explicit normalized three-letter code")
        return value

    @field_validator("price_unit")
    @classmethod
    def _validate_unit(cls, value: str) -> str:
        if _unit(value) != value:
            raise ValueError("price unit must be normalized")
        return value

    @field_validator("issue_codes", mode="before")
    @classmethod
    def _canonical_issues(cls, value: object) -> tuple[CommercialIssueCode, ...]:
        if not isinstance(value, tuple) or len(value) > len(CommercialIssueCode):
            raise ValueError("issue codes must be a bounded tuple")
        codes = tuple(CommercialIssueCode(item) for item in value)
        if len(set(codes)) != len(codes):
            raise ValueError("issue codes must be unique")
        return tuple(sorted(codes, key=lambda item: item.value))

    @model_validator(mode="after")
    def _validate_basis(self) -> CommercialOfferEvidence:
        issues = set(self.issue_codes)
        expected_family = normalize_sourcing_unit_family(self.price_unit)
        if self.unit_family != expected_family:
            raise ValueError("unit family must follow shared sourcing normalization")
        required = set()
        if self.amount is None and CommercialIssueCode.PRICE_INVALID not in issues:
            required.add(CommercialIssueCode.PRICE_MISSING)
        if self.amount is not None and self.amount == 0:
            required.add(CommercialIssueCode.PRICE_NON_POSITIVE)
        if not self.currency:
            required.add(CommercialIssueCode.CURRENCY_UNKNOWN)
        if self.vat_basis == VatBasis.UNKNOWN:
            required.add(CommercialIssueCode.VAT_BASIS_UNKNOWN)
        if not self.price_unit:
            required.add(CommercialIssueCode.PRICE_UNIT_UNKNOWN)
        elif self.unit_family is None:
            required.add(CommercialIssueCode.PRICE_UNIT_UNTRUSTED)
        if self.unit_family in {"pack", "set"}:
            required.add(CommercialIssueCode.PACKAGING_BASIS_UNPROVEN)
        if not required.issubset(issues):
            raise ValueError("unknown or unusable facts require explicit issue codes")
        expected_state = (
            CommercialEvidenceState.INVALID if issues & _INVALID_ISSUES else
            CommercialEvidenceState.INCOMPLETE if issues else CommercialEvidenceState.COMPLETE
        )
        if self.evidence_state != expected_state:
            raise ValueError("evidence state contradicts its commercial facts")
        return self


def _facts(offer: Offer) -> tuple[Decimal | None, str, str, set[CommercialIssueCode]]:
    issues: set[CommercialIssueCode] = set()
    amount = None
    if offer.price is None:
        issues.add(CommercialIssueCode.PRICE_MISSING)
    else:
        try:
            amount = _amount(offer.price)
        except ValueError:
            issues.add(CommercialIssueCode.PRICE_INVALID)
        if amount == 0:
            issues.add(CommercialIssueCode.PRICE_NON_POSITIVE)
    currency = _currency(offer.currency)
    if not currency:
        issues.add(CommercialIssueCode.CURRENCY_UNKNOWN)
    unit = _unit(offer.price_unit)
    family = normalize_sourcing_unit_family(unit)
    if not unit:
        issues.add(CommercialIssueCode.PRICE_UNIT_UNKNOWN)
    elif family is None or "price_unit" not in offer.model_fields_set:
        # The legacy Offer default is not proof of the supplier's price unit.
        issues.add(CommercialIssueCode.PRICE_UNIT_UNTRUSTED)
    if family in {"pack", "set"}:
        issues.add(CommercialIssueCode.PACKAGING_BASIS_UNPROVEN)
    return amount, currency, unit, issues


def _coherent_source(offer: Offer, source_field: str) -> bool:
    provenance = offer.data_provenance
    return (
        isinstance(provenance, dict)
        and provenance.get("source") == offer.provider
        and isinstance(offer.source_item_id, str)
        and 0 < len(offer.source_item_id) <= 180
        and bool(offer.source_item_id.strip())
        and provenance.get(source_field) == offer.source_item_id
    )


def _contradictory_facts(offer: Offer, vat: VatBasis) -> bool:
    provenance = offer.data_provenance
    if not isinstance(provenance, dict):
        return True
    if "currency" in provenance and (
        not _currency(provenance["currency"])
        or _currency(provenance["currency"]) != _currency(offer.currency)
    ):
        return True
    if "price_unit" in provenance and (
        not _unit(provenance["price_unit"])
        or (normalize_sourcing_unit_family(provenance["price_unit"]) or _unit(provenance["price_unit"]).casefold())
        != (normalize_sourcing_unit_family(offer.price_unit) or _unit(offer.price_unit).casefold())
    ):
        return True
    return (
        vat != VatBasis.UNKNOWN and "vat_basis" in provenance
        and provenance["vat_basis"] != vat.value
    )


def _evidence(offer: Offer, *, revision: BasisRevision, vat: VatBasis,
              issues: set[CommercialIssueCode], currency_coherent: bool = True) -> CommercialOfferEvidence:
    amount, currency, unit, factual_issues = _facts(offer)
    issues = issues | factual_issues
    if not currency_coherent:
        currency = ""
        issues.add(CommercialIssueCode.CURRENCY_UNKNOWN)
    if vat == VatBasis.UNKNOWN:
        issues.add(CommercialIssueCode.VAT_BASIS_UNKNOWN)
    if _contradictory_facts(offer, vat):
        issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        vat = VatBasis.UNKNOWN
        issues.add(CommercialIssueCode.VAT_BASIS_UNKNOWN)
    state = (
        CommercialEvidenceState.INVALID if issues & _INVALID_ISSUES else
        CommercialEvidenceState.INCOMPLETE if issues else CommercialEvidenceState.COMPLETE
    )
    return CommercialOfferEvidence(
        offer_reference=ProviderOfferReference.from_offer(offer), amount=amount,
        currency=currency, vat_basis=vat, price_unit=unit,
        unit_family=normalize_sourcing_unit_family(unit), evidence_state=state,
        issue_codes=tuple(issues), basis_revision=revision,
    )


class CommercialEvidenceResolver(Protocol):
    def resolve(self, offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
        ...


class EtmCommercialEvidenceResolver:
    def resolve(self, offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
        issues: set[CommercialIssueCode] = set()
        coherent = offer.provider == "etm_ipro" and _coherent_source(offer, "source_item_id")
        if not coherent:
            issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        proven_path = coherent and offer.data_provenance.get("price_field") == "pricewnds"
        if not proven_path:
            issues.add(CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN)
        expected_vat = VatBasis.GROSS_INCLUDING_VAT if proven_path else VatBasis.UNKNOWN
        if _contradictory_facts(offer, expected_vat):
            issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        if coherent and offer.price is not None and offer.data_provenance.get("price_status", "") != "":
            issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        if coherent and "catalog_version" in offer.data_provenance:
            version = offer.data_provenance["catalog_version"]
            if not isinstance(version, str) or not 0 < len(version) <= 120:
                issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
            if outcome is not None and outcome.catalog_version is not None and version != outcome.catalog_version:
                issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        if outcome is not None and outcome.provider_key != offer.provider:
            issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        amount, currency, unit, factual_issues = _facts(offer)
        proven = (
            proven_path and amount is not None and amount > 0 and bool(currency)
            and normalize_sourcing_unit_family(unit) in _DIRECT_UNIT_FAMILIES
            and not factual_issues and not issues
        )
        return _evidence(
            offer, revision="m2a-etm-pricewnds-v1",
            vat=VatBasis.GROSS_INCLUDING_VAT if proven else VatBasis.UNKNOWN, issues=issues,
            currency_coherent=coherent and CommercialIssueCode.PROVENANCE_MISMATCH not in issues,
        )


class LemanaCommercialEvidenceResolver:
    def resolve(self, offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
        issues = {CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN}
        coherent = offer.provider == "lemana_b2b" and _coherent_source(offer, "product_item")
        provenance = offer.data_provenance if isinstance(offer.data_provenance, dict) else {}
        revision = provenance.get("mirror_revision")
        region = provenance.get("region_id")
        # Current mirror revisions are SHA-256 fingerprints; regions are positive integers.
        coherent = coherent and isinstance(revision, str) and bool(re.fullmatch(r"[a-f0-9]{64}", revision))
        coherent = coherent and type(region) is int and 0 < region <= 2_147_483_647
        if outcome is not None:
            coherent = coherent and outcome.provider_key == offer.provider
            if outcome.affinity.region_id:
                coherent = coherent and outcome.affinity.region_id == str(region)
            if outcome.catalog_version is not None:
                coherent = coherent and outcome.catalog_version == revision
        if not coherent:
            issues.add(CommercialIssueCode.PROVENANCE_MISMATCH)
        # Mirror and region values are validated, never copied into public evidence.
        return _evidence(offer, revision="m2a-lemana-unproven-v1", vat=VatBasis.UNKNOWN, issues=issues)


class LocalCommercialEvidenceResolver:
    def resolve(self, offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
        return _evidence(
            offer, revision="m2a-local-unproven-v1", vat=VatBasis.UNKNOWN,
            issues={CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN, CommercialIssueCode.PRICE_UNIT_UNTRUSTED},
        )


class UnprovenCommercialEvidenceResolver:
    def resolve(self, offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
        return _evidence(
            offer, revision="m2a-provider-unproven-v1", vat=VatBasis.UNKNOWN,
            issues={CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN, CommercialIssueCode.PRICE_UNIT_UNTRUSTED},
        )


# Independent, explicit trust allowlist. Runtime provider registration cannot extend it.
COMMERCIAL_EVIDENCE_RESOLVERS = MappingProxyType({
    "etm_ipro": EtmCommercialEvidenceResolver(),
    "lemana_b2b": LemanaCommercialEvidenceResolver(),
    "local_catalog": LocalCommercialEvidenceResolver(),
})
_UNPROVEN_RESOLVER = UnprovenCommercialEvidenceResolver()


def resolve_commercial_evidence(offer: Offer, outcome: ProviderSearchOutcome | None = None) -> CommercialOfferEvidence:
    resolver = COMMERCIAL_EVIDENCE_RESOLVERS.get(offer.provider, _UNPROVEN_RESOLVER)
    return resolver.resolve(offer, outcome)


class CommercialComparabilityReason(str, Enum):
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    PRICE_UNUSABLE = "PRICE_UNUSABLE"
    CURRENCY_UNKNOWN = "CURRENCY_UNKNOWN"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    VAT_BASIS_UNKNOWN = "VAT_BASIS_UNKNOWN"
    VAT_BASIS_MISMATCH = "VAT_BASIS_MISMATCH"
    PRICE_UNIT_UNTRUSTED = "PRICE_UNIT_UNTRUSTED"
    PRICE_UNIT_MISMATCH = "PRICE_UNIT_MISMATCH"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    PACKAGING_BASIS_UNPROVEN = "PACKAGING_BASIS_UNPROVEN"


class CommercialComparability(ProviderContractModel):
    left_reference: ProviderOfferReference
    right_reference: ProviderOfferReference
    comparable: StrictBool
    reason_codes: tuple[CommercialComparabilityReason, ...] = Field(max_length=len(CommercialComparabilityReason))

    @field_validator("left_reference", "right_reference", mode="before")
    @classmethod
    def _validate_reference(cls, value: object) -> object:
        return CommercialOfferEvidence._validate_reference(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _canonical_reasons(cls, value: object) -> tuple[CommercialComparabilityReason, ...]:
        if not isinstance(value, tuple) or len(value) > len(CommercialComparabilityReason):
            raise ValueError("reasons must be a bounded tuple")
        codes = tuple(CommercialComparabilityReason(item) for item in value)
        if len(set(codes)) != len(codes):
            raise ValueError("reasons must be unique")
        return tuple(sorted(codes, key=lambda item: item.value))

    @model_validator(mode="after")
    def _validate_result(self) -> CommercialComparability:
        if self.comparable != (not self.reason_codes):
            raise ValueError("comparability must agree with its reasons")
        return self


def compare_commercial_evidence(left: CommercialOfferEvidence, right: CommercialOfferEvidence) -> CommercialComparability:
    # Revalidate escape-hatch model_copy/model_construct inputs at this public boundary.
    left = CommercialOfferEvidence.model_validate(left.model_dump(mode="python"))
    right = CommercialOfferEvidence.model_validate(right.model_dump(mode="python"))
    reasons: set[CommercialComparabilityReason] = set()
    for item in (left, right):
        if item.evidence_state == CommercialEvidenceState.INVALID:
            reasons.add(CommercialComparabilityReason.EVIDENCE_INVALID)
        elif item.evidence_state != CommercialEvidenceState.COMPLETE:
            reasons.add(CommercialComparabilityReason.EVIDENCE_INCOMPLETE)
        if item.amount is None or not item.amount.is_finite() or item.amount <= 0:
            reasons.add(CommercialComparabilityReason.PRICE_UNUSABLE)
        if not item.currency:
            reasons.add(CommercialComparabilityReason.CURRENCY_UNKNOWN)
        if item.vat_basis == VatBasis.UNKNOWN:
            reasons.add(CommercialComparabilityReason.VAT_BASIS_UNKNOWN)
        if item.unit_family not in _DIRECT_UNIT_FAMILIES or CommercialIssueCode.PRICE_UNIT_UNTRUSTED in item.issue_codes:
            reasons.add(CommercialComparabilityReason.PRICE_UNIT_UNTRUSTED)
        if CommercialIssueCode.PROVENANCE_MISMATCH in item.issue_codes:
            reasons.add(CommercialComparabilityReason.PROVENANCE_MISMATCH)
        if CommercialIssueCode.PACKAGING_BASIS_UNPROVEN in item.issue_codes:
            reasons.add(CommercialComparabilityReason.PACKAGING_BASIS_UNPROVEN)
    if left.currency != right.currency:
        reasons.add(CommercialComparabilityReason.CURRENCY_MISMATCH)
    if left.vat_basis != right.vat_basis:
        reasons.add(CommercialComparabilityReason.VAT_BASIS_MISMATCH)
    if left.unit_family != right.unit_family:
        reasons.add(CommercialComparabilityReason.PRICE_UNIT_MISMATCH)
    return CommercialComparability(
        left_reference=left.offer_reference, right_reference=right.offer_reference,
        comparable=not reasons, reason_codes=tuple(reasons),
    )


def _matching_snapshot(evaluation: ProviderMatchEvaluation) -> ProviderMatchEvaluation:
    if not isinstance(evaluation, ProviderMatchEvaluation):
        raise CommercialEvaluationError("matching must be a ProviderMatchEvaluation")
    try:
        return ProviderMatchEvaluation(
            intent=deepcopy(evaluation.intent), execution=deepcopy(evaluation.execution),
            matches=evaluation.matches,
            recommended_offer_reference=evaluation.recommended_offer_reference,
            review_candidate_reference=evaluation.review_candidate_reference,
        )
    except (TypeError, ValueError) as exc:
        raise CommercialEvaluationError("matching snapshot failed structural validation") from exc


def _resolve_snapshot(evaluation: ProviderMatchEvaluation) -> tuple[CommercialOfferEvidence, ...]:
    outcomes = {item.provider_key: item for item in evaluation.outcomes}
    return tuple(
        resolve_commercial_evidence(offer, outcomes[offer.provider])
        for offer in sorted(evaluation.execution.offers, key=lambda item: (item.provider, item.offer_id))
    )


@dataclass(frozen=True, init=False)
class ProviderCommercialEvaluation:
    """Exact evidence cardinality, with a private deep-owned matching snapshot."""

    _matching: ProviderMatchEvaluation = field(repr=False)
    evidence: tuple[CommercialOfferEvidence, ...]
    _validated_matching: ProviderMatchEvaluation = field(repr=False, compare=False)
    _validated_evidence: tuple[CommercialOfferEvidence, ...] = field(repr=False, compare=False)

    def __init__(self, matching_evaluation: ProviderMatchEvaluation,
                 evidence: tuple[CommercialOfferEvidence, ...]) -> None:
        snapshot = _matching_snapshot(matching_evaluation)
        if not isinstance(evidence, tuple) or len(evidence) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise CommercialEvaluationError("evidence must be a bounded immutable tuple")
        try:
            records = tuple(CommercialOfferEvidence.model_validate(item.model_dump(mode="python")) for item in evidence)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CommercialEvaluationError("evidence failed structural validation") from exc
        references = tuple(item.offer_reference for item in records)
        if len(set(references)) != len(references):
            raise CommercialEvaluationError("duplicate composite commercial reference")
        records = tuple(sorted(records, key=lambda item: item.offer_reference.ordering_key))
        if records != _resolve_snapshot(snapshot):
            # Checks missing/foreign records AND exact factual/provider provenance correlation.
            raise CommercialEvaluationError("evidence does not match the exact execution offers")
        object.__setattr__(self, "_matching", snapshot)
        object.__setattr__(self, "evidence", records)
        # Keep a detached validation witness for downstream consumers. Checking
        # this witness must not repeat the provider-specific evidence resolvers.
        object.__setattr__(self, "_validated_matching", deepcopy(snapshot))
        object.__setattr__(self, "_validated_evidence", deepcopy(records))

    def _validated_snapshot(self) -> ProviderCommercialEvaluation:
        try:
            if self._matching != self._validated_matching or self.evidence != self._validated_evidence:
                raise CommercialEvaluationError("commercial evaluation changed after validation")
            if any(
                actual.model_fields_set != validated.model_fields_set
                for actual, validated in zip(
                    self._matching.execution.offers, self._validated_matching.execution.offers,
                )
            ):
                raise CommercialEvaluationError("explicit offer fields changed after validation")
            return deepcopy(self)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CommercialEvaluationError("commercial evaluation validation witness is invalid") from exc

    @property
    def matching_evaluation(self) -> ProviderMatchEvaluation:
        return deepcopy(self._matching)

    @property
    def execution(self) -> ProviderExecutionResult:
        return deepcopy(self._matching.execution)

    @property
    def outcomes(self) -> tuple[ProviderSearchOutcome, ...]:
        return deepcopy(self._matching.outcomes)

    @property
    def partial_failure(self) -> bool:
        return self._matching.partial_failure


def evaluate_provider_commercial_evidence(evaluation: ProviderMatchEvaluation) -> ProviderCommercialEvaluation:
    snapshot = _matching_snapshot(evaluation)
    return ProviderCommercialEvaluation(snapshot, _resolve_snapshot(snapshot))
