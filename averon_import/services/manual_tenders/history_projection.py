"""Compact, run-scoped storage for bounded 1C event display provenance."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any


_PENDING_KEY = "_history_candidate_v2_pending"
HISTORY_CANDIDATE_V2_KEY = "history_candidate_v2"
HISTORY_TEXT_POOL_KEY = "history_text_pool_v2"
MAX_WAREHOUSE_DISPLAY_BYTES = 128
MAX_COUNTERPARTY_DISPLAY_BYTES = 120
_POOL_REF_RE = re.compile(r"^~[0-9A-Z]+$")


def _pool_ref(index: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = index
    rendered = "0" if value == 0 else ""
    while value:
        value, remainder = divmod(value, 36)
        rendered = digits[remainder] + rendered
    return "~" + rendered


def _pool_index(token: str) -> int | None:
    if not _POOL_REF_RE.fullmatch(token):
        return None
    value = 0
    for char in token[1:]:
        value = value * 36 + int(char, 36)
    return value


def bounded_utf8_text(value: Any, max_bytes: int) -> tuple[str, bool]:
    """Return bounded display text without splitting a UTF-8 code point."""
    if not isinstance(value, str):
        return "", False
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return "", True
    if len(raw) <= max_bytes:
        return value, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def pending_history_candidate_v2(
    compact_fuzzy_v1: dict[str, Any],
    *,
    warehouse: Any,
    counterparty: Any,
    warehouse_truncated: bool = False,
    counterparty_truncated: bool = False,
) -> dict[str, Any]:
    """Wrap already-validated fuzzy-v1 evidence with bounded event display facts.

    Authority-bearing fuzzy fields are copied only from the existing validated
    compact-v1 projection. The display values are bounded before the run-level
    encoder sees them.
    """
    warehouse_text, warehouse_cut = bounded_utf8_text(warehouse, MAX_WAREHOUSE_DISPLAY_BYTES)
    counterparty_text, counterparty_cut = bounded_utf8_text(counterparty, MAX_COUNTERPARTY_DISPLAY_BYTES)
    flags = (1 if warehouse_truncated or warehouse_cut else 0) | (2 if counterparty_truncated or counterparty_cut else 0)
    return {
        _PENDING_KEY: [
            compact_fuzzy_v1.get("identity"),
            compact_fuzzy_v1.get("provenance"),
            compact_fuzzy_v1.get("match"),
            compact_fuzzy_v1.get("rank"),
            warehouse_text or None,
            counterparty_text or None,
            flags,
        ]
    }


def encode_history_candidate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Encode pending v2 candidates and a deterministic shared text pool.

    Existing candidates, including persisted fuzzy_v1 values, pass through
    unchanged. Only strings repeated in validated compact evidence, plus the
    event display fields, are pooled.
    """
    pending: list[tuple[int, int, list[Any]]] = []
    occurrences: Counter[str] = Counter()
    mandatory: set[str] = set()
    for row_index, row in enumerate(rows):
        candidates = row.get("history_review_candidates")
        if not isinstance(candidates, list):
            continue
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict) or set(candidate) != {_PENDING_KEY}:
                continue
            value = candidate.get(_PENDING_KEY)
            if not isinstance(value, list) or len(value) != 7:
                raise ValueError("invalid pending history candidate")
            identity, provenance, match, rank, warehouse, counterparty, flags = value
            if (
                not isinstance(identity, list) or len(identity) != 10
                or not isinstance(provenance, list) or len(provenance) != 10
                or not isinstance(match, list) or len(match) != 4
                or isinstance(rank, bool) or not isinstance(rank, int)
                or warehouse is not None and not isinstance(warehouse, str)
                or counterparty is not None and not isinstance(counterparty, str)
                or isinstance(flags, bool) or not isinstance(flags, int) or flags not in range(4)
            ):
                raise ValueError("invalid pending history candidate")
            for text, limit in ((warehouse, MAX_WAREHOUSE_DISPLAY_BYTES), (counterparty, MAX_COUNTERPARTY_DISPLAY_BYTES)):
                if text is not None and len(text.encode("utf-8")) > limit:
                    raise ValueError("unbounded history display provenance")
                if text:
                    occurrences[text] += 1
                    mandatory.add(text)

            def count_strings(item: Any) -> None:
                if isinstance(item, str):
                    occurrences[item] += 1
                elif isinstance(item, list):
                    for nested in item:
                        count_strings(nested)
                elif isinstance(item, dict):
                    for nested in item.values():
                        count_strings(nested)

            count_strings(identity)
            count_strings(provenance)
            count_strings(match)
            pending.append((row_index, candidate_index, value))

    pooled_values = sorted(
        text for text, count in occurrences.items()
        if text in mandatory or _POOL_REF_RE.fullmatch(text)
        or (count >= 2 and len(text.encode("utf-8")) >= 8)
    )
    pool_index = {text: index for index, text in enumerate(pooled_values)}

    def encode_strings(value: Any) -> Any:
        if isinstance(value, str) and value in pool_index:
            return _pool_ref(pool_index[value])
        if isinstance(value, list):
            return [encode_strings(item) for item in value]
        if isinstance(value, dict):
            return {key: encode_strings(item) for key, item in value.items()}
        return value

    output = [dict(row) for row in rows]
    for row in output:
        candidates = row.get("history_review_candidates")
        if isinstance(candidates, list):
            row["history_review_candidates"] = list(candidates)
    for row_index, candidate_index, value in pending:
        identity, provenance, match, rank, warehouse, counterparty, flags = value
        row = output[row_index]
        candidates = row["history_review_candidates"]
        candidates[candidate_index] = {
            HISTORY_CANDIDATE_V2_KEY: [
                encode_strings(identity), encode_strings(provenance), encode_strings(match), rank,
                pool_index[warehouse] if warehouse else None,
                pool_index[counterparty] if counterparty else None,
                flags,
            ]
        }
    return output, pooled_values


def _decode_pool_refs(value: Any, pool: list[str]) -> Any:
    if isinstance(value, str):
        index = _pool_index(value)
        if index is not None:
            if index >= len(pool):
                raise ValueError("invalid history text pool reference")
            return pool[index]
        return value
    if isinstance(value, dict):
        return {key: _decode_pool_refs(item, pool) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_pool_refs(item, pool) for item in value]
    return value


def decode_history_candidate_v2(candidate: Any, pool: Any) -> dict[str, Any] | None:
    """Decode persisted v2 into a v1-shaped compact candidate plus display facts."""
    if not isinstance(candidate, dict) or set(candidate) != {HISTORY_CANDIDATE_V2_KEY}:
        return None
    value = candidate[HISTORY_CANDIDATE_V2_KEY]
    if not isinstance(pool, list) or any(not isinstance(item, str) for item in pool):
        raise ValueError("invalid history text pool")
    if not isinstance(value, list) or len(value) != 7:
        raise ValueError("invalid history candidate v2")
    identity, provenance, match, rank, warehouse_index, counterparty_index, flags = value
    if (
        not isinstance(identity, list) or len(identity) != 10
        or not isinstance(provenance, list) or len(provenance) != 10
        or not isinstance(match, list) or len(match) != 4
        or isinstance(rank, bool) or not isinstance(rank, int)
        or isinstance(flags, bool) or not isinstance(flags, int) or flags not in range(4)
    ):
        raise ValueError("invalid history candidate v2")
    def pool_text(index: Any) -> str | None:
        if index is None:
            return None
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(pool):
            raise ValueError("invalid history text pool reference")
        value = pool[index]
        return value if value else None

    warehouse = pool_text(warehouse_index)
    counterparty = pool_text(counterparty_index)
    for text, limit in (
        (warehouse, MAX_WAREHOUSE_DISPLAY_BYTES),
        (counterparty, MAX_COUNTERPARTY_DISPLAY_BYTES),
    ):
        if text is not None:
            try:
                if len(text.encode("utf-8")) > limit:
                    raise ValueError("unbounded history display provenance")
            except UnicodeEncodeError as exc:
                raise ValueError("invalid history display provenance") from exc

    return {
        "fuzzy_v1": {
            "classification": "FUZZY", "provider": "1c",
            "identity": _decode_pool_refs(identity, pool),
            "provenance": _decode_pool_refs(provenance, pool),
            "match": _decode_pool_refs(match, pool), "rank": rank,
        },
        "warehouse": warehouse,
        "counterparty": counterparty,
        "warehouse_truncated": bool(flags & 1),
        "counterparty_truncated": bool(flags & 2),
    }


__all__ = [
    "HISTORY_CANDIDATE_V2_KEY", "HISTORY_TEXT_POOL_KEY",
    "MAX_WAREHOUSE_DISPLAY_BYTES", "MAX_COUNTERPARTY_DISPLAY_BYTES",
    "bounded_utf8_text", "pending_history_candidate_v2",
    "encode_history_candidate_rows", "decode_history_candidate_v2",
]
