from __future__ import annotations

from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
)
from averon_import.services.sourcing.providers.base import SourcingProviderCachePolicy


class LocalCatalogProvider:
    key = "local_catalog"
    label = "Локальный каталог"
    cache_policy = SourcingProviderCachePolicy()
    capabilities = SourcingProviderCapabilities(
        supports_price=True,
        supports_availability=True,
        supports_product_url=True,
        supports_article_search=True,
        supports_model_search=True,
        supports_catalog_version=True,
    )

    def __init__(self, repository: CatalogRepository):
        self.repository = repository

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        return [
            offer.model_copy(update={"provider": self.key})
            for offer in self.repository.search(intent, limit=limit)
        ]

    def stats(self) -> dict:
        return self.repository.stats()


# Compatibility import for integrations that historically imported the
# placeholder from this module.  The implementation is provider-owned.
from averon_import.services.sourcing.providers.lemana_b2b.provider import LemanaB2BProvider
