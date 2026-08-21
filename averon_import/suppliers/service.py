from __future__ import annotations

from typing import Any

from averon_import.suppliers.matcher import ProductMatcher
from averon_import.suppliers.models import ProductMatch
from averon_import.suppliers.query_builder import build_product_query
from averon_import.suppliers.registry import SupplierRegistry


class SupplierSearchService:
    def __init__(
        self,
        registry: SupplierRegistry | None = None,
        matcher: ProductMatcher | None = None,
    ):
        self.registry = registry or SupplierRegistry()
        self.matcher = matcher or ProductMatcher()

    def search_row(
        self,
        row: dict[str, Any],
        supplier_ids: list[str] | None = None,
    ) -> list[ProductMatch]:
        query = build_product_query(row)
        ids = supplier_ids or self.registry.ids()
        matches: list[ProductMatch] = []
        for supplier_id in ids:
            adapter = self.registry.get(supplier_id)
            for offer in adapter.search(query):
                matches.append(self.matcher.match(query, offer))
        return sorted(
            matches,
            key=lambda item: (
                bool(item.hard_conflicts),
                -item.score,
                item.offer.supplier,
                item.offer.title,
            ),
        )
