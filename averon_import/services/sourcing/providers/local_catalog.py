from __future__ import annotations

from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.models import Offer, ProductIntent


class LocalCatalogProvider:
    key = "local_catalog"
    label = "Локальный каталог"

    def __init__(self, repository: CatalogRepository):
        self.repository = repository

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        return [
            offer.model_copy(update={"provider": self.key})
            for offer in self.repository.search(intent, limit=limit)
        ]

    def stats(self) -> dict:
        return self.repository.stats()


class LemanaB2BProvider:
    """Future adapter boundary; network integration is intentionally absent."""

    key = "lemana"
    label = "Lemana B2B"

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        raise RuntimeError("Lemana B2B provider is not configured")

    def stats(self) -> dict:
        return {"item_count": 0, "catalog_version": "not_configured"}
