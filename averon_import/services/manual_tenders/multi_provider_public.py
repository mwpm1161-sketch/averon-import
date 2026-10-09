"""Strict allowlisted public projections for controlled multi-provider runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from averon_import.services.sourcing.models import MatchDecision
from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState, CommercialIssueCode, VatBasis,
)
from averon_import.services.sourcing.provider_commercial_selection import (
    CommercialSelectionBasis, CommercialSelectionReason, CommercialSelectionState,
)
from averon_import.services.sourcing.providers.contracts import ProviderFailureCategory, ProviderSearchState

from .durable_read import DurableTenderSourcingRunV2

PublicProvider = Literal["etm_ipro", "lemana_b2b"]


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PublicOfferReference(_PublicModel):
    provider_key: PublicProvider
    offer_id: StrictStr = Field(min_length=1, max_length=180)


class PublicProviderOutcome(_PublicModel):
    provider_key: PublicProvider
    state: ProviderSearchState
    offers_returned_count: StrictInt = Field(ge=0, le=100)
    retained_offer_count: StrictInt = Field(ge=0, le=100)
    failure_category: ProviderFailureCategory | None


class PublicOffer(_PublicModel):
    provider_key: PublicProvider
    offer_id: StrictStr = Field(min_length=1, max_length=180)
    title: StrictStr = Field(min_length=1, max_length=320)
    article: StrictStr = Field(max_length=100)
    manufacturer: StrictStr = Field(max_length=100)
    brand: StrictStr = Field(max_length=100)
    price: StrictStr | None = Field(max_length=120)
    currency: StrictStr = Field(max_length=3)
    price_unit: StrictStr = Field(max_length=80)
    availability: StrictBool | None
    availability_text: StrictStr = Field(max_length=120)
    url: StrictStr = Field(max_length=500)


class PublicMatch(_PublicModel):
    provider_key: PublicProvider
    offer_id: StrictStr = Field(min_length=1, max_length=180)
    decision: MatchDecision
    rank: StrictInt = Field(ge=1, le=500)
    matched_attributes: tuple[StrictStr, ...] = Field(max_length=16)
    supporting_attributes: tuple[StrictStr, ...] = Field(max_length=16)
    conflicting_attributes: tuple[StrictStr, ...] = Field(max_length=16)
    missing_attributes: tuple[StrictStr, ...] = Field(max_length=16)


class PublicCommercialEvidence(_PublicModel):
    provider_key: PublicProvider
    offer_id: StrictStr = Field(min_length=1, max_length=180)
    state: CommercialEvidenceState
    issue_codes: tuple[CommercialIssueCode, ...] = Field(max_length=10)
    vat_basis: VatBasis


class PublicCommercialSelection(_PublicModel):
    state: CommercialSelectionState
    selected_reference: PublicOfferReference | None
    candidate_references: tuple[PublicOfferReference, ...] = Field(max_length=500)
    reason_codes: tuple[CommercialSelectionReason, ...] = Field(max_length=5)
    selection_basis: CommercialSelectionBasis | None


class PublicMultiProviderRow(_PublicModel):
    source_row_id: StrictStr = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    excel_row: StrictInt = Field(ge=1, le=1_048_576)
    provider_outcomes: tuple[PublicProviderOutcome, ...] = Field(min_length=2, max_length=2)
    offers: tuple[PublicOffer, ...] = Field(max_length=400)
    matches: tuple[PublicMatch, ...] = Field(max_length=400)
    commercial_evidence: tuple[PublicCommercialEvidence, ...] = Field(max_length=400)
    recommended_reference: PublicOfferReference | None
    review_candidate_reference: PublicOfferReference | None
    commercial_selection: PublicCommercialSelection
    partial_failure: StrictBool


class PublicMultiProviderSummary(_PublicModel):
    schema_version: Literal[2] = 2
    run_kind: Literal["multi_provider"] = "multi_provider"
    run_id: StrictStr = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    tender_id: StrictStr = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    status: Literal["running", "completed", "failed", "interrupted"]
    source_mode: Literal["provider_only"] = "provider_only"
    providers: tuple[PublicProvider, ...] = Field(min_length=2, max_length=2)
    created_at: StrictStr = Field(max_length=40)
    started_at: StrictStr = Field(max_length=40)
    completed_at: StrictStr | None = Field(max_length=40)
    selected_positions: StrictInt = Field(ge=1, le=100)
    evaluated_positions: StrictInt = Field(ge=0, le=100)
    partial_failure_rows: StrictInt = Field(ge=0, le=100)
    safe_winner_rows: StrictInt = Field(ge=0, le=100)
    no_safe_winner_rows: StrictInt = Field(ge=0, le=100)


class PublicMultiProviderRun(PublicMultiProviderSummary):
    rows: tuple[PublicMultiProviderRow, ...] = Field(max_length=100)


def _reference(value) -> dict:
    return {"provider_key": value.provider_key, "offer_id": value.offer_id}


def _check_public_selection(run: DurableTenderSourcingRunV2) -> None:
    if tuple(run.selection.provider_keys) != ("etm_ipro", "lemana_b2b"):
        raise ValueError("run is outside the M4A public provider contract")


def _summary_values(run: DurableTenderSourcingRunV2) -> dict:
    _check_public_selection(run)
    winners = sum(row.commercial_selection.state == CommercialSelectionState.SELECTED for row in run.rows)
    return {
        "schema_version": 2,
        "run_kind": "multi_provider",
        "run_id": run.run_id,
        "tender_id": run.tender_id,
        "status": run.status,
        "source_mode": "provider_only",
        "providers": tuple(run.selection.provider_keys),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "selected_positions": len(run.selected_source_row_ids),
        "evaluated_positions": len(run.rows),
        "partial_failure_rows": sum(row.partial_failure for row in run.rows),
        "safe_winner_rows": winners,
        "no_safe_winner_rows": len(run.rows) - winners,
    }


def project_multi_provider_summary(run: DurableTenderSourcingRunV2) -> dict:
    """Return a compact summary; no offer facts, provenance, or runtime metadata."""
    return PublicMultiProviderSummary.model_validate(_summary_values(run)).model_dump(mode="json")


def project_multi_provider_run(run: DurableTenderSourcingRunV2) -> dict:
    """Map durable facts through explicit DTO allowlists; never serialize the DTO directly."""
    payload = _summary_values(run)
    rows = []
    for row in run.rows:
        rows.append({
            "source_row_id": row.source_row_id,
            "excel_row": row.physical_excel_row,
            "provider_outcomes": tuple({
                "provider_key": outcome.provider_key,
                "state": outcome.state,
                "offers_returned_count": outcome.offers_returned_count,
                "retained_offer_count": len(outcome.retained_offer_references),
                "failure_category": outcome.failure_category,
            } for outcome in row.outcomes),
            "offers": tuple({
                "provider_key": offer.offer_reference.provider_key,
                "offer_id": offer.offer_reference.offer_id,
                "title": offer.title,
                "article": offer.article,
                "manufacturer": offer.manufacturer,
                "brand": offer.brand,
                "price": None if offer.price is None else str(offer.price),
                "currency": offer.currency,
                "price_unit": offer.price_unit,
                "availability": offer.availability,
                "availability_text": offer.availability_text,
                "url": offer.url,
            } for offer in row.offers),
            "matches": tuple({
                "provider_key": match.offer_reference.provider_key,
                "offer_id": match.offer_reference.offer_id,
                "decision": match.decision,
                "rank": match.rank,
                "matched_attributes": match.matched_attributes,
                "supporting_attributes": match.supporting_attributes,
                "conflicting_attributes": match.conflicting_attributes,
                "missing_attributes": match.missing_attributes,
            } for match in row.matches),
            "commercial_evidence": tuple({
                "provider_key": evidence.offer_reference.provider_key,
                "offer_id": evidence.offer_reference.offer_id,
                "state": evidence.evidence_state,
                "issue_codes": evidence.issue_codes,
                "vat_basis": evidence.vat_basis,
            } for evidence in row.commercial_evidence),
            "recommended_reference": None if row.recommended_offer_reference is None else _reference(row.recommended_offer_reference),
            "review_candidate_reference": None if row.review_candidate_reference is None else _reference(row.review_candidate_reference),
            "commercial_selection": {
                "state": row.commercial_selection.state,
                "selected_reference": None if row.commercial_selection.selected_reference is None else _reference(row.commercial_selection.selected_reference),
                "candidate_references": tuple(_reference(value) for value in row.commercial_selection.candidate_references),
                "reason_codes": row.commercial_selection.reason_codes,
                "selection_basis": row.commercial_selection.selection_basis,
            },
            "partial_failure": row.partial_failure,
        })
    payload["rows"] = tuple(rows)
    return PublicMultiProviderRun.model_validate(payload).model_dump(mode="json")
