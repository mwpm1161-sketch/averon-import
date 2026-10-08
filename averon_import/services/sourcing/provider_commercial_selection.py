"""Inactive, request-local winner selection within the strongest identity cohort."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from itertools import combinations

from pydantic import Field, field_validator, model_validator

from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState,
    CommercialOfferEvidence,
    ProviderCommercialEvaluation,
    compare_commercial_evidence,
)
from averon_import.services.sourcing.provider_matching import (
    _RECOMMENDATION_PRECEDENCE,
    _recommendation_quality,
)
from averon_import.services.sourcing.providers.contracts import (
    ProviderContractModel,
    ProviderOfferReference,
)
from averon_import.services.sourcing.providers.execution import MAX_PROVIDER_EXECUTION_OFFERS


class CommercialSelectionError(ValueError):
    """Selection or its exact validated input has been corrupted."""


class CommercialSelectionState(str, Enum):
    SELECTED = "SELECTED"
    NO_SAFE_WINNER = "NO_SAFE_WINNER"


class CommercialSelectionReason(str, Enum):
    NO_IDENTITY_CANDIDATE = "NO_IDENTITY_CANDIDATE"
    COMMERCIAL_EVIDENCE_INCOMPLETE = "COMMERCIAL_EVIDENCE_INCOMPLETE"
    COMMERCIAL_EVIDENCE_INVALID = "COMMERCIAL_EVIDENCE_INVALID"
    COMMERCIAL_BASIS_NOT_COMPARABLE = "COMMERCIAL_BASIS_NOT_COMPARABLE"
    LOWEST_PRICE_TIED = "LOWEST_PRICE_TIED"


class CommercialSelectionBasis(str, Enum):
    SOLE_STRONGEST_IDENTITY = "SOLE_STRONGEST_IDENTITY"
    LOWEST_COMPARABLE_PRICE = "LOWEST_COMPARABLE_PRICE"


def _reference(value: object) -> ProviderOfferReference:
    if isinstance(value, ProviderOfferReference):
        value = value.model_dump(mode="python")
    result = ProviderOfferReference.model_validate(value)
    if not result.offer_id.strip():
        raise ValueError("offer identifier must not be blank")
    return result


class _SelectionDecision(ProviderContractModel):
    """Bounded public scalar payload, independently of the retained legacy data."""

    state: CommercialSelectionState
    selected_reference: ProviderOfferReference | None
    candidate_references: tuple[ProviderOfferReference, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    reason_codes: tuple[CommercialSelectionReason, ...] = Field(max_length=len(CommercialSelectionReason))
    selection_basis: CommercialSelectionBasis | None

    @field_validator("selected_reference", mode="before")
    @classmethod
    def _selected_reference(cls, value: object) -> ProviderOfferReference | None:
        return None if value is None else _reference(value)

    @field_validator("candidate_references", mode="before")
    @classmethod
    def _candidates(cls, value: object) -> tuple[ProviderOfferReference, ...]:
        if not isinstance(value, tuple) or len(value) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise ValueError("candidates must be a bounded immutable tuple")
        references = tuple(_reference(item) for item in value)
        if len(set(references)) != len(references):
            raise ValueError("candidate references must be unique")
        return tuple(sorted(references, key=lambda item: item.ordering_key))

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _reasons(cls, value: object) -> tuple[CommercialSelectionReason, ...]:
        if not isinstance(value, tuple) or len(value) > len(CommercialSelectionReason):
            raise ValueError("reasons must be a bounded immutable tuple")
        reasons = tuple(CommercialSelectionReason(item) for item in value)
        if len(set(reasons)) != len(reasons):
            raise ValueError("reasons must be unique")
        return tuple(sorted(reasons, key=lambda item: item.value))

    @model_validator(mode="after")
    def _validate_state(self) -> _SelectionDecision:
        if self.state == CommercialSelectionState.SELECTED:
            if self.selected_reference is None or self.selected_reference not in self.candidate_references:
                raise ValueError("selected reference must identify exactly one candidate")
            if self.selection_basis is None or self.reason_codes:
                raise ValueError("selected state requires a basis and no failure reasons")
            if self.selection_basis == CommercialSelectionBasis.SOLE_STRONGEST_IDENTITY and len(self.candidate_references) != 1:
                raise ValueError("sole identity basis requires exactly one candidate")
            if self.selection_basis == CommercialSelectionBasis.LOWEST_COMPARABLE_PRICE and len(self.candidate_references) < 2:
                raise ValueError("price comparison basis requires multiple candidates")
        elif self.selected_reference is not None or self.selection_basis is not None or not self.reason_codes:
            raise ValueError("no-safe-winner state requires reasons and no selected reference or basis")
        return self


def _commercial_snapshot(evaluation: ProviderCommercialEvaluation) -> ProviderCommercialEvaluation:
    if type(evaluation) is not ProviderCommercialEvaluation:
        raise CommercialSelectionError("input must be a validated ProviderCommercialEvaluation")
    try:
        snapshot = evaluation._validated_snapshot()
        matching = snapshot.matching_evaluation
        if any(item.price is not None and type(item.price) is not Decimal for item in matching.execution.offers):
            raise ValueError("execution prices must retain their normalized Decimal type")
        # Structural validation only: no OfferMatcher or evidence resolver call.
        matching.__post_init__()
        if not isinstance(snapshot.evidence, tuple) or len(snapshot.evidence) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise ValueError("commercial evidence exceeds its immutable bound")
        if any(item.amount is not None and type(item.amount) is not Decimal for item in snapshot.evidence):
            raise ValueError("commercial amounts must retain their exact Decimal type")
        evidence = tuple(
            CommercialOfferEvidence.model_validate(item.model_dump(mode="python", warnings=False))
            for item in snapshot.evidence
        )
        references = tuple(item.offer_reference for item in evidence)
        expected = tuple(ProviderOfferReference.from_offer(item) for item in matching.execution.offers)
        if len(set(references)) != len(references) or references != expected:
            raise ValueError("commercial evidence must correlate exactly to execution offers")
        return snapshot
    except (AttributeError, TypeError, ValueError) as exc:
        raise CommercialSelectionError("commercial input failed exact snapshot validation") from exc


def _strongest_identity_cohort(evaluation: ProviderCommercialEvaluation) -> tuple[ProviderOfferReference, ...]:
    matches = evaluation.matching_evaluation.matches
    for decision in _RECOMMENDATION_PRECEDENCE:
        candidates = tuple(item for item in matches if item.decision == decision)
        if not candidates:
            continue
        # Reuse the exact approved M1D dimensions, including their precedence.
        strongest = min(_recommendation_quality(item) for item in candidates)
        return tuple(sorted(
            (ProviderOfferReference.from_offer(item.offer) for item in candidates
             if _recommendation_quality(item) == strongest),
            key=lambda item: item.ordering_key,
        ))
    return ()


def _decision(evaluation: ProviderCommercialEvaluation) -> _SelectionDecision:
    candidates = _strongest_identity_cohort(evaluation)

    def no_winner(*reasons: CommercialSelectionReason) -> _SelectionDecision:
        return _SelectionDecision(
            state=CommercialSelectionState.NO_SAFE_WINNER, selected_reference=None,
            candidate_references=candidates, reason_codes=reasons, selection_basis=None,
        )

    if not candidates:
        return no_winner(CommercialSelectionReason.NO_IDENTITY_CANDIDATE)
    indexed = {item.offer_reference: item for item in evaluation.evidence}
    cohort = tuple(indexed[reference] for reference in candidates)
    reasons = set()
    if any(item.evidence_state == CommercialEvidenceState.INCOMPLETE for item in cohort):
        reasons.add(CommercialSelectionReason.COMMERCIAL_EVIDENCE_INCOMPLETE)
    if any(item.evidence_state == CommercialEvidenceState.INVALID for item in cohort):
        reasons.add(CommercialSelectionReason.COMMERCIAL_EVIDENCE_INVALID)
    if reasons:
        return no_winner(*reasons)

    if len(candidates) == 1:
        selected, basis = candidates[0], CommercialSelectionBasis.SOLE_STRONGEST_IDENTITY
    else:
        for left, right in combinations(cohort, 2):
            if not compare_commercial_evidence(left, right).comparable:
                return no_winner(CommercialSelectionReason.COMMERCIAL_BASIS_NOT_COMPARABLE)
        # The first cross-offer price ordering in this architecture. Evidence
        # completeness and every pair's M2A comparability are already proven.
        minimum = min(item.amount for item in cohort)
        lowest = tuple(item.offer_reference for item in cohort if item.amount == minimum)
        if len(lowest) != 1:
            return no_winner(CommercialSelectionReason.LOWEST_PRICE_TIED)
        selected, basis = lowest[0], CommercialSelectionBasis.LOWEST_COMPARABLE_PRICE
    return _SelectionDecision(
        state=CommercialSelectionState.SELECTED, selected_reference=selected,
        candidate_references=candidates, reason_codes=(), selection_basis=basis,
    )


_COMPUTE = object()


@dataclass(frozen=True, init=False)
class ProviderCommercialSelection:
    """A selection validated against one private, deep-owned commercial snapshot."""

    _commercial: ProviderCommercialEvaluation = field(repr=False, compare=False)
    state: CommercialSelectionState
    selected_reference: ProviderOfferReference | None
    candidate_references: tuple[ProviderOfferReference, ...]
    reason_codes: tuple[CommercialSelectionReason, ...]
    selection_basis: CommercialSelectionBasis | None

    def __init__(
        self,
        commercial_evaluation: ProviderCommercialEvaluation,
        *,
        state: CommercialSelectionState | object = _COMPUTE,
        selected_reference: ProviderOfferReference | None | object = _COMPUTE,
        candidate_references: tuple[ProviderOfferReference, ...] | object = _COMPUTE,
        reason_codes: tuple[CommercialSelectionReason, ...] | object = _COMPUTE,
        selection_basis: CommercialSelectionBasis | None | object = _COMPUTE,
    ) -> None:
        snapshot = _commercial_snapshot(commercial_evaluation)
        expected = _decision(snapshot)
        supplied = dict(
            state=state, selected_reference=selected_reference,
            candidate_references=candidate_references, reason_codes=reason_codes,
            selection_basis=selection_basis,
        )
        if any(value is not _COMPUTE for value in supplied.values()):
            if any(value is _COMPUTE for value in supplied.values()):
                raise CommercialSelectionError("explicit selection requires all decision fields")
            try:
                supplied_decision = _SelectionDecision(**supplied)
            except (TypeError, ValueError) as exc:
                raise CommercialSelectionError("selection failed structural validation") from exc
            if supplied_decision != expected:
                raise CommercialSelectionError("selection contradicts the exact strongest cohort and commercial evidence")
        object.__setattr__(self, "_commercial", snapshot)
        for name, value in expected:
            object.__setattr__(self, name, value)

    @property
    def commercial_evaluation(self) -> ProviderCommercialEvaluation:
        return deepcopy(self._commercial)

    @property
    def partial_failure(self) -> bool:
        return self._commercial.partial_failure


def select_provider_commercial_winner(
    commercial_evaluation: ProviderCommercialEvaluation,
) -> ProviderCommercialSelection:
    return ProviderCommercialSelection(commercial_evaluation)
