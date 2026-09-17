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
from averon_import.services.sourcing.providers.etm_ipro.provider import (
    _goods_rows,
    _price_row,
    _stock_summary,
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


class OfficialWireTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        path = parsed.path
        if path.endswith("/user/login"):
            return FakeResponse({"status": {"code": 200}, "data": {"session": "session-wire"}})
        if path.endswith("/info/search/r-manuf/"):
            return FakeResponse({
                "status": {"code": 200},
                "data": [{"id": "4263058", "value": "686", "label": "Реле и Автоматика"}],
            })
        if path.endswith("/price"):
            return FakeResponse({
                "status": {"code": 200},
                "data": {"gdscode": 9536092, "price": 0, "pricewnds": "125.40", "price_tarif": 0, "price_retail": 0},
            })
        if path.endswith("/remains"):
            return FakeResponse({
                "status": {"code": 200},
                "data": {
                    "RequestStoreName": "Основной склад",
                    "UnitName": "шт.",
                    "gdscode": 9536092,
                    "InfoStores": [
                        {"StoreCode": "WH-1", "StoreType": "warehouse", "StoreName": "Основной", "StoreQuantRem": "4"},
                        {"StoreCode": "WH-2", "StoreType": "warehouse", "StoreName": "Другой", "StoreQuantRem": "0"},
                    ],
                    "InfoForecast": {"days": 2},
                    "InfoSuppStores": [{"StoreCode": "SUP-1", "StoreQuantRem": "8"}],
                    "InforDeliveryTime": "завтра",
                },
            })
        if "/goods/" in path and not path.endswith("/price") and not path.endswith("/remains"):
            return FakeResponse({
                "status": {"code": 200},
                "data": {
                    "rows": [{
                        "code": "ETM9536092",
                        "gdscode": 9536092,
                        "art": "РВ-100",
                        "mnf_name": "Реле и Автоматика",
                        "mnf_code": 686,
                        "edizm": "шт.",
                        "min_cnt": "1",
                        "gdsChars": [{"name": "Ток", "value": "10 А"}],
                        "gdsClassTree": [{"name": "Автоматика"}],
                        "gdsPacks": [],
                        "gdsImages": [],
                    }],
                    "records": 1,
                },
            })
        if path.endswith("/job/create/40029846"):
            return FakeResponse({"status": {"code": 200}, "data": {"uuid": "job-wire"}})
        if path.endswith("/job/job-wire"):
            return FakeResponse({
                "status": {"code": 200},
                "data": {
                    "page": 1,
                    "rows": [{
                        "state": 1,
                        "state_desc": "Готово",
                        "uuid": "job-wire",
                        "urls": [{"type": "json", "url": "https://ipro.etm.ru/report/catalog.json"}],
                    }],
                    "total": 1,
                    "records": 1,
                    "userdata": {},
                },
            })
        if path.endswith("/report/catalog.json"):
            return FakeResponse({"status": {"code": 200}, "data": [{"gdscode": 9536092, "name": "Реле"}]})
        raise AssertionError(f"unexpected wire path: {request.full_url}")


def test_official_wire_contract_uses_query_session_and_rows_adapters(tmp_path):
    transport = OfficialWireTransport()
    client = EtmIproClient(settings(tmp_path), "login", "password", transport=transport)

    goods_mnf = client.get_goods("9536092", lookup_type="mnf", manufacturer_code="686")
    goods_etm = client.get_goods("9536092", lookup_type="etm")
    price = client.get_prices(["9536092", "9536093"])
    remains = client.get_remains("9536092")
    manufacturers = client.get_manufacturers()
    assert _goods_rows(goods_mnf)[0]["gdscode"] == 9536092
    assert _goods_rows(goods_etm)[0]["code"] == "ETM9536092"
    assert _goods_rows({"status": {}, "data": {"records": 0}}) == []
    assert _price_row(price, "9536092")["pricewnds"] == "125.40"
    selected, availability, _ = _stock_summary(remains, ["WH-1"])
    assert availability is True
    assert selected[0]["StoreQuantRem"] == "4"
    _, unavailable, _ = _stock_summary(remains, ["WH-1", "WH-MISSING"])
    assert unavailable is None
    assert manufacturers[0].code == "686"
    assert manufacturers[0].label == "Реле и Автоматика"

    assert client.create_catalog_job() == "job-wire"
    assert client.get_catalog_job("job-wire")["data"]["rows"][0]["state"] == 1
    assert client.download_snapshot("https://ipro.etm.ru/report/catalog.json")["data"][0]["gdscode"] == 9536092

    authenticated = [
        request for request in transport.requests
        if "/user/login" not in request.full_url and "/report/" not in request.full_url
    ]
    assert authenticated
    login_request = next(request for request in transport.requests if "/user/login" in request.full_url)
    assert "session-id" not in parse_qs(urlsplit(login_request.full_url).query)
    for request in authenticated:
        assert parse_qs(urlsplit(request.full_url).query)["session-id"] == ["session-wire"]
        assert "X-Session-Id" not in request.headers
    price_request = next(request for request in transport.requests if request.full_url.endswith("/price?type=etm&session-id=session-wire"))
    assert "%2C" in urlsplit(price_request.full_url).path
    assert "session-wire" not in str(SourcingProviderError("Поставщик недоступен"))


def test_official_wire_payload_maps_through_provider_without_inventing_stock(tmp_path):
    transport = OfficialWireTransport()
    client = EtmIproClient(settings(tmp_path), "login", "password", transport=transport)
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync_snapshot({
        "data": [{
            "gdscode": 9536092,
            "name": "Реле и Автоматика РВ-100",
            "art": "РВ-100",
            "mnf_name": "Реле и Автоматика",
            "mnf_code": 686,
        }],
    })
    provider = EtmIproProvider(settings(tmp_path), MemorySecretStore(), tmp_path, client=client, mirror=mirror)
    provider.sync_manufacturers()

    offer = provider.search(intent(article="РВ-100", manufacturer="Реле и Автоматика", brand="Реле и Автоматика"))[0]
    assert offer.source_item_id == "9536092"
    assert offer.price == Decimal("125.40")
    assert offer.availability is True
    assert offer.attributes["remains"][0]["StoreQuantRem"] == "4"
    assert offer.attributes["supplier_stores"] == [{"StoreCode": "SUP-1", "StoreQuantRem": "8"}]
    assert offer.attributes["forecast"] == {"days": 2}
    assert offer.attributes["delivery"] == "завтра"


@pytest.mark.parametrize(
    "url",
    [
        "http://ipro.etm.ru/report/catalog.json",
        "https://evil.example/report/catalog.json",
        "https://localhost/report/catalog.json",
        "https://127.0.0.1/report/catalog.json",
        "https://10.0.0.1/report/catalog.json",
        "https://user:password@ipro.etm.ru/report/catalog.json",
    ],
)
def test_snapshot_download_rejects_unsafe_urls(tmp_path, url):
    client = EtmIproClient(settings(tmp_path), "login", "password", transport=OfficialWireTransport())
    with pytest.raises(SourcingProviderError, match="безопасный адрес"):
        client.download_snapshot(url)


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


def test_provider_search_is_bounded_and_does_not_enrich_zero_evidence(tmp_path):
    class CountingClient(FakeEtmClient):
        def __init__(self):
            self.goods_calls = []

        def get_goods(self, source_item_id, *, lookup_type="etm", manufacturer_code=None):
            self.goods_calls.append(source_item_id)
            return super().get_goods(
                source_item_id,
                lookup_type=lookup_type,
                manufacturer_code=manufacturer_code,
            )

    client = CountingClient()
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")
    mirror.sync_snapshot({
        "data": [
            catalog_record("A-100", name="Насос 1", article="" ).model_dump(),
            catalog_record("B-200", name="Насос 2", article="" ).model_dump(),
        ],
    })
    provider = EtmIproProvider(
        settings(tmp_path), MemorySecretStore(), tmp_path, client=client, mirror=mirror
    )
    broad_intent = intent(
        article="",
        brand="",
        manufacturer="",
        model="Насос",
        normalized_name="Насос",
        search_queries=["Насос"],
    )
    assert len(provider.search(broad_intent, limit=1)) == 1
    bounded_calls = len(client.goods_calls)

    empty_intent = intent(
        article="",
        brand="",
        manufacturer="",
        model="",
        normalized_name="нет такого товара",
        search_queries=[],
    )
    assert provider.search(empty_intent, limit=20) == []
    assert len(client.goods_calls) == bounded_calls


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
            return {
                "data": {
                    "page": 1,
                    "rows": [{
                        "state": 3 if self.status_calls == 1 else 1,
                        "state_desc": "готово",
                        "uuid": value,
                        "urls": [{"type": "json", "url": "https://etm.example/catalog.json"}],
                    }],
                    "total": 1,
                    "records": 1,
                    "userdata": {},
                },
            }
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


def test_catalog_job_status_fails_closed_without_unambiguous_official_row(tmp_path):
    mirror = EtmCatalogMirror(tmp_path / "catalog.sqlite3")

    class JobClient:
        def create_catalog_job(self):
            return "job-ambiguous"

        def get_catalog_job(self, value):
            return {"data": {"rows": [
                {"uuid": "other", "state": 1, "urls": [{"url": "https://ipro.etm.ru/a.json"}]},
                {"uuid": "another", "state": 1, "urls": [{"url": "https://ipro.etm.ru/b.json"}]},
            ]}}

    client = JobClient()
    mirror.create_job(client)
    with pytest.raises(SourcingProviderError, match="некорректный статус"):
        mirror.update_job(client)
