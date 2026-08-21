from __future__ import annotations

from typing import Protocol

from averon_import.suppliers.models import ProductOffer, ProductQuery


class SupplierAdapter(Protocol):
    """One supported supplier/site integration."""

    @property
    def supplier_id(self) -> str:
        ...

    def search(self, query: ProductQuery) -> list[ProductOffer]:
        ...
