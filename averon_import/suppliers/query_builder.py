from __future__ import annotations

import re
from typing import Any

from averon_import.suppliers.models import ProductQuery


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_product_query(row: dict[str, Any]) -> ProductQuery:
    """Build a deterministic search query from a verified/recognized row.

    No AI-generated attributes are invented here. Later providers may enrich
    the query, but the baseline always remains reproducible from row fields.
    """
    name = _clean(row.get("name"))
    manufacturer = _clean(row.get("manufacturer"))
    model = _clean(row.get("type_mark"))
    article = _clean(row.get("code"))
    parts = []
    for value in (manufacturer, model, article, name):
        if value and value.lower() not in {item.lower() for item in parts}:
            parts.append(value)
    return ProductQuery(
        row_id=str(row.get("id") or ""),
        search_text=" ".join(parts),
        name=name,
        manufacturer=manufacturer,
        model=model,
        article=article,
        attributes={},
    )
