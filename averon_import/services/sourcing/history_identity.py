"""Deterministic, conservative identity signatures for 1C history review."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation


HISTORY_IDENTITY_NORMALIZER_REVISION = "normalized-name-unit-v1"
_MAX_IDENTITY_TEXT_LENGTH = 4000
_MEASUREMENT_UNITS = r"(?:мм|см|кг|мл|м|л|г|т|шт|компл|пар)"
_DIMENSION_COMPONENT = rf"\d+(?:[.,]\d+)?(?:\s*(?:мм|см|м))?"
_DIMENSION_RE = re.compile(
    rf"(?<![\w]){_DIMENSION_COMPONENT}(?:\s*[xх×]\s*{_DIMENSION_COMPONENT})+(?![\w])",
    re.IGNORECASE,
)
_MEASUREMENT_RE = re.compile(
    rf"(?<![\w])(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>{_MEASUREMENT_UNITS})(?![\w])",
    re.IGNORECASE,
)
_NUMERIC_RANGE_RE = re.compile(r"(?<![\w])(\d{2,4})\s*-\s*(\d{2,4})(?![\w])")
_SPECIFICATION_RE = re.compile(
    r"(?<![\w])(?P<prefix>dn|pn|ip|sdr|ral|в|w|f)\s*"
    r"(?P<value>\d+(?:-\d+)*)(?![\w])",
    re.IGNORECASE,
)
_CABLE_MARKING_RE = re.compile(
    r"(?<![\w])(?P<prefix>ввгнг|пвснг)\s*\(\s*(?P<variant>[а-яa-z])\s*\)\s*-\s*ls(?![\w])",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_OPAQUE_RE = re.compile(r"[^\W_]+(?:[-/+._][^\W_]+)+", re.UNICODE)
_RELATION_WORDS = frozenset({"для", "по", "из", "с", "без", "к", "от", "на", "в", "под", "над", "через"})
_NEGATION_WORDS = frozenset({"не", "ни", "анти"})


def _decimal_text(value: str) -> str | None:
    try:
        number = Decimal(value.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    rendered = format(number.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _dimension_token(value: re.Match[str]) -> str | None:
    parts = re.split(r"\s*[xх×]\s*", value.group(0))
    if len(parts) < 2:
        return None
    normalized: list[str] = []
    for part in parts:
        match = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:\s*(мм|см|м))?", part, re.IGNORECASE)
        if not match:
            return None
        number = _decimal_text(match.group(1))
        if number is None:
            return None
        normalized.append(number + (match.group(2) or ""))
    return "dimension:" + "x".join(normalized)


def history_name_signature(value: object) -> str | None:
    """Return a stable typed lexical signature, or None when evidence is ambiguous.

    Ordinary descriptive words are order-independent, while dimensions, opaque
    identifiers, technical specifications, relations and negated clauses retain
    their structure.  This function never decides automatic match safety.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_IDENTITY_TEXT_LENGTH:
        return None
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").replace("\u00a0", " ")
    if any(unicodedata.category(character) == "Cc" for character in text):
        return None

    tokens: list[str] = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        matched = False
        for pattern, builder in (
            (_DIMENSION_RE, _dimension_token),
            (_CABLE_MARKING_RE, lambda match: f"cable:{match.group('prefix')}({match.group('variant')})-ls"),
            (_SPECIFICATION_RE, lambda match: f"spec:{match.group('prefix')}:{match.group('value')}"),
            (_NUMERIC_RANGE_RE, lambda match: f"range:{match.group(1)}-{match.group(2)}"),
            (_MEASUREMENT_RE, lambda match: f"measure:{_decimal_text(match.group('number'))}:{match.group('unit')}"),
            (_OPAQUE_RE, lambda match: f"opaque:{match.group(0)}"),
            (_WORD_RE, lambda match: match.group(0)),
        ):
            match = pattern.match(text, position)
            if match is None:
                continue
            token = builder(match)
            if token is None:
                return None
            tokens.append(token)
            position = match.end()
            matched = True
            break
        if matched:
            continue
        character = text[position]
        if character in ",.;:()[]{}":
            tokens.append(f"punctuation:{character}")
            position += 1
            continue
        # Unknown punctuation/operators are not discarded as formatting.
        return None

    # Keep preposition/object relations and negative clauses ordered.  In
    # particular, do not equate "насос для клапана" with "клапан для насоса".
    relation_index = next((index for index, token in enumerate(tokens) if token in _RELATION_WORDS | _NEGATION_WORDS), None)
    # Unrecognized punctuation can encode list boundaries or identifier
    # structure. Exact retrieval still handles literal spellings; normalized
    # review requires that such punctuation be absent or typed above.
    if any(token.startswith("punctuation:") for token in tokens):
        return None
    if relation_index is None:
        stable_tokens = sorted(tokens)
    elif tokens[relation_index:relation_index + 2] == ["по", "металлу"]:
        # This fixed material phrase is a common descriptive qualifier. Keep
        # its relation typed while allowing its other adjectives to move.
        remaining = tokens[:relation_index] + tokens[relation_index + 2:]
        stable_tokens = [*sorted(remaining), "relation:по:металлу"]
    else:
        stable_tokens = [*sorted(tokens[:relation_index]), "ordered-clause:" + " ".join(tokens[relation_index:])]
    return json.dumps(Counter(stable_tokens), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def history_name_signature_digest(value: object) -> str | None:
    signature = history_name_signature(value)
    if signature is None:
        return None
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()
