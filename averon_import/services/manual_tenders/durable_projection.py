"""Inactive, pure decision-closure projection and bounded deterministic encoder.

No production callsites, repository writes, supplier traffic or recomputation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StrictInt, ValidationError

from averon_import.services.sourcing.provider_commercial import ProviderCommercialEvaluation
from averon_import.services.sourcing.provider_commercial_selection import ProviderCommercialSelection
from averon_import.services.sourcing.provider_matching import ProviderMatchEvaluation
from averon_import.services.sourcing.providers.contracts import ProviderOfferReference, ProviderSelection
from averon_import.services.sourcing.providers.execution import ProviderExecutionResult

from .durable_read import (
    MAX_ACTUAL_ITEMS, MAX_DURABLE_V2_BYTES, DurableContract, DurableTenderReadCode,
    DurableTenderReadError, DurableTenderSourcingRowV2, DurableTenderSourcingRunV2,
    Fingerprint, Id, Timestamp, _size_default,
)
from .durable_wire import MAX_RETAINED_OFFERS, PROJECTION_POLICY, _wire_payload


class DurableProjectionCode(str, Enum):
    CORRUPT_INPUT = "DURABLE_V2_CORRUPT_INPUT"
    TOO_LARGE = "DURABLE_V2_TOO_LARGE"


class DurableProjectionError(ValueError):
    def __init__(self, code: DurableProjectionCode = DurableProjectionCode.CORRUPT_INPUT):
        self.code = code
        super().__init__("Durable v2 projection could not be produced.")


class DurableRowIdentity(DurableContract):
    source_row_id: Id
    physical_excel_row: Annotated[StrictInt, Field(ge=1, le=1_048_576)]


class DurableRunMetadata(DurableContract):
    run_id: Id
    tender_id: Id
    source_sha256: Fingerprint
    workspace_revision: Annotated[StrictInt, Field(ge=1, le=1_000_000_000)]
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: Timestamp
    started_at: Timestamp
    completed_at: Timestamp | None
    selected_source_row_ids: tuple[Id, ...] = Field(min_length=1, max_length=MAX_ACTUAL_ITEMS)


def _failure(exc: Exception) -> DurableProjectionError:
    errors = [exc]
    if isinstance(exc, ValidationError):
        errors.extend(error.get("ctx", {}).get("error") for error in exc.errors(include_input=False, include_url=False))
    code = (DurableProjectionCode.TOO_LARGE if any(isinstance(error, DurableTenderReadError)
            and error.code == DurableTenderReadCode.TOO_LARGE for error in errors) else DurableProjectionCode.CORRUPT_INPUT)
    return DurableProjectionError(code)


def _reference(value: ProviderOfferReference | None):
    return None if value is None else {"provider_key": value.provider_key, "offer_id": value.offer_id}


def _offer(value):
    proof = value.data_provenance
    if value.provider == "etm_ipro":
        provenance = {"source": proof.get("source"), "source_item_id": proof.get("source_item_id", ""),
                      "price_field": proof.get("price_field", ""), "catalog_version": proof.get("catalog_version"),
                      "price_status": proof.get("price_status", "")}
    elif value.provider == "lemana_b2b":
        provenance = {"source": proof.get("source"), "product_item": proof.get("product_item", ""),
                      "mirror_revision": proof.get("mirror_revision"), "region_id": proof.get("region_id")}
    else:
        provenance = {"source": "unproven"}
    return dict(offer_reference=_reference(ProviderOfferReference.from_offer(value)),
                source_item_id=value.source_item_id, title=value.title, article=value.article,
                manufacturer=value.manufacturer, brand=value.brand, price=value.price, currency=value.currency,
                price_unit=value.price_unit, availability=value.availability, availability_text=value.availability_text,
                url=value.url, provenance=provenance)


def _match(value):
    proof = value.deterministic_evidence
    return dict(offer_reference=_reference(ProviderOfferReference.from_offer(value.offer)),
                decision=value.decision, rank=value.rank, matched_attributes=tuple(sorted(value.matched_attributes)),
                supporting_attributes=tuple(sorted(value.supporting_attributes)), conflicting_attributes=tuple(sorted(value.conflicting_attributes)),
                missing_attributes=tuple(sorted(value.missing_attributes)), explanation=value.explanation,
                deterministic_evidence=dict(hard_contradiction=proof.get("hard_contradiction", False),
                    preferred_differences=tuple(sorted(proof.get("preferred_differences", ()))),
                    model_evidence_source=proof.get("model_evidence_source", "")))


def project_durable_provider_row_v2(
    identity: DurableRowIdentity, *, execution: ProviderExecutionResult,
    matching: ProviderMatchEvaluation, commercial: ProviderCommercialEvaluation,
    selection: ProviderCommercialSelection,
) -> DurableTenderSourcingRowV2:
    """Consume one exact validated chain; retain the union of decision references."""
    try:
        for value, expected in ((identity, DurableRowIdentity), (execution, ProviderExecutionResult),
                                (matching, ProviderMatchEvaluation), (commercial, ProviderCommercialEvaluation),
                                (selection, ProviderCommercialSelection)):
            if type(value) is not expected:
                raise ValueError("invalid projection boundary type")
        identity = DurableRowIdentity.model_validate(identity.model_dump(mode="python", warnings=False))
        snapshot = selection._validated_snapshot()
        evidence_snapshot = commercial._validated_snapshot()
        owned_commercial = snapshot.commercial_evaluation
        owned_matching = owned_commercial.matching_evaluation
        if (evidence_snapshot != owned_commercial or matching != owned_matching
                or execution != owned_matching.execution or identity.source_row_id != matching.intent.source_row_id):
            raise ValueError("runtime phases do not belong to the exact validated chain")
        for phase in (execution, matching.execution, owned_matching.execution):
            if any(item.price is not None and type(item.price) is not Decimal for item in phase.offers):
                raise ValueError("runtime prices must retain exact Decimal types")
        if any(item.amount is not None and type(item.amount) is not Decimal
               for item in (*evidence_snapshot.evidence, *owned_commercial.evidence)):
            raise ValueError("runtime evidence must retain exact Decimal types")
        # M2A records explicit field presence as well as values. Equality alone
        # does not distinguish an absent price/currency from a default value.
        if (matching.intent.model_fields_set != owned_matching.intent.model_fields_set
                or any(a.model_fields_set != b.model_fields_set for phase in (execution, matching.execution)
                       for a, b in zip(phase.offers, owned_matching.execution.offers))
                or any(a.offer.model_fields_set != b.offer.model_fields_set
                       for a, b in zip(matching.matches, owned_matching.matches))):
            raise ValueError("runtime explicit fields differ from the validated chain")
        closure = set(snapshot.candidate_references)
        closure.update(ref for ref in (snapshot.selected_reference, owned_matching.recommended_offer_reference,
                                       owned_matching.review_candidate_reference) if ref is not None)
        offers = {ProviderOfferReference.from_offer(item): item for item in owned_matching.execution.offers}
        if not closure.issubset(offers):
            raise ValueError("decision closure references absent offers")
        matches = {ProviderOfferReference.from_offer(item.offer): item for item in owned_matching.matches}
        evidence = {item.offer_reference: item for item in owned_commercial.evidence}
        references = sorted(closure, key=lambda item: item.ordering_key)
        outcomes = [dict(provider_key=item.provider_key, state=item.state, request_count=item.request_count,
                         failure_category=item.failure_category, affinity=item.affinity.model_dump(mode="python", warnings=False),
                         catalog_version=item.catalog_version, offers_returned_count=len(item.offers),
                         retained_offer_references=[_reference(ref) for ref in references if ref.provider_key == item.provider_key])
                    for item in owned_matching.outcomes]
        return DurableTenderSourcingRowV2(
            **identity.model_dump(mode="python", warnings=False), result_limit=execution.result_limit, outcomes=outcomes,
            reused_provider_keys=execution.reused_provider_keys, offers=[_offer(offers[ref]) for ref in references],
            matches=[_match(matches[ref]) for ref in references],
            recommended_offer_reference=_reference(owned_matching.recommended_offer_reference),
            review_candidate_reference=_reference(owned_matching.review_candidate_reference),
            commercial_evidence=[evidence[ref].model_dump(mode="python", warnings=False) for ref in references],
            commercial_selection=dict(state=snapshot.state, selected_reference=_reference(snapshot.selected_reference),
                candidate_references=[_reference(ref) for ref in snapshot.candidate_references],
                reason_codes=snapshot.reason_codes, selection_basis=snapshot.selection_basis),
        )
    except Exception as exc:
        raise _failure(exc) from None


def build_durable_tender_sourcing_run_v2(
    metadata: DurableRunMetadata, selection: ProviderSelection, rows: tuple[DurableTenderSourcingRowV2, ...],
) -> DurableTenderSourcingRunV2:
    try:
        if (type(metadata) is not DurableRunMetadata or type(selection) is not ProviderSelection
                or type(rows) is not tuple or len(rows) > MAX_ACTUAL_ITEMS
                or any(type(row) is not DurableTenderSourcingRowV2 for row in rows)):
            raise ValueError("invalid run builder boundary")
        if sum(len(row.offers) for row in rows) > MAX_RETAINED_OFFERS:
            raise DurableProjectionError(DurableProjectionCode.TOO_LARGE)
        metadata = DurableRunMetadata.model_validate(metadata.model_dump(mode="python", warnings=False))
        selection = ProviderSelection.model_validate(selection.model_dump(mode="python", warnings=False))
        return DurableTenderSourcingRunV2(
            schema_version=2, projection_policy=PROJECTION_POLICY, **metadata.model_dump(mode="python", warnings=False),
            selection=selection, rows=[row.model_dump(mode="python", warnings=False) for row in rows],
        )
    except Exception as exc:
        if isinstance(exc, DurableProjectionError):
            raise DurableProjectionError(exc.code) from None
        raise _failure(exc) from None


def encode_tender_sourcing_run_v2(run: DurableTenderSourcingRunV2) -> bytes:
    """All-or-nothing UTF-8 JSON, exact Decimal strings, no fallback stringification."""
    try:
        if type(run) is not DurableTenderSourcingRunV2:
            raise ValueError("encoder requires a validated v2 DTO")
        if (type(run.rows) is not tuple or len(run.rows) > MAX_ACTUAL_ITEMS
                or any(type(row) is not DurableTenderSourcingRowV2 for row in run.rows)):
            raise ValueError("invalid encoder row boundary")
        if sum(len(row.offers) for row in run.rows) > MAX_RETAINED_OFFERS:
            raise DurableProjectionError(DurableProjectionCode.TOO_LARGE)
        canonical = DurableTenderSourcingRunV2.model_validate(run.model_dump(mode="python", warnings=False))
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                                   default=_size_default, sort_keys=True)
        result = bytearray()
        for chunk in encoder.iterencode(_wire_payload(canonical.model_dump(mode="python", warnings=False))):
            encoded = chunk.encode("utf-8")
            if len(result) + len(encoded) > MAX_DURABLE_V2_BYTES:
                raise DurableProjectionError(DurableProjectionCode.TOO_LARGE)
            result.extend(encoded)
        return bytes(result)
    except Exception as exc:
        if isinstance(exc, DurableProjectionError):
            raise DurableProjectionError(exc.code) from None
        raise _failure(exc) from None
