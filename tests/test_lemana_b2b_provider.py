from __future__ import annotations

from decimal import Decimal

import pytest

from averon_import.services.app_settings import LemanaB2BSettings
from averon_import.services.secrets import LEMANA_B2B_CLIENT_SECRET, MemorySecretStore
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.lemana_b2b import (
    LemanaB2BProvider,
    LemanaCatalogMirror,
    LemanaPriceRecord,
    LemanaProductRecord,
    LemanaProductsPage,
)
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.service import SourcingService


def product(
    item: str = "82331508",
    *,
    name: str = "Клапан шаровый",
    available: bool | None = True,
    url: str = "https://example.test/p/1",
):
    return LemanaProductRecord(
        product_item=item,
        product_available=available,
        product_name=name,
        product_description="Стальной клапан",
        product_url=url,
        product_model="K-100",
        product_brand="Brand",
        product_barcode="4600000000000",
        product_params={"diameter": "100"},
        product_unit_sale={"name": "шт."},
        categories=[{"id": 10}],
    )


def intent() -> ProductIntent:
    return ProductIntent(
        source_row_id="row-1",
        source_text="Клапан K-100",
        normalized_name="Клапан",
        model="K-100",
        search_queries=["Клапан", "K-100"],
    )


class ProviderClient:
    configured = True

    def __init__(self, prices=(), *, access_error=None):
        self.prices = tuple(prices)
        self.access_error = access_error
        self.access_calls = 0
        self.price_calls: list[tuple] = []

    def check_access(self):
        self.access_calls += 1
        if self.access_error:
            raise self.access_error
        return True

    def get_prices(self, product_items, *, region_id):
        self.price_calls.append((tuple(product_items), region_id))
        return self.prices


def settings() -> LemanaB2BSettings:
    return LemanaB2BSettings(
        enabled=True,
        environment="test",
        client_id="client-1",
        region_id=34,
    )


def configured_provider(tmp_path, *, prices=(), mirror=None, client=None):
    store = MemorySecretStore()
    store.set(LEMANA_B2B_CLIENT_SECRET, "client-secret")
    mirror = mirror or LemanaCatalogMirror(tmp_path / "catalog.sqlite3")
    client = client or ProviderClient(prices)
    provider = LemanaB2BProvider(
        settings(), store, tmp_path, client=client, mirror=mirror
    )
    return provider, client, mirror


def test_unconfigured_provider_stats_fail_closed_without_network(tmp_path):
    settings_value = LemanaB2BSettings(enabled=False, client_id="", region_id=None)
    store = MemorySecretStore()
    provider = LemanaB2BProvider(settings_value, store, tmp_path)

    state = provider.stats()

    assert state.configured is False
    assert state.reachable is False
    assert state.item_count == 0
    assert "не настроен" in state.error


def test_configured_provider_exposes_mirror_revision_and_auth_health(tmp_path):
    provider, client, mirror = configured_provider(tmp_path)
    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(products=(product(),), page=1, per_page=100, total_count=1)

    mirror.sync(SyncClient(), region_id=34)
    state = provider.stats()

    assert state.configured is True
    assert state.reachable is True
    assert state.item_count == 1
    assert state.catalog_version == mirror.revision
    assert client.access_calls == 1


def test_provider_search_uses_local_candidates_and_bounded_live_batch_prices(tmp_path):
    price = LemanaPriceRecord("82331508", Decimal("796.14"), "Rub")
    provider, client, mirror = configured_provider(tmp_path, prices=[price])

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(products=(product(),), page=1, per_page=100, total_count=1)

    mirror.sync(SyncClient(), region_id=34)
    offers = provider.search(intent(), limit=5)

    assert len(offers) == 1
    offer = offers[0]
    assert offer.offer_id == "lemana_b2b:82331508"
    assert offer.provider == "lemana_b2b"
    assert offer.source_item_id == "82331508"
    assert offer.article == "82331508"
    assert offer.price == Decimal("796.14")
    assert offer.currency == "RUB"
    assert offer.availability is True
    assert offer.url == "https://example.test/p/1"
    assert offer.attributes["model"] == "K-100"
    assert offer.data_provenance == {
        "source": "lemana_b2b",
        "product_item": "82331508",
        "mirror_revision": mirror.revision,
        "region_id": 34,
    }
    assert client.price_calls == [(('82331508',), 34)]


def test_price_missing_keeps_null_price_and_supplier_availability(tmp_path):
    provider, _, mirror = configured_provider(tmp_path, prices=[])

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(products=(product(available=False),), page=1, per_page=100, total_count=1)

    mirror.sync(SyncClient(), region_id=34)
    offer = provider.search(intent())[0]

    assert offer.price is None
    assert offer.currency == ""
    assert offer.availability is False
    assert offer.availability_text == "Нет в наличии"


def test_invalid_supplier_url_is_not_exposed_as_clickable_offer_url(tmp_path):
    provider, _, mirror = configured_provider(tmp_path, prices=[])

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(
                products=(product(url="javascript:alert(1)"),),
                page=1,
                per_page=100,
                total_count=1,
            )

    mirror.sync(SyncClient(), region_id=34)
    assert provider.search(intent())[0].url == ""


def test_configured_provider_without_mirror_is_not_reported_healthy(tmp_path):
    provider, client, _ = configured_provider(tmp_path)

    state = provider.stats()

    assert state.configured is True
    assert state.reachable is False
    assert "зеркало" in state.error.casefold()
    assert client.access_calls == 0


def test_region_affinity_mismatch_blocks_health_and_search_before_auth(tmp_path):
    provider, client, mirror = configured_provider(tmp_path)

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(products=(product(),), page=1, per_page=100, total_count=1)

    mirror.sync(SyncClient(), region_id=34, environment="test")
    provider.settings.region_id = 35

    state = provider.stats()

    assert state.configured is True
    assert state.reachable is False
    assert "окружения и региона" in state.error
    assert client.access_calls == 0
    with pytest.raises(SourcingProviderError, match="окружения и региона"):
        provider.search(intent())
    assert client.access_calls == 0


def test_environment_affinity_mismatch_blocks_health_and_sync_restores_it(tmp_path):
    provider, client, mirror = configured_provider(tmp_path)

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(products=(product(),), page=1, per_page=100, total_count=1)

    mirror.sync(SyncClient(), region_id=34, environment="test")
    provider.settings.environment = "prod"

    state = provider.stats()

    assert state.configured is True
    assert state.reachable is False
    assert "окружения и региона" in state.error
    assert client.access_calls == 0

    client.get_products = SyncClient().get_products
    provider.sync()
    restored = provider.stats()
    assert restored.reachable is True
    assert mirror.environment == "prod"
    assert mirror.region_id == 34
    assert client.access_calls == 1


def test_project_continues_after_one_lemana_price_failure(tmp_path):
    class FailOnceClient(ProviderClient):
        def __init__(self):
            super().__init__([LemanaPriceRecord("2", Decimal("20"), "Rub")])
            self.failed = False

        def get_prices(self, product_items, *, region_id):
            if not self.failed:
                self.failed = True
                raise SourcingProviderError(
                    "internal body token private",
                    category="network",
                )
            return super().get_prices(product_items, region_id=region_id)

    mirror = LemanaCatalogMirror(tmp_path / "catalog.sqlite3")

    class SyncClient(ProviderClient):
        def get_products(self, **kwargs):
            return LemanaProductsPage(
                products=(product("1"), product("2")),
                page=1,
                per_page=100,
                total_count=2,
            )

    mirror.sync(SyncClient(), region_id=34)
    provider, client, _ = configured_provider(
        tmp_path,
        mirror=mirror,
        client=FailOnceClient(),
    )
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        cache=SourcingCache(tmp_path / "cache.json"),
    )

    result = service.search_project(
        [
            {"id": "row-1", "row_type": "item", "name": "Клапан K-100"},
            {"id": "row-2", "row_type": "item", "name": "Клапан K-100"},
        ],
        ai_rerank=False,
    )

    assert result.positions_processed == 2
    assert result.results[0].offers == []
    assert any(notice.code == "PROVIDER_ERROR" for notice in result.results[0].notices)
    assert result.results[1].offers
    assert client.failed is True


def test_sourcing_cache_key_uses_mirror_revision(tmp_path):
    price = LemanaPriceRecord("82331508", Decimal("10"), "Rub")
    provider, client, mirror = configured_provider(tmp_path, prices=[price])

    class SyncClient(ProviderClient):
        def __init__(self, current_product):
            super().__init__()
            self.current_product = current_product

        def get_products(self, **kwargs):
            return LemanaProductsPage(
                products=(self.current_product,), page=1, per_page=100, total_count=1
            )

    mirror.sync(SyncClient(product()), region_id=34)
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        cache=SourcingCache(tmp_path / "cache.json"),
    )

    first = service.search_intent(intent(), ai_rerank=False)
    second = service.search_intent(intent(), ai_rerank=False)
    old_revision = mirror.revision
    mirror.sync(SyncClient(product(name="Клапан обновлён")), region_id=34)
    third = service.search_intent(intent(), ai_rerank=False)

    assert first.timings["search_cache_hit"] == 0.0
    assert second.timings["search_cache_hit"] == 1.0
    assert mirror.revision != old_revision
    assert third.timings["search_cache_hit"] == 0.0
    assert client.price_calls
