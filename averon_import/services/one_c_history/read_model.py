"""Bounded, source-backed read models for historical 1C purchase evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class OneCHistoryReadError(RuntimeError):
    """Safe, path-free signal that the active history snapshot is unreadable."""


@dataclass(frozen=True, slots=True)
class OneCHistoryVariant:
    variant_id: str
    item_name: str
    raw_unit: str
    unit_family: str | None
    article: str
    manufacturer: str
    characteristic: str
    first_source_row: int
    provenance_valid: bool = True


@dataclass(frozen=True, slots=True)
class OneCHistoryEvent:
    event_id: str
    item_id: str
    document_date: str
    document_type: str
    counterparty: str
    quantity: Decimal | None
    reported_unit_price_gross: Decimal | None
    effective_unit_price_gross: Decimal | None
    amount_gross: Decimal | None
    price_usable: bool
    price_basis: str
    raw_unit: str
    unit_family: str | None
    currency: str
    source_row: int
    provenance_valid: bool = True
    numeric_values_valid: bool = True


@dataclass(frozen=True, slots=True)
class OneCHistoryItem:
    item_id: str
    source_item_code: str
    display_name: str
    raw_unit: str
    unit_family: str | None
    identity_quality: str
    group_number: int | None
    article: str
    manufacturer: str
    characteristic: str
    variants: tuple[OneCHistoryVariant, ...]
    events: tuple[OneCHistoryEvent, ...]
    provenance_valid: bool = True
    integrity_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OneCHistoryCatalogSnapshot:
    version: str
    items: tuple[OneCHistoryItem, ...]
