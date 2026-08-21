from __future__ import annotations

from averon_import.suppliers.base import SupplierAdapter


class SupplierRegistry:
    def __init__(self, adapters: list[SupplierAdapter] | None = None):
        self._adapters: dict[str, SupplierAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: SupplierAdapter) -> None:
        supplier_id = adapter.supplier_id.strip()
        if not supplier_id:
            raise ValueError("Supplier adapter must have a non-empty supplier_id")
        if supplier_id in self._adapters:
            raise ValueError(f"Supplier adapter already registered: {supplier_id}")
        self._adapters[supplier_id] = adapter

    def get(self, supplier_id: str) -> SupplierAdapter:
        try:
            return self._adapters[supplier_id]
        except KeyError as exc:
            raise KeyError(f"Unknown supplier: {supplier_id}") from exc

    def ids(self) -> list[str]:
        return sorted(self._adapters)
