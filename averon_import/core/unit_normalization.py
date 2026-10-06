"""Conservative unit-family rules shared by purchase-history consumers."""

from __future__ import annotations

import re
import unicodedata


# Each normalized alias maps to its canonical family and whether the original
# sourcing resolver accepted it. Keeping both consumers in this one registry
# avoids drifting family maps while preserving sourcing's historical boundary.
_UNIT_FAMILIES: dict[str, tuple[str, bool]] = {
    "шт": ("piece", True), "штука": ("piece", True), "штуки": ("piece", True), "штук": ("piece", True),
    "пар": ("pair", False),
    "кг": ("kilogram", True), "kg": ("kilogram", True),
    "килограмм": ("kilogram", True), "килограмма": ("kilogram", True), "килограммов": ("kilogram", True),
    "г": ("gram", False), "гр": ("gram", False), "g": ("gram", False),
    "т": ("tonne", True), "ton": ("tonne", True), "tonne": ("tonne", True),
    "тонна": ("tonne", True), "тонны": ("tonne", True), "тонн": ("tonne", True),
    "м": ("meter", True), "m": ("meter", True),
    "метр": ("meter", True), "метра": ("meter", True), "метров": ("meter", True),
    "пог. м": ("meter", False), "пог м": ("meter", False), "пог.м": ("meter", False),
    "м2": ("square_meter", True), "m2": ("square_meter", True),
    "м3": ("cubic_meter", True), "m3": ("cubic_meter", True),
    "л": ("litre", True), "l": ("litre", True), "litre": ("litre", True),
    "литр": ("litre", True), "литра": ("litre", True), "литров": ("litre", True),
    "мл": ("millilitre", False), "ml": ("millilitre", False),
    "компл": ("set", True), "комплект": ("set", True),
    "комп": ("set", False), "уп": ("pack", True), "упак": ("pack", True), "упаковка": ("pack", True),
    "боб": ("bobbin", False),
}


def _sourcing_key(value: object) -> str:
    """Keep the sourcing resolver's pre-existing normalization semantics."""
    normalized = " ".join(str(value or "").casefold().split())
    return normalized.replace("²", "2").replace("³", "3").rstrip(".")


def normalize_unit_family(value: object) -> str | None:
    """Return a known canonical family; callers retain unknown raw units."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    normalized = re.sub(r"\s+", " ", normalized).replace("ё", "е")
    normalized = normalized.rstrip(".")
    entry = _UNIT_FAMILIES.get(normalized)
    return entry[0] if entry else None


def normalize_sourcing_unit_family(value: object) -> str | None:
    """Normalize only aliases historically accepted by sourcing."""
    normalized = _sourcing_key(value)
    entry = _UNIT_FAMILIES.get(normalized)
    return entry[0] if entry and entry[1] else None


__all__ = ["normalize_unit_family", "normalize_sourcing_unit_family"]
