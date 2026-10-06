"""Shared conservative checks for explicit human history confirmations.

These checks decide whether a retrieval result can occupy one of the small
human-confirmation slots. The decision store still revalidates all evidence
when a candidate is read, confirmed, or exported.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import unicodedata
from typing import Any

from averon_import.core.unit_normalization import normalize_unit_family
from averon_import.services.sourcing.history_identity import history_model_characteristic_conflicts
from averon_import.services.sourcing.history_policy import MAX_FUZZY_RETRIEVAL_RANK

from .parser import parse_unit_basis


# These matcher outcomes can be considered only for FUZZY retrieval through
# the explicit identity-confirmation flow. They never change retrieval trust.
FUZZY_CONFIRMABLE_MATCH_DECISIONS = frozenset({"MATCH", "LIKELY_MATCH", "REVIEW"})
MANUAL_CONFIRMABLE_MATCH_DECISIONS = frozenset({"MATCH", "LIKELY_MATCH", "REVIEW"})


def _identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(
        unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").replace("\u00a0", " ").split()
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or len(str(value)) > 100:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def human_history_candidate_hard_gate(
    source: dict[str, Any],
    offer: dict[str, Any],
    provenance: dict[str, Any],
    match: dict[str, Any],
    route: dict[str, Any],
    *,
    expected_snapshot_version: Any,
    physical_excel_row: Any,
    require_manual_integrity: bool = True,
) -> str | None:
    """Shared immutable identity and commercial checks for human decisions."""
    if (
        offer.get("provider") != "one_c_history"
        or provenance.get("source") != "one_c_history"
        or provenance.get("source_kind") != "historical_purchase"
    ):
        return "HISTORY_CANDIDATE_PROVENANCE_INVALID"
    if (
        not isinstance(offer.get("offer_id"), str) or not offer["offer_id"].strip()
        or not isinstance(offer.get("title"), str) or not offer["title"].strip()
    ):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID"
    item_id = offer.get("source_item_id")
    if (
        not isinstance(item_id, str) or not item_id
        or provenance.get("history_item_id") != item_id
        or provenance.get("snapshot_version") != expected_snapshot_version
        or not isinstance(provenance.get("selected_event_id"), str)
        or not provenance.get("selected_event_id")
    ):
        return "HISTORY_CANDIDATE_PROVENANCE_INVALID"
    if route.get("history_selected_event_id") not in (None, "", provenance.get("selected_event_id")):
        return "HISTORY_CANDIDATE_EVENT_INVALID"
    if route.get("history_purchase_date") not in (None, "", provenance.get("purchase_date")):
        return "HISTORY_CANDIDATE_DATE_INVALID"
    if (
        isinstance(physical_excel_row, bool)
        or not isinstance(physical_excel_row, int)
        or isinstance(source.get("excel_row"), bool)
        or not isinstance(source.get("excel_row"), int)
        or physical_excel_row != source.get("excel_row")
    ):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT"

    source_article = _identity(source.get("article"))
    candidate_article = _identity(offer.get("article"))
    if source_article and (not candidate_article or source_article != candidate_article):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT"
    for source_key, offer_key in (("manufacturer", "manufacturer"),):
        source_value = _identity(source.get(source_key))
        offer_value = _identity(offer.get(offer_key))
        if source_value and offer_value and source_value != offer_value:
            return "HISTORY_CANDIDATE_SOURCE_CONFLICT"
    characteristic = offer.get("history_characteristic", "")
    if not isinstance(characteristic, str) or len(characteristic) > 300:
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID"
    if history_model_characteristic_conflicts(source.get("model"), characteristic):
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT"

    try:
        purchase_date = date.fromisoformat(str(provenance.get("purchase_date")))
    except (TypeError, ValueError):
        return "HISTORY_CANDIDATE_DATE_INVALID"
    if purchase_date > datetime.now(timezone.utc).date():
        return "HISTORY_CANDIDATE_DATE_INVALID"
    price = _decimal(offer.get("price"))
    effective_price = _decimal(provenance.get("effective_unit_price_gross"))
    if price is None or effective_price is None or price <= 0 or price != effective_price:
        return "HISTORY_CANDIDATE_PRICE_INVALID"
    if offer.get("currency") != "RUB" or provenance.get("currency_basis") not in {"source", "company_default"}:
        return "HISTORY_CANDIDATE_CURRENCY_INVALID"
    if provenance.get("price_basis") != "gross_including_vat":
        return "HISTORY_CANDIDATE_PRICE_BASIS_INVALID"

    source_unit = parse_unit_basis(source.get("raw_unit"))
    offer_unit = parse_unit_basis(offer.get("price_unit"))
    family = normalize_unit_family(offer.get("price_unit"))
    if (
        source_unit.get("trusted") is not True
        or offer_unit.get("trusted") is not True
        or source_unit.get("dimension") != offer_unit.get("dimension")
        or source_unit.get("base_unit") != offer_unit.get("base_unit")
        or not family
        or provenance.get("unit_family") != family
    ):
        return "HISTORY_CANDIDATE_UNIT_CONFLICT"
    if require_manual_integrity and offer.get("retrieval_classification") == "MANUAL_HISTORY_SEARCH":
        # This flag is produced only by exact server-side rematerialization and
        # is bound into the durable evidence fingerprint.
        if provenance.get("manual_integrity_valid") is not True:
            return "HISTORY_CANDIDATE_PROVENANCE_INVALID"
    return None


def fuzzy_confirmation_eligibility_reason(
    source: dict[str, Any],
    offer: dict[str, Any],
    provenance: dict[str, Any],
    match: dict[str, Any],
    route: dict[str, Any],
    *,
    expected_snapshot_version: Any,
    physical_excel_row: Any,
    retrieval_rank: Any,
) -> str | None:
    """Apply fuzzy-only authority checks, then the shared hard gate."""
    if (
        isinstance(retrieval_rank, bool)
        or not isinstance(retrieval_rank, int)
        or not 1 <= retrieval_rank <= MAX_FUZZY_RETRIEVAL_RANK
    ):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID"
    if match.get("decision") not in FUZZY_CONFIRMABLE_MATCH_DECISIONS:
        return "HISTORY_CANDIDATE_MATCH_NOT_CONFIRMABLE"
    conflicts = match.get("conflicting_attributes")
    if not isinstance(conflicts, list) or conflicts:
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT"
    if match.get("offer_id") != offer.get("offer_id"):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID"
    if (
        offer.get("retrieval_classification") != "FUZZY"
        or route.get("source_mode") != "one_c_only"
        or route.get("final_source_kind") != "history_review"
        or route.get("history_outcome") != "REVIEW"
        or route.get("history_safe_basis") not in (None, "")
        or not isinstance(expected_snapshot_version, str)
        or not expected_snapshot_version
        or route.get("history_catalog_version") != expected_snapshot_version
    ):
        return "HISTORY_CANDIDATE_ROUTE_INVALID"
    return human_history_candidate_hard_gate(
        source, offer, provenance, match, route,
        expected_snapshot_version=expected_snapshot_version,
        physical_excel_row=physical_excel_row,
    )


def manual_search_confirmation_eligibility_reason(
    source: dict[str, Any],
    offer: dict[str, Any],
    provenance: dict[str, Any],
    match: dict[str, Any],
    route: dict[str, Any],
    *,
    expected_snapshot_version: Any,
    physical_excel_row: Any,
) -> str | None:
    """Apply explicit manual-discovery policy, then the shared hard gate."""
    if match.get("decision") not in MANUAL_CONFIRMABLE_MATCH_DECISIONS:
        return "HISTORY_CANDIDATE_MATCH_NOT_CONFIRMABLE"
    conflicts = match.get("conflicting_attributes")
    if not isinstance(conflicts, list) or conflicts:
        return "HISTORY_CANDIDATE_SOURCE_CONFLICT"
    if match.get("offer_id") != offer.get("offer_id"):
        return "HISTORY_CANDIDATE_EVIDENCE_INVALID"
    if (
        offer.get("retrieval_classification") != "MANUAL_HISTORY_SEARCH"
        or route.get("source_mode") != "one_c_only"
        or route.get("final_source_kind") not in {"history_review", "none"}
        or route.get("history_outcome") not in {"REVIEW", "NO_MATCH"}
        or route.get("history_safe_basis") not in (None, "")
        or not isinstance(expected_snapshot_version, str)
        or not expected_snapshot_version
        or route.get("history_catalog_version") != expected_snapshot_version
    ):
        return "HISTORY_CANDIDATE_ROUTE_INVALID"
    return human_history_candidate_hard_gate(
        source, offer, provenance, match, route,
        expected_snapshot_version=expected_snapshot_version,
        physical_excel_row=physical_excel_row,
    )
