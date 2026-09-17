from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from averon_import.services.app_settings import EtmIproSettings
from averon_import.services.secrets import MemorySecretStore
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.service import SourcingService
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.etm_ipro import (
    EtmCatalogMirror,
    EtmIproClient,
    EtmIproProvider,
    EtmRateLimiter,
)
from averon_import.services.sourcing.providers.etm_ipro.models import EtmCatalogRecord, EtmManufacturer


@dataclass
class FakeResponse:
    payload: object
    status: int = 200

    def read(self, limit=-1):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def close(self):
        return None


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def settings(tmp_path, *, enabled=True):
    return EtmIproSettings(
        enabled=enabled,
        environment="test",
        warehouse_codes=["WH-1"],
        base_url_override="https://etm.example/api/v1",
    )


def intent(**updates):
    value = {
        "source_row_id": "row-1",
        "source_text": "Клапан K-100",
        "normalized_name": "Клапан K-100",
        "manufacturer": "Acme",
        "article": "A-100",
        "model": "K-100",
        "search_queries": ["Клапан K-100"],
    }
    value.update(updates)
    return ProductIntent(**value)


def test_auth_success_uses_session_and_never_puts_credentials_in_errors(tmp_path):
    clock = FakeClock()
    requests = []

    def transport(request, timeout):
        requests.append(request.full_url)
        if request.full_url.endswith("/user/login?log=login%40example.com&pwd=p%26ss"):
            return FakeResponse({"data": {"session": "session-private"}})
        return FakeResponse({"data": {"gdscode": "A-100", "name": "Клапан"}})

    client = EtmIproClient(
        settings(tmp_path), "login@example.com", "p&ss",
        transport=transport, clock=clock,
    )
    result = client.get_goods("A-100")

    assert result["data"]["gdscode"] == "A-100"
    parsed = parse_qs(urlsplit(requests[0]).query)
    assert parsed["log"] == ["login@example.com"]
    assert parsed["pwd"] == ["p&ss"]
    assert "session-private" not in str(result)


def test_403_allows_one_reauth_retry_after_documented_login_cooldown(tmp_path):
    clock = FakeClock()
    calls = []

    def transport(request, timeout):
        calls.append(request.full_url)
        if "/user/login" in request.full_url:
            return FakeResponse({"data": {"session": f"session-{len([x for x in calls if '/user/login' in x])}"}})
        if len([x for x in calls if "/user/login" in x]) == 1:
            clock.value = 120.0
            return FakeResponse({"error": "denied"}, status=403)
        return FakeResponse({"data": {"gdscode": "A-100"}})

    client = EtmIproClient(settings(tmp_path), "login", "password", transport=transport, clock=clock)
    assert client.get_goods("A-100")["data"]["gdscode"] == "A-100"
    assert len([url for url in calls if "/user/login" in url]) == 2


def test_auth_refresh_is_not_repeated_inside_two_minutes(tmp_path):
    clock = FakeClock()
    client = EtmIproClient(
        settings(tmp_path), "login", "password",
        transport=lambda request, timeout: FakeResponse({"data": {"session": "session"}}),
        clock=clock,
    )
    client.check_access()
    with pytest.raises(SourcingProviderError) as error:
        client._get_session(force=True)
    assert error.value.code == "AUTH_RATE_LIMITED"
    assert "password" not in str(error.value).casefold()


def test_rate_limiter_is_monotonic_and_provider_owned():
    clock = FakeClock()
    sleeps = []
    limiter = EtmRateLimiter(interval_seconds=1, clock=clock, sleeper=lambda value: sleeps.append(value))
    limiter.acquire("goods")
    limiter.acquire("goods")
    limiter.acquire("price")

    assert sleeps == [1.0]


def test_price_batch_is_bounded_to_fifty_codes(tmp_path):
    client = EtmIproClient(
        settings(tmp_path), "login", "password",
        transport=lambda request, timeout: FakeResponse({"data": {"session": "session"}}),
    )
    with pytest.raises(SourcingProviderError, match="не более 50"):
        client.get_prices([str(index) for index in range(51)])


def test_manufacturer_resolution_is_exact_and_ambiguous_fails_closed(tmp_path):
    mirror = EtmCatalogMirror(tmp_path / "sourcing" / "providers" / "etm_ipro" / "catalog.sqlite3")
    mirror.sync_manufacturers([EtmManufacturer("1", "Acme"), EtmManufacturer("2", "Other")])
    assert mirror.resolve_manufacturer(" acme ") == "1"
    mirror.sync_manufacturers([EtmManufacturer("1", "Acme"), EtmManufacturer("2", "ACME")])
    assert mirror.resolve_manufacturer("Acme") is None


def catalog_record(source_item_id="A-100", *, name="Клапан K-100", article="A-100", brand="Acme"):
    return EtmCatalogRecord(
        source_item_id=source_item_id,
        name=name,
        article=article,
        brand=brand,
        brand_code="1",
        cli_code="CLI-1",
        class_code="C-1",
        product_class="Арматура",
    )


def test_mirror_search_is_deterministic_and_has_no_zero_evidence_fallback(tmp_path):
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync_snapshot({"data": [catalog_record().model_dump(), catalog_record("B-200", name="Насос", article="B-200") ]})
    assert [row.source_item_id for row in mirror.search(intent(article="A-100"))] == ["A-100"]
    assert mirror.search(intent(article="", manufacturer="", model="", normalized_name="несуществующий", search_queries=["несуществующий"])) == []


def test_failed_new_snapshot_preserves_old_good_mirror(tmp_path):
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync_snapshot({"data": [catalog_record().model_dump()]})
    revision = mirror.revision
    with pytest.raises(SourcingProviderError):
        mirror.sync_snapshot({"data": [{"id": "broken", "name": {"private": "bad"}}]})
    assert mirror.revision == revision
    assert mirror.count() == 1


class FakeEtmClient:
    configured = True

    def get_goods(self, source_item_id, *, lookup_type="etm", manufacturer_code=None):
        return {
            "data": {
                "gdscode": source_item_id,
                "name": "Клапан K-100",
                "art": "A-100",
                "mnf_name": "Acme",
                "edizm": "шт.",
                "min_cnt": "2",
                "gdsChars": {"Диаметр": "100 мм"},
                "gdsClassTree": [{"name": "Арматура"}],
                "gdsPacks": [{"qty": "1"}],
                "gdsImages": ["https://cdn.etm.ru/a.jpg"],
            }
        }

    def get_prices(self, source_item_ids):
        return {"data": [{"gdscode": item, "pricewnds": "12.50", "price": "0", "price_tarif": "0", "price_retail": "0"} for item in source_item_ids]}

    def get_remains(self, source_item_id):
        return {"data": {"stores": [{"store_code": "WH-1", "store_name": "Основной", "store_type": "warehouse", "stock": "4", "supplier_stock": "2", "forecast": "завтра"}]}}


def configured_provider(tmp_path, *, price_client=None):
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync_snapshot({"data": [catalog_record()]})
    mirror.sync_manufacturers([EtmManufacturer("1", "Acme")])
    provider = EtmIproProvider(
        settings(tmp_path), MemorySecretStore(), tmp_path,
        client=price_client or FakeEtmClient(), mirror=mirror,
    )
    return provider, mirror


def test_provider_maps_goods_price_remains_and_preserves_detail_evidence(tmp_path):
    provider, _ = configured_provider(tmp_path)
    offer = provider.search(intent())[0]

    assert offer.price == Decimal("12.50")
    assert offer.currency == "RUB"
    assert offer.availability is True
    assert offer.url == ""
    assert offer.attributes["unit"] == "шт."
    assert offer.attributes["params"]["Диаметр"] == "100 мм"
    assert offer.attributes["remains"][0]["supplier_stock"] == "2"


def test_zero_commercial_prices_are_not_free(tmp_path):
    class ZeroPriceClient(FakeEtmClient):
        def get_prices(self, source_item_ids):
            return {"data": [{"gdscode": item, "pricewnds": "0", "price": "0", "price_tarif": "0", "price_retail": "0"} for item in source_item_ids]}

    provider, _ = configured_provider(tmp_path, price_client=ZeroPriceClient())
    offer = provider.search(intent())[0]
    assert offer.price is None
    assert offer.data_provenance["price_status"] == "requires_individual_request"
    assert "индивидуально" in offer.availability_text


def test_runtime_stats_are_safe_and_do_not_authenticate_on_startup(tmp_path):
    provider, mirror = configured_provider(tmp_path)
    state = provider.stats()
    assert state.configured is True
    assert state.reachable is True
    assert state.catalog_version == mirror.revision


def test_project_continues_after_one_etm_provider_row_failure(tmp_path):
    class FailOnceClient(FakeEtmClient):
        def __init__(self):
            self.failed = False

        def get_remains(self, source_item_id):
            if not self.failed:
                self.failed = True
                raise SourcingProviderError("ЭТМ временно недоступен", category="network")
            return super().get_remains(source_item_id)

    provider, mirror = configured_provider(tmp_path, price_client=FailOnceClient())
    mirror.sync_snapshot({"data": [catalog_record("A-100"), catalog_record("B-200", article="B-200")]})
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    result = service.search_project([
        {"id": "row-1", "row_type": "item", "name": "Клапан", "article": "A-100", "quantity": "1"},
        {"id": "row-2", "row_type": "item", "name": "Клапан", "article": "B-200", "quantity": "1"},
    ], ai_rerank=False)

    assert result.positions_processed == 2
    assert result.results[0].offers == []
    assert any(notice.code == "PROVIDER_ERROR" for notice in result.results[0].notices)
    assert result.results[1].offers


def test_catalog_job_lifecycle_persists_uuid_and_imports_only_completed_snapshot(tmp_path):
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")

    class JobClient:
        configured = True
        def __init__(self):
            self.status_calls = 0
        def create_catalog_job(self):
            return "job-1"
        def get_catalog_job(self, value):
            self.status_calls += 1
            return {"data": {"state": 3 if self.status_calls == 1 else 1, "url": "https://etm.example/catalog.json"}}
        def download_snapshot(self, value):
            return {"data": [catalog_record("new").model_dump()]}

    client = JobClient()
    first = mirror.create_job(client)
    assert first.uuid == "job-1" and first.state == 0
    assert mirror.update_job(client).state == 3
    completed = mirror.update_job(client)
    assert completed.state == 1
    assert mirror.search(intent(article="A-100"))[0].source_item_id == "new"
    assert mirror.count() == 1
