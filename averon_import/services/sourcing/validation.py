from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from averon_import.services.sourcing.models import Offer, ProductIntent


_CYRILLIC_IDENTIFIER_TRANSLATION = str.maketrans({
    "а": "a", "в": "v", "е": "e", "з": "z", "и": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "х": "x", "э": "e",
})


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


def _same_identity(left: object, right: object) -> bool:
    expected = _norm(left)
    actual = _norm(right)
    if not expected or not actual:
        return False
    return expected == actual or (
        min(len(expected), len(actual)) >= 4
        and (expected in actual or actual in expected)
    )


def _number(value: object) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


def _same_value(left: object, right: object) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        left_value, right_value = left.get("value", left), right.get("value", right)
        if _number(left_value) is not None and _number(right_value) is not None:
            return _number(left_value) == _number(right_value) and _norm(left.get("unit")) == _norm(right.get("unit"))
    left_number, right_number = _number(left), _number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return _norm(left) == _norm(right)


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
                preferred.append("model")
        elif _same_identity(intent.model, offer.title):
            model_evidence_source = "title"
            supporting.append("model")
        elif _same_identity(intent.model, offer.article):
            model_evidence_source = "article"
            supporting.append("model")
        else:
            missing.append("model")
    elif intent.normalized_name and _same_identity(intent.normalized_name, offer.title):
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
        elif _same_value(expected, actual):
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
        elif _same_value(expected, actual):
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
