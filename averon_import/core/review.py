from __future__ import annotations

CRITICAL_FIELDS = ("quantity", "unit", "type_mark", "code")


def critical_confidence(values: dict[str, str], confidences: dict[str, float]) -> float:
    """Return the weakest confidence among populated critical fields.

    The value is intentionally separate from the row average: one uncertain
    quantity/article/model must remain visible even when descriptive text is
    recognized with high confidence.
    """
    present = [
        float(confidences.get(key, 0.0) or 0.0)
        for key in CRITICAL_FIELDS
        if str(values.get(key, "")).strip()
    ]
    return round(min(present), 1) if present else 0.0
