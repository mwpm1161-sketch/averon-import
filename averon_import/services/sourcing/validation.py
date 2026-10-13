from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from averon_import.services.sourcing.models import Offer, ProductIntent


_CYRILLIC_IDENTIFIER_TRANSLATION = str.maketrans({
    "а": "a", "в": "v", "е": "e", "з": "z", "и": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "х": "x", "э": "e",
})
_STRICT_NUMBER = re.compile(
    r"[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?\Z"
)
_TITLE_MODEL_VARIANT_RE = re.compile(
    r"(?<![\w])(?:"
    r"v(?:er(?:sion)?)?\.?\s*\d+[a-zа-яё0-9]*|"
    r"version\s*\d+[a-zа-яё0-9]*|версия\s*\d+[a-zа-яё0-9]*|"
    r"gen(?:eration)?\s*\d+[a-zа-яё0-9]*|поколение\s*\d+[a-zа-яё0-9]*|"
    r"mk\s*\d+[a-zа-яё0-9]*|"
    r"plus|плюс|pro|max|mini|xl|xxl|"
    r"модификац\w*|исполнение\w*|серия\s*\d*[a-zа-яё]*"
    r")(?![\w])",
    re.IGNORECASE,
)
_TITLE_TECHNICAL_ATTRIBUTE_RE = re.compile(
    r"(?<![\w])(?:"
    r"(?:dn|ду|pn|ру)\s*\d+(?:[.,]\d+)?|"
    r"\d+(?:[.,]\d+)?\s*(?:kw|квт|w|вт|v|в|mm|мм|cm|см|hz|гц|bar|бар)|"
    r"(?:ip|ик)\s*\d{2}|"
    r"\d+(?:[.,]\d+)?\s*[xх×]\s*\d+(?:[.,]\d+)?"
    r")(?![\w])",
    re.IGNORECASE,
)
_DIMENSION_NUMBER = r"\d+(?:[.,]\d+)?"
_UNIT_ALIASES = {
    "power": {"kw": ("power", Decimal("1")), "квт": ("power", Decimal("1")),
              "w": ("power", Decimal("0.001")), "вт": ("power", Decimal("0.001"))},
    "voltage": {"v": ("voltage", Decimal("1")), "в": ("voltage", Decimal("1"))},
    "current": {"a": ("current", Decimal("1")), "а": ("current", Decimal("1"))},
    "diameter": {"mm": ("length", Decimal("1")), "мм": ("length", Decimal("1"))},
    "dimensions": {"mm": ("length", Decimal("1")), "мм": ("length", Decimal("1"))},
    "pressure": {"pn": ("pressure", Decimal("1")), "ру": ("pressure", Decimal("1"))},
    "cable_section": {"mm2": ("area", Decimal("1")), "мм2": ("area", Decimal("1")),
                       "mm²": ("area", Decimal("1")), "мм²": ("area", Decimal("1"))},
    "cores": {
        "": ("count", Decimal("1")), "count": ("count", Decimal("1")),
        "шт": ("count", Decimal("1")), "жил": ("count", Decimal("1")),
        "жила": ("count", Decimal("1")), "жилы": ("count", Decimal("1")),
    },
}
_IMPLICIT_ATTRIBUTE_UNITS = {
    "power": "kw", "voltage": "v", "current": "a", "diameter": "mm",
    "dimensions": "mm", "pressure": "pn", "cable_section": "mm2", "cores": "count",
}


@dataclass(frozen=True)
class ValidationResult:
    matched: tuple[str, ...] = ()
    supporting_matches: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    preferred_differences: tuple[str, ...] = ()
    model_evidence_source: str = ""


def _norm(value: object) -> str:
    # Product identifiers may cross the Cyrillic/Latin boundary in OCR and
    # catalog data (for example КЭВ-9П2012Е vs KEV-9P2012E).  Normalize only
    # for comparison; source and provider values remain unchanged.
    text = str(value or "").casefold().replace("ё", "е").translate(
        _CYRILLIC_IDENTIFIER_TRANSLATION
    )
    text = text.replace("×", "x").replace("х", "x")
    text = re.sub(r"[^\w]+", "", text)
    return text


def _identity_tokens(value: object) -> tuple[str, ...]:
    text = str(value or "").casefold().replace("ё", "е").translate(
        _CYRILLIC_IDENTIFIER_TRANSLATION
    )
    text = text.replace("×", "x").replace("х", "x")
    return tuple(re.findall(r"[a-zа-яё0-9]+", text))


def _same_identity(left: object, right: object) -> bool:
    """Compare two complete structured identifiers, never a substring."""

    expected = _identity_tokens(left)
    actual = _identity_tokens(right)
    if not expected or not actual:
        return False
    # Joining token parts treats separators and whitespace consistently
    # (ABC100 == ABC-100, П17 == П 17), while requiring every model token on
    # both sides to be present. Versions, prefixes, and suffix modifications
    # therefore remain distinct structured identities.
    return "".join(expected) == "".join(actual)


def _model_in_title(model: object, title: object) -> bool:
    """Find a model as supporting title evidence when its context is safe.

    A joined or version-like suffix can identify a different model, so it
    blocks support. A recognized independent technical specification (such
    as power, voltage, diameter, pressure, IP class, or dimensions) may follow
    the model. Other alphanumeric suffixes remain ambiguous and yield no model
    evidence; none of these cases is emitted as a model conflict.
    """

    model_tokens = _identity_tokens(model)
    raw_title = str(title or "").casefold().replace("ё", "е")
    title_text = raw_title.translate(
        _CYRILLIC_IDENTIFIER_TRANSLATION
    )
    title_text = title_text.replace("×", "x").replace("х", "x")
    title_tokens = list(re.finditer(r"[a-zа-яё0-9]+", title_text))
    if not model_tokens or not title_tokens:
        return False

    target = "".join(model_tokens)
    for start, token in enumerate(title_tokens):
        combined = ""
        for stop in range(start, len(title_tokens)):
            combined += title_tokens[stop].group(0)
            if combined == target:
                before_join = (
                    title_text[title_tokens[start - 1].end():token.start()]
                    if start else ""
                )
                # A compact prefixed/suffixed token remains one identifier;
                # do not accept only the inner model segment.
                if (
                    (before_join and not any(char.isspace() for char in before_join)
                     and any(char in "-._/\\" for char in before_join))
                ):
                    break
                raw_suffix = raw_title[title_tokens[stop].end():]
                if _TITLE_MODEL_VARIANT_RE.search(raw_suffix):
                    continue
                if not any(char.isalnum() for char in raw_suffix):
                    if any(char in "-_/\\" for char in raw_suffix):
                        continue
                    return True
                technical = _TITLE_TECHNICAL_ATTRIBUTE_RE.search(raw_suffix)
                if technical and not any(
                    char.isalnum() for char in raw_suffix[:technical.start()]
                ):
                    return True
                # A non-technical suffix may be a description, a model
                # continuation, or a version we do not recognize. Keep that
                # ambiguity at REVIEW instead of upgrading it to likely.
                continue
            if not target.startswith(combined):
                break
    return False


def _same_text_identity(left: object, right: object) -> bool:
    """Legacy weak containment for non-model product-name evidence."""

    expected = _identity_tokens(left)
    actual = _identity_tokens(right)
    if not expected or not actual:
        return False
    target = "".join(expected)
    for start in range(len(actual)):
        combined = ""
        for stop in range(start, len(actual)):
            combined += actual[stop]
            if combined == target:
                return True
            if not target.startswith(combined):
                break
    return False


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        return None
    if not _STRICT_NUMBER.fullmatch(raw):
        return None
    try:
        parsed = Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _unit_text(value: object) -> str:
    return re.sub(r"[\s.]", "", str(value or "").casefold().replace("ё", "е"))


def _quantity(value: object, key: str) -> tuple[str, Decimal] | None:
    raw_value = value
    unit = ""
    if isinstance(value, dict):
        if "value" not in value or "unit" not in value:
            return None
        raw_value = value.get("value")
        unit = _unit_text(value.get("unit"))
    elif isinstance(value, str):
        text = value.strip()
        pressure = re.fullmatch(
            r"(pn|ру)\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)"
            r"(?:[eE][+-]?\d+)?)",
            text,
            re.IGNORECASE,
        )
        if pressure:
            unit, raw_value = pressure.group(1).casefold(), pressure.group(2)
        else:
            diameter = re.fullmatch(
                r"(?:dn|ду|ø|⌀)\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)"
                r"(?:[eE][+-]?\d+)?)(?:\s*(мм|mm))?",
                text,
                re.IGNORECASE,
            ) if key == "diameter" else None
            measured = re.fullmatch(
                r"([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*([^\d\s].*?)?",
                text,
                re.IGNORECASE,
            )
            if diameter:
                raw_value = diameter.group(1)
                unit = _unit_text(diameter.group(2))
            elif measured:
                raw_value = measured.group(1)
                unit = _unit_text(measured.group(2))

    number = _number(raw_value)
    if number is None:
        return None

    if not unit:
        unit = _IMPLICIT_ATTRIBUTE_UNITS.get(key, "")
    aliases = _UNIT_ALIASES.get(key, {})
    converted = aliases.get(unit)
    if converted is None:
        # For an unknown attribute preserve only exact scalar/unit semantics.
        if key in _UNIT_ALIASES:
            return None
        return (f"unit:{unit}" if unit else "scalar", number)
    dimension, factor = converted
    return dimension, number * factor


def _dimensions(value: object) -> tuple[Decimal, ...] | None:
    if isinstance(value, (list, tuple)):
        if len(value) not in {2, 3}:
            return None
        parsed = tuple(_number(part) for part in value)
        if any(part is None for part in parsed):
            return None
        return tuple(part for part in parsed if part is not None)
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s*(?:мм|mm)\s*$", "", value.strip(), flags=re.IGNORECASE)
    parts = re.split(r"\s*[x×х]\s*", text, flags=re.IGNORECASE)
    if len(parts) not in {2, 3} or any(
        not re.fullmatch(_DIMENSION_NUMBER, part.strip()) for part in parts
    ):
        return None
    parsed = tuple(_number(part.strip()) for part in parts)
    if any(part is None for part in parsed):
        return None
    return tuple(part for part in parsed if part is not None)


def _same_value(left: object, right: object, key: str) -> bool:
    if key == "model":
        return _same_identity(left, right)
    if key in {"material", "mounting_type", "features", "protection_class"}:
        return bool(_norm(left)) and _norm(left) == _norm(right)
    if key == "dimensions":
        left_dimensions, right_dimensions = _dimensions(left), _dimensions(right)
        return (
            left_dimensions is not None
            and right_dimensions is not None
            and left_dimensions == right_dimensions
        )
    left_quantity, right_quantity = _quantity(left, key), _quantity(right, key)
    if left_quantity is not None or right_quantity is not None:
        return left_quantity is not None and left_quantity == right_quantity
    if key in _UNIT_ALIASES:
        return False
    if isinstance(left, dict) or isinstance(right, dict):
        return left == right
    return bool(_norm(left)) and _norm(left) == _norm(right)


def _attribute_origin(intent: ProductIntent, key: str) -> str:
    origins = intent.evidence.get("attribute_origins", {})
    if not isinstance(origins, dict):
        return ""
    return str(origins.get(key) or "").strip()


def validate_offer(intent: ProductIntent, offer: Offer) -> ValidationResult:
    matched: list[str] = []
    supporting: list[str] = []
    conflicts: list[str] = []
    missing: list[str] = []
    preferred: list[str] = []
    model_evidence_source = ""
    offer_attrs = {str(key): value for key, value in offer.attributes.items()}
    if intent.article:
        if not offer.article:
            missing.append("article")
        elif _norm(intent.article) == _norm(offer.article):
            matched.append("article")
        else:
            conflicts.append("article")
    if intent.model:
        explicit_model = offer_attrs.get("model")
        if explicit_model is not None and str(explicit_model).strip():
            model_evidence_source = "explicit_model"
            if _same_identity(intent.model, explicit_model):
                matched.append("model")
            else:
                # Explicitly different models stay visible as alternatives,
                # matching the existing catalog-selection contract, while
                # never qualifying as MATCH or LIKELY_MATCH.
                preferred.append("model")
        elif _model_in_title(intent.model, offer.title):
            model_evidence_source = "title"
            supporting.append("model")
        elif _same_identity(intent.model, offer.article):
            model_evidence_source = "article"
            supporting.append("model")
        else:
            missing.append("model")
    elif intent.normalized_name and _same_text_identity(intent.normalized_name, offer.title):
        # A name-only intent still has deterministic product evidence.  Keep
        # this separate from model evidence so a missing requested model can
        # never be hidden by a generic title match.
        supporting.append("normalized_name")
    for key, expected in intent.required_attributes.items():
        if _attribute_origin(intent, key) == "ai_inferred":
            continue
        actual = offer_attrs.get(key)
        if actual is None:
            missing.append(key)
        elif _same_value(expected, actual, key):
            matched.append(key)
        else:
            conflicts.append(key)
    if intent.manufacturer:
        if offer.manufacturer and _norm(intent.manufacturer) != _norm(offer.manufacturer):
            preferred.append("manufacturer")
        elif offer.manufacturer:
            matched.append("manufacturer")
        else:
            missing.append("manufacturer")
    for key, expected in intent.preferred_attributes.items():
        actual = offer_attrs.get(key)
        if key == "manufacturer":
            continue
        # AI-inferred attributes remain available on ProductIntent and in the
        # audit envelope, but they are advisory and cannot change a
        # deterministic match class.
        if _attribute_origin(intent, key) == "ai_inferred":
            continue
        if actual is None:
            preferred.append(key)
        elif _same_value(expected, actual, key):
            matched.append(key)
        else:
            preferred.append(key)
    return ValidationResult(
        matched=tuple(dict.fromkeys(matched)),
        supporting_matches=tuple(dict.fromkeys(supporting)),
        conflicts=tuple(dict.fromkeys(conflicts)),
        missing=tuple(dict.fromkeys(missing)),
        preferred_differences=tuple(dict.fromkeys(preferred)),
        model_evidence_source=model_evidence_source,
    )


def explanation_for(result: ValidationResult) -> str:
    parts: list[str] = []
    if result.matched:
        parts.append("Совпало: " + ", ".join(result.matched))
    if result.supporting_matches:
        source = f" по {result.model_evidence_source}" if result.model_evidence_source else ""
        parts.append("Поддерживается" + source + ": " + ", ".join(result.supporting_matches))
    if result.preferred_differences:
        parts.append("Отличается: " + ", ".join(result.preferred_differences))
    if result.missing:
        parts.append("Не подтверждено: " + ", ".join(result.missing))
    if result.conflicts:
        parts.append("Не подходит: " + ", ".join(result.conflicts))
    return "; ".join(parts) or "Недостаточно структурированных данных для сравнения"


class DeterministicValidator:
    def validate(self, intent: ProductIntent, offer: Offer) -> ValidationResult:
        return validate_offer(intent, offer)
