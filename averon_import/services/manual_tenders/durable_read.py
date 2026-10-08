"""Opt-in, pure durable readers. Current production writes/readers remain v1.

V2 stores bounded facts, not runtime Offer objects or a decision engine. Nothing
in this module opens files, migrates data, serializes a run for storage, or calls
providers/matching/commercial resolvers. JSON encoding below only measures size.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, ClassVar, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from averon_import.services.sourcing.models import MatchDecision
from averon_import.services.sourcing.provider_commercial import (
    CommercialEvidenceState, CommercialIssueCode, CommercialOfferEvidence, VatBasis,
)
from averon_import.services.sourcing.provider_commercial_selection import (
    CommercialSelectionBasis, CommercialSelectionReason, CommercialSelectionState,
)
from averon_import.services.sourcing.providers.contracts import (
    MAX_PROVIDER_OUTCOME_OFFERS, MAX_PROVIDER_REQUESTS, MAX_PROVIDER_SELECTION_SIZE,
    ProviderContractModel, ProviderFailureCategory, ProviderKey,
    ProviderOfferReference, ProviderSearchState, ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import MAX_PROVIDER_EXECUTION_OFFERS

from .parser import MAX_ACTUAL_ITEMS
from .repository import MAX_TENDER_RUN_BYTES


# 25% headroom below the unchanged 1 MiB repository hard cap. Both input bytes
# and the compact canonical read projection must fit; no truncation is allowed.
MAX_DURABLE_V2_BYTES = 768 * 1024
MAX_DURABLE_OFFER_BYTES = 2048
MAX_DURABLE_MATCH_BYTES = 1024
MAX_DURABLE_EVIDENCE_BYTES = 1024
MAX_DURABLE_ATTRIBUTE_NAMES = 16

Id = Annotated[StrictStr, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")]
Fingerprint = Annotated[StrictStr, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")]
ItemId = Annotated[StrictStr, Field(max_length=180)]
Revision = Annotated[StrictStr, Field(min_length=1, max_length=120)]
Timestamp = Annotated[StrictStr, Field(min_length=1, max_length=40)]
Currency = Annotated[StrictStr, Field(max_length=3, pattern=r"^(?:[A-Z]{3})?$")]
Unit = Annotated[StrictStr, Field(max_length=80)]
AttributeName = Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_.-]*$")]
AttributeNames = Annotated[tuple[AttributeName, ...], Field(max_length=MAX_DURABLE_ATTRIBUTE_NAMES)]

_SECRET_TEXT = re.compile(
    r"(?:authorization\s*[:=]|\bbearer\s+|(?:password|passwd|client_secret|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|token)\s*[:=])", re.I,
)


class DurableTenderReadCode(str, Enum):
    CORRUPT = "TENDER_RUN_CORRUPT"
    TOO_LARGE = "TENDER_RUN_TOO_LARGE"
    UNSUPPORTED_VERSION = "TENDER_RUN_UNSUPPORTED_VERSION"


class DurableTenderReadError(ValueError):
    """Fixed safe diagnostics; validation input and upstream details never escape."""

    def __init__(self, code: DurableTenderReadCode = DurableTenderReadCode.CORRUPT):
        self.code = code
        super().__init__("Durable tender run could not be read.")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if type(value) not in (str, int, Decimal):
        raise ValueError("price must be an exact decimal, integer or decimal string")
    if isinstance(value, str) and (len(value) > 120 or not re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value,
    )):
        raise ValueError("invalid decimal text")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal") from None
    parts = result.as_tuple()
    if not result.is_finite() or result < 0 or len(parts.digits) > 80 or abs(parts.exponent) > 100:
        raise ValueError("decimal exceeds its finite nonnegative bound")
    return result


def _size_default(value: object) -> str:
    if type(value) is Decimal:
        return str(value)
    raise TypeError("not a JSON fact")


def _check_size(value: object, limit: int, *, legacy: bool = False) -> None:
    """Count compact UTF-8 JSON incrementally; never return serialized data."""
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"),
                               allow_nan=legacy, default=_size_default)
    size = 0
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > limit:
            raise DurableTenderReadError(DurableTenderReadCode.TOO_LARGE)


def _sequence(value: object, limit: int) -> tuple:
    if not isinstance(value, (tuple, list)) or len(value) > limit:
        raise ValueError("collection must be a bounded list or tuple")
    return tuple(value)


def _references(value: object, limit: int) -> tuple[DurableOfferReference, ...]:
    refs = tuple(DurableOfferReference.model_validate(item) for item in _sequence(value, limit))
    if len(set(refs)) != len(refs):
        raise ValueError("duplicate composite reference")
    return tuple(sorted(refs, key=lambda item: item.ordering_key))


class DurableContract(ProviderContractModel):
    MAX_RECORD_BYTES: ClassVar[int | None] = None

    @field_validator("*", mode="after")
    @classmethod
    def _safe_text(cls, value: object) -> object:
        values = value if isinstance(value, tuple) else (value,)
        for item in values:
            if isinstance(item, str) and (any(ord(char) < 32 for char in item) or _SECRET_TEXT.search(item)):
                raise ValueError("credential-shaped or control text is forbidden")
        return value

    @model_validator(mode="after")
    def _record_budget(self):
        if self.MAX_RECORD_BYTES is not None:
            _check_size(self.model_dump(mode="json"), self.MAX_RECORD_BYTES)
        return self


class DurableOfferReference(ProviderOfferReference, DurableContract):
    @field_validator("offer_id")
    @classmethod
    def _nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank offer identity")
        return value


class DurableProviderAffinity(DurableContract):
    environment: Annotated[StrictStr, Field(max_length=24)] = ""
    region_id: Annotated[StrictStr, Field(max_length=80)] = ""
    config_revision: Annotated[StrictStr, Field(max_length=120)] = ""
    adapter_revision: Annotated[StrictStr, Field(max_length=120)] = ""


class DurableProviderOutcome(DurableContract):
    MAX_RECORD_BYTES = 40 * 1024
    provider_key: ProviderKey
    state: ProviderSearchState
    request_count: Annotated[StrictInt, Field(ge=0, le=MAX_PROVIDER_REQUESTS)]
    failure_category: ProviderFailureCategory | None
    affinity: DurableProviderAffinity
    catalog_version: Revision | None
    offer_references: tuple[DurableOfferReference, ...] = Field(max_length=MAX_PROVIDER_OUTCOME_OFFERS)

    @field_validator("offer_references", mode="before")
    @classmethod
    def _refs(cls, value: object):
        return _references(value, MAX_PROVIDER_OUTCOME_OFFERS)

    @model_validator(mode="after")
    def _state_facts(self):
        if any(ref.provider_key != self.provider_key for ref in self.offer_references):
            raise ValueError("outcome references a foreign provider")
        success = self.state in {ProviderSearchState.SUCCESS, ProviderSearchState.PARTIAL_SUCCESS}
        if success != bool(self.offer_references):
            raise ValueError("outcome state contradicts returned references")
        if self.state in {ProviderSearchState.NOT_ATTEMPTED, ProviderSearchState.SUPPRESSED} and self.request_count:
            raise ValueError("unattempted outcome counts requests")
        failed = self.state in {ProviderSearchState.FAILURE, ProviderSearchState.PARTIAL_SUCCESS}
        if failed and self.failure_category is None:
            raise ValueError("failed outcome requires a category")
        if not failed and self.state != ProviderSearchState.SUPPRESSED and self.failure_category is not None:
            raise ValueError("nonfailed outcome has a failure category")
        return self


class DurableEtmProvenance(DurableContract):
    source: Literal["etm_ipro"]
    source_item_id: ItemId
    price_field: Annotated[StrictStr, Field(max_length=40)]
    catalog_version: Revision | None
    price_status: Annotated[StrictStr, Field(max_length=80)]


class DurableLemanaProvenance(DurableContract):
    source: Literal["lemana_b2b"]
    product_item: ItemId
    mirror_revision: Fingerprint | None
    region_id: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)] | None


class DurableUnprovenProvenance(DurableContract):
    source: Literal["unproven"]


DurableProvenance = Annotated[
    DurableEtmProvenance | DurableLemanaProvenance | DurableUnprovenProvenance,
    Field(discriminator="source"),
]


class DurableOffer(DurableContract):
    MAX_RECORD_BYTES = MAX_DURABLE_OFFER_BYTES
    offer_reference: DurableOfferReference
    source_item_id: ItemId
    title: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    article: Annotated[StrictStr, Field(max_length=100)]
    manufacturer: Annotated[StrictStr, Field(max_length=100)]
    brand: Annotated[StrictStr, Field(max_length=100)]
    price: Decimal | None
    currency: Currency
    price_unit: Unit
    availability: StrictBool | None
    availability_text: Annotated[StrictStr, Field(max_length=120)]
    url: Annotated[StrictStr, Field(max_length=500)]
    provenance: DurableProvenance

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, value: object):
        return _decimal(value)

    @field_validator("url")
    @classmethod
    def _product_url(cls, value: str) -> str:
        if value:
            parts = urlsplit(value)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.query or parts.fragment or "\\" in value
                    or any(char.isspace() for char in value) or _SECRET_TEXT.search(unquote(value))):
                raise ValueError("product URL must be a credential-free HTTP(S) URL without query or fragment")
            # Accessing port validates malformed/non-numeric ports without I/O.
            _ = parts.port
        return value


class DurableDeterministicEvidence(DurableContract):
    hard_contradiction: StrictBool
    preferred_differences: AttributeNames
    model_evidence_source: Literal["", "explicit_model", "title", "article"]


class DurableMatch(DurableContract):
    MAX_RECORD_BYTES = MAX_DURABLE_MATCH_BYTES
    offer_reference: DurableOfferReference
    decision: MatchDecision
    rank: Annotated[StrictInt, Field(ge=1, le=MAX_PROVIDER_EXECUTION_OFFERS)]
    matched_attributes: AttributeNames
    supporting_attributes: AttributeNames
    conflicting_attributes: AttributeNames
    missing_attributes: AttributeNames
    deterministic_evidence: DurableDeterministicEvidence
    explanation: Annotated[StrictStr, Field(max_length=240)]

    @model_validator(mode="after")
    def _deterministic_facts(self):
        if self.deterministic_evidence.hard_contradiction != bool(self.conflicting_attributes):
            raise ValueError("hard contradiction disagrees with stored conflicts")
        return self


class DurableCommercialEvidence(CommercialOfferEvidence, DurableContract):
    """Reuse M2A scalar invariants, never an M2A resolver/evaluation."""
    MAX_RECORD_BYTES = MAX_DURABLE_EVIDENCE_BYTES
    offer_reference: DurableOfferReference

    @field_validator("offer_reference", mode="before")
    @classmethod
    def _validate_reference(cls, value: object):
        return DurableOfferReference.model_validate(value)

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object):
        return _decimal(value)

    @field_validator("issue_codes", mode="before")
    @classmethod
    def _canonical_issues(cls, value: object):
        return super()._canonical_issues(_sequence(value, 10))


class DurableCommercialSelection(DurableContract):
    MAX_RECORD_BYTES = 128 * 1024
    state: CommercialSelectionState
    selected_reference: DurableOfferReference | None
    candidate_references: tuple[DurableOfferReference, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    reason_codes: tuple[CommercialSelectionReason, ...] = Field(max_length=len(CommercialSelectionReason))
    selection_basis: CommercialSelectionBasis | None

    @field_validator("candidate_references", mode="before")
    @classmethod
    def _candidates(cls, value: object):
        return _references(value, MAX_PROVIDER_EXECUTION_OFFERS)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _reasons(cls, value: object):
        codes = tuple(CommercialSelectionReason(item) for item in _sequence(value, len(CommercialSelectionReason)))
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate selection reason")
        return tuple(sorted(codes, key=lambda item: item.value))

    @model_validator(mode="after")
    def _selection_facts(self):
        if self.state == CommercialSelectionState.SELECTED:
            if self.selected_reference is None or self.selected_reference not in self.candidate_references:
                raise ValueError("selected reference must belong to candidates")
            if self.selection_basis is None or self.reason_codes:
                raise ValueError("selected requires a basis and no failure reasons")
            count = len(self.candidate_references)
            if self.selection_basis == CommercialSelectionBasis.SOLE_STRONGEST_IDENTITY and count != 1:
                raise ValueError("sole identity requires one candidate")
            if self.selection_basis == CommercialSelectionBasis.LOWEST_COMPARABLE_PRICE and count < 2:
                raise ValueError("price comparison requires multiple candidates")
        elif self.selected_reference is not None or self.selection_basis is not None or not self.reason_codes:
            raise ValueError("no safe winner requires reasons and no selection/basis")
        return self


class DurableTenderSourcingRowV2(DurableContract):
    source_row_id: Id
    physical_excel_row: Annotated[StrictInt, Field(ge=1, le=1_048_576)]
    result_limit: Annotated[StrictInt, Field(ge=1, le=MAX_PROVIDER_OUTCOME_OFFERS)]
    outcomes: tuple[DurableProviderOutcome, ...] = Field(min_length=1, max_length=MAX_PROVIDER_SELECTION_SIZE)
    reused_provider_keys: tuple[ProviderKey, ...] = Field(max_length=MAX_PROVIDER_SELECTION_SIZE)
    offers: tuple[DurableOffer, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    matches: tuple[DurableMatch, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    recommended_offer_reference: DurableOfferReference | None
    review_candidate_reference: DurableOfferReference | None
    commercial_evidence: tuple[DurableCommercialEvidence, ...] = Field(max_length=MAX_PROVIDER_EXECUTION_OFFERS)
    commercial_selection: DurableCommercialSelection

    @model_validator(mode="after")
    def _correlate_row(self):
        keys = tuple(item.provider_key for item in self.outcomes)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate provider outcome")
        reused = self.reused_provider_keys
        if len(set(reused)) != len(reused) or not set(reused).issubset(keys):
            raise ValueError("reused providers must be unique selected providers")
        indexed = {item.offer_reference: item for item in self.offers}
        if len(indexed) != len(self.offers):
            raise ValueError("duplicate composite offer")
        returned = [ref for outcome in self.outcomes for ref in outcome.offer_references]
        if len(set(returned)) != len(returned) or set(returned) != set(indexed):
            raise ValueError("outcome references do not match returned offers")
        if any(len(item.offer_references) > self.result_limit for item in self.outcomes):
            raise ValueError("outcome exceeds result limit")
        for records in (self.matches, self.commercial_evidence):
            refs = [item.offer_reference for item in records]
            if len(set(refs)) != len(refs) or set(refs) != set(indexed):
                raise ValueError("exactly one correlated record per offer is required")
        matches_by_reference = {item.offer_reference: item for item in self.matches}
        evidence_by_reference = {item.offer_reference: item for item in self.commercial_evidence}
        for reference in (self.recommended_offer_reference, self.review_candidate_reference):
            if reference is not None and reference not in matches_by_reference:
                raise ValueError("identity candidate references an absent match")
        if self.recommended_offer_reference is not None and matches_by_reference[self.recommended_offer_reference].decision not in {
            MatchDecision.MATCH, MatchDecision.LIKELY_MATCH, MatchDecision.ALTERNATIVE,
        }:
            raise ValueError("identity recommendation requires a recommendable stored decision")
        if self.review_candidate_reference is not None and matches_by_reference[self.review_candidate_reference].decision != MatchDecision.REVIEW:
            raise ValueError("review candidate requires a stored REVIEW decision")
        outcomes = {item.provider_key: item for item in self.outcomes}
        for evidence in self.commercial_evidence:
            offer = indexed[evidence.offer_reference]
            if evidence.amount != offer.price or evidence.price_unit != offer.price_unit:
                raise ValueError("commercial amount/unit does not match the referenced offer")
            # Approved INVALID evidence may deliberately erase an unproved
            # currency; it cannot substitute a different nonempty currency.
            if evidence.currency != offer.currency and not (
                evidence.currency == "" and evidence.evidence_state == CommercialEvidenceState.INVALID
            ):
                raise ValueError("commercial currency does not match the referenced offer")
            provider = offer.offer_reference.provider_key
            expected_revision = {"etm_ipro": "m2a-etm-pricewnds-v1",
                                 "lemana_b2b": "m2a-lemana-unproven-v1",
                                 "local_catalog": "m2a-local-unproven-v1"}.get(provider, "m2a-provider-unproven-v1")
            if evidence.basis_revision != expected_revision:
                raise ValueError("basis revision belongs to a different provider")
            proof = offer.provenance
            if provider == "etm_ipro":
                if not isinstance(proof, DurableEtmProvenance):
                    raise ValueError("ETM requires its allowlisted provenance")
                if evidence.evidence_state != CommercialEvidenceState.COMPLETE and evidence.vat_basis != VatBasis.UNKNOWN:
                    raise ValueError("unproved ETM facts cannot assert a VAT basis")
                if evidence.evidence_state == CommercialEvidenceState.COMPLETE and (
                    proof.source_item_id != offer.source_item_id or not offer.source_item_id.strip()
                    or proof.price_field != "pricewnds" or proof.price_status
                    or evidence.vat_basis != VatBasis.GROSS_INCLUDING_VAT
                    or (proof.catalog_version is not None and outcomes[provider].catalog_version is not None
                        and proof.catalog_version != outcomes[provider].catalog_version)
                ):
                    raise ValueError("complete ETM facts contradict their provenance")
            elif provider == "lemana_b2b":
                if not isinstance(proof, DurableLemanaProvenance) or evidence.vat_basis != VatBasis.UNKNOWN:
                    raise ValueError("Lemana VAT remains unproved")
                if CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN not in evidence.issue_codes:
                    raise ValueError("Lemana requires its stored unproved-basis issue")
                outcome = outcomes[provider]
                if evidence.evidence_state != CommercialEvidenceState.INVALID and (
                    proof.product_item != offer.source_item_id or not offer.source_item_id.strip()
                    or proof.mirror_revision is None or proof.region_id is None
                    or (outcome.catalog_version is not None and proof.mirror_revision != outcome.catalog_version)
                    or (outcome.affinity.region_id and str(proof.region_id) != outcome.affinity.region_id)
                ):
                    raise ValueError("Lemana evidence contradicts its source identity")
            elif not isinstance(proof, DurableUnprovenProvenance) or evidence.vat_basis != VatBasis.UNKNOWN:
                raise ValueError("other provider commercial basis remains unproved")
            elif not {CommercialIssueCode.PROVIDER_PRICE_BASIS_UNPROVEN,
                      CommercialIssueCode.PRICE_UNIT_UNTRUSTED}.issubset(evidence.issue_codes):
                raise ValueError("unproved provider requires its stored issue facts")
        selection = self.commercial_selection
        candidates = selection.candidate_references
        if not set(candidates).issubset(indexed):
            raise ValueError("commercial candidate references absent offers")
        eligible_decisions = {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH, MatchDecision.ALTERNATIVE}
        if any(matches_by_reference[reference].decision not in eligible_decisions for reference in candidates):
            raise ValueError("commercial candidates require identity-eligible stored matches")
        candidate_states = {evidence_by_reference[reference].evidence_state for reference in candidates}
        if selection.state == CommercialSelectionState.SELECTED:
            # The scalar selection contract already requires selected_reference
            # to be one of these candidates. This also proves its exact evidence
            # is COMPLETE, without deriving a cohort or comparing any prices.
            if candidate_states != {CommercialEvidenceState.COMPLETE}:
                raise ValueError("selected state requires complete evidence for every candidate")
        else:
            reasons = set(selection.reason_codes)
            if CommercialSelectionReason.NO_IDENTITY_CANDIDATE in reasons:
                if reasons != {CommercialSelectionReason.NO_IDENTITY_CANDIDATE} or candidates:
                    raise ValueError("no identity candidate requires its sole reason and empty candidates")
            elif reasons & {CommercialSelectionReason.LOWEST_PRICE_TIED,
                            CommercialSelectionReason.COMMERCIAL_BASIS_NOT_COMPARABLE}:
                if len(reasons) != 1:
                    raise ValueError("comparison reasons cannot mix with other reason families")
                # Validate only the stored branch prerequisites, without
                # comparing prices/bases or proving the recorded conclusion.
                if len(candidates) < 2 or candidate_states != {CommercialEvidenceState.COMPLETE}:
                    raise ValueError("comparison reasons require multiple complete candidates")
            elif not candidates or not reasons.issubset({
                CommercialSelectionReason.COMMERCIAL_EVIDENCE_INCOMPLETE,
                CommercialSelectionReason.COMMERCIAL_EVIDENCE_INVALID,
            }):
                raise ValueError("evidence reason family requires nonempty candidates")
            if (CommercialSelectionReason.COMMERCIAL_EVIDENCE_INCOMPLETE in reasons
                    and CommercialEvidenceState.INCOMPLETE not in candidate_states):
                raise ValueError("incomplete reason requires an incomplete candidate")
            if (CommercialSelectionReason.COMMERCIAL_EVIDENCE_INVALID in reasons
                    and CommercialEvidenceState.INVALID not in candidate_states):
                raise ValueError("invalid reason requires an invalid candidate")
        # Canonical ordering changes presentation only, never decisions/ranks.
        object.__setattr__(self, "outcomes", tuple(sorted(self.outcomes, key=lambda item: item.provider_key)))
        object.__setattr__(self, "reused_provider_keys", tuple(sorted(reused)))
        for name in ("offers", "matches", "commercial_evidence"):
            object.__setattr__(self, name, tuple(sorted(getattr(self, name), key=lambda item: item.offer_reference.ordering_key)))
        return self

    @property
    def partial_failure(self) -> bool:
        usable = {ProviderSearchState.SUCCESS, ProviderSearchState.EMPTY, ProviderSearchState.PARTIAL_SUCCESS}
        unavailable = {ProviderSearchState.FAILURE, ProviderSearchState.SUPPRESSED}
        return any(item.state in usable for item in self.outcomes) and any(item.state in unavailable for item in self.outcomes)

    @property
    def recommended_match(self) -> DurableMatch | None:
        return next((item for item in self.matches if item.offer_reference == self.recommended_offer_reference), None)

    @property
    def review_candidate(self) -> DurableMatch | None:
        return next((item for item in self.matches if item.offer_reference == self.review_candidate_reference), None)


class DurableTenderSourcingRunV2(DurableContract):
    schema_version: Annotated[StrictInt, Field(ge=2, le=2)]
    run_id: Id
    tender_id: Id
    source_sha256: Fingerprint
    workspace_revision: Annotated[StrictInt, Field(ge=1, le=1_000_000_000)]
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: Timestamp
    started_at: Timestamp
    completed_at: Timestamp | None
    selection: ProviderSelection
    selected_source_row_ids: tuple[Id, ...] = Field(min_length=1, max_length=MAX_ACTUAL_ITEMS)
    rows: tuple[DurableTenderSourcingRowV2, ...] = Field(max_length=MAX_ACTUAL_ITEMS)

    @field_validator("created_at", "started_at", "completed_at")
    @classmethod
    def _timestamp(cls, value: str | None):
        if value is not None and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("timestamps require a UTC offset")
        return value

    @model_validator(mode="after")
    def _correlate_run(self):
        ids = self.selected_source_row_ids
        row_ids = [row.source_row_id for row in self.rows]
        if len(set(ids)) != len(ids) or len(set(row_ids)) != len(row_ids) or not set(row_ids).issubset(ids):
            raise ValueError("rows must reference distinct selected source rows")
        if self.status == "completed" and set(row_ids) != set(ids):
            raise ValueError("completed run requires every selected row")
        if (self.status == "running") != (self.completed_at is None):
            raise ValueError("completion timestamp contradicts run status")
        for row in self.rows:
            if tuple(item.provider_key for item in row.outcomes) != self.selection.provider_keys:
                raise ValueError("every selected provider requires exactly one outcome per evaluated row")
        # The durable snapshot also caps aggregate offers across rows at 400.
        # This is deliberately more conservative than 400 per execution.
        if sum(len(row.offers) for row in self.rows) > MAX_PROVIDER_EXECUTION_OFFERS:
            raise ValueError("durable run exceeds aggregate execution-offer bound")
        object.__setattr__(self, "selected_source_row_ids", tuple(sorted(ids)))
        object.__setattr__(self, "rows", tuple(sorted(self.rows, key=lambda row: row.source_row_id)))
        _check_size(self.model_dump(mode="json"), MAX_DURABLE_V2_BYTES)
        return self

    @property
    def partial_failure(self) -> bool:
        return any(row.partial_failure for row in self.rows)


@dataclass(frozen=True, init=False)
class DurableTenderSourcingRunV1:
    """Detached legacy payload; existing v1 consumers keep their old structures."""
    _payload: dict = field(repr=False)

    def __init__(self, payload: dict):
        object.__setattr__(self, "_payload", deepcopy(payload))

    @property
    def schema_version(self) -> int:
        return 1

    @property
    def payload(self) -> dict:
        return deepcopy(self._payload)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise ValueError("non-finite JSON number")


def read_tender_sourcing_run(payload: bytes | str | dict) -> DurableTenderSourcingRunV1 | DurableTenderSourcingRunV2:
    """Explicit opt-in discrimination; no file access or changes to legacy callers.

    V1 follows the existing _read_path version check and json.loads semantics,
    including its historical equality-to-1 behavior; missing versions reject.
    V2 decodes JSON decimal tokens exactly and forbids duplicate object members.
    Python float inputs are rejected for canonical prices (use Decimal/strings).
    """
    try:
        raw = None
        if isinstance(payload, (bytes, str)):
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            raw_size = len(raw.encode("utf-8"))
            if raw_size > MAX_TENDER_RUN_BYTES:
                raise DurableTenderReadError(DurableTenderReadCode.TOO_LARGE)
            value = json.loads(raw)
        elif type(payload) is dict:
            value = payload
        else:
            raise ValueError("reader requires JSON bytes/text or a payload dictionary")
        if not isinstance(value, dict):
            raise ValueError("run must be an object")
        version = value.get("schema_version")
        if version == 1:
            # Legacy files are bounded by their original bytes, not by a new
            # encoding of decoded floats (which can be a few bytes larger).
            if raw is None:
                _check_size(value, MAX_TENDER_RUN_BYTES, legacy=True)
            return DurableTenderSourcingRunV1(value)
        if "schema_version" not in value:
            raise ValueError("legacy reader requires schema_version")
        if type(version) is not int or version != 2:
            raise DurableTenderReadError(DurableTenderReadCode.UNSUPPORTED_VERSION)
        if raw is not None:
            if raw_size > MAX_DURABLE_V2_BYTES:
                raise DurableTenderReadError(DurableTenderReadCode.TOO_LARGE)
            value = json.loads(raw, parse_float=Decimal, parse_constant=_invalid_constant,
                               object_pairs_hook=_unique_object)
        _check_size(value, MAX_DURABLE_V2_BYTES)
        return DurableTenderSourcingRunV2.model_validate(value)
    except DurableTenderReadError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise DurableTenderReadError() from None
