from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import unquote, urlsplit

import pytest

from averon_import.services.app_settings import EtmIproSettings, LemanaB2BSettings
from averon_import.services.secrets import MemorySecretStore
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.contracts import (
    ProviderFailureCategory,
    ProviderSelection,
)
from averon_import.services.sourcing.providers.etm_ipro.client import (
    EtmIproClient,
    EtmRateLimiter,
)
from averon_import.services.sourcing.providers.etm_ipro.models import EtmCatalogRecord
from averon_import.services.sourcing.providers.etm_ipro.provider import EtmIproProvider
from averon_import.services.sourcing.providers.execution import ProviderExecutionScope, ProviderRunner
from averon_import.services.sourcing.providers.execution_adapters import (
    EtmIproExecutionAdapter,
    LemanaB2BExecutionAdapter,
    LocalCatalogExecutionAdapter,
    failure_category_for_sourcing_error,
)
from averon_import.services.sourcing.providers.lemana_b2b.client import LemanaB2BClient
from averon_import.services.sourcing.providers.lemana_b2b.models import (
    LemanaPriceRecord,
    LemanaProductRecord,
)
from averon_import.services.sourcing.providers.lemana_b2b.provider import LemanaB2BProvider


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def close(self) -> None:
        return None


def _run(adapter, intent: ProductIntent, *, limit: int = 5):
    result = ProviderRunner({adapter.key: adapter}).run(
        selection=ProviderSelection(provider_keys=(adapter.key,)),
        intent=intent,
        limit=limit,
        scope=ProviderExecutionScope(execution_scope_id="m1b-test-run"),
    )
    return result.outcomes[0]


def _offer_dump(offers):
    return [offer.model_dump(mode="json", exclude={"retrieved_at"}) for offer in offers]


def _intent(**updates) -> ProductIntent:
    value = {
        "source_row_id": "row-1",
        "source_text": "Valve K-100",
        "normalized_name": "Valve K-100",
        "model": "K-100",
        "search_queries": ["Valve K-100"],
    }
    value.update(updates)
    return ProductIntent(**value)


def test_local_adapter_matches_legacy_search_and_never_probes_stats(tmp_path):
    repository = CatalogRepository(tmp_path / "local.sqlite3")
    repository.bulk_upsert(
        [
            Offer(
                offer_id="fixture:local-1",
                provider="fixture",
                source_item_id="local-1",
                title="Valve K-100",
                article="K-100",
                manufacturer="Acme",
                price=Decimal("12.50"),
                currency="RUB",
            ),
            Offer(
                offer_id="fixture:local-2",
                provider="fixture",
                source_item_id="local-2",
                title="Valve K-101",
                article="K-101",
                manufacturer="Acme",
            ),
        ]
    )
    from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider

    legacy = LocalCatalogProvider(repository)
    legacy.stats = lambda: (_ for _ in ()).throw(AssertionError("stats must not run"))
    expected = legacy.search(_intent(), limit=5)
    outcome = _run(LocalCatalogExecutionAdapter(legacy), _intent(), limit=5)

    assert outcome.state == "success"
    assert outcome.request_count == 0
    assert _offer_dump(outcome.offers) == _offer_dump(expected)
    assert [offer.provider for offer in outcome.offers] == ["local_catalog"] * len(expected)


class FakeLemanaMirror:
    environment = "test"
    region_id = 34
    revision = "lemana-rev-1"

    def __init__(self, products=(), *, has_catalog=True):
        self.products = tuple(products)
        self.has_catalog = has_catalog

    def has_content(self):
        return self.has_catalog

    def search(self, intent, *, limit):
        return list(self.products[:limit])


def _lemana_product(item: str = "82331508") -> LemanaProductRecord:
    return LemanaProductRecord(
        product_item=item,
        product_available=True,
        product_name="Valve K-100",
        product_description="Steel valve",
        product_url="https://supplier.example/products/82331508",
        product_model="K-100",
        product_brand="Acme",
        product_unit_sale={"name": "шт."},
    )


class LemanaTransport:
    def __init__(self, statuses=()):
        self.calls = []
        self.statuses = list(statuses)

    def __call__(self, request, timeout):
        self.calls.append(request)
        parsed = urlsplit(request.full_url)
        if parsed.path.endswith("/token"):
            return FakeResponse(
                {"access_token": "fixture-token", "expires_in": 3600, "token_type": "Bearer"}
            )
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return FakeResponse({}, status=status)
        return FakeResponse(
            {
                "productPrice": [
                    {
                        "productItem": "82331508",
                        "salesPrices": [{"salesPrice": "796.14", "currencyName": "RUB"}],
                    }
                ]
            }
        )


def _lemana_provider(transport, *, mirror=None, configured=True):
    settings = LemanaB2BSettings(
        enabled=configured,
        environment="test",
        client_id="client-1",
        region_id=34,
    )
    client = LemanaB2BClient(settings, "client-secret" if configured else "", transport=transport)
    provider = LemanaB2BProvider(
        settings,
        MemorySecretStore(),
        ".",
        client=client,
        mirror=mirror or FakeLemanaMirror((_lemana_product(),)),
    )
    return provider, client


def test_lemana_warm_and_cold_token_counts_match_transport_attempts():
    warm_transport = LemanaTransport()
    warm_provider, warm_client = _lemana_provider(warm_transport)
    warm_client.check_access()
    warm_transport.calls.clear()
    warm = _run(LemanaB2BExecutionAdapter(warm_provider), _intent())
    assert warm.state == "success"
    assert warm.request_count == len(warm_transport.calls) == 1

    cold_transport = LemanaTransport()
    cold_provider, _ = _lemana_provider(cold_transport)
    cold = _run(LemanaB2BExecutionAdapter(cold_provider), _intent())
    assert cold.state == "success"
    assert cold.request_count == len(cold_transport.calls) == 2
    assert urlsplit(cold_transport.calls[0].full_url).path.endswith("/token")


def test_lemana_401_refresh_counts_price_retry_and_auth_attempts():
    transport = LemanaTransport(statuses=[401])
    provider, client = _lemana_provider(transport)
    client.check_access()
    transport.calls.clear()

    outcome = _run(LemanaB2BExecutionAdapter(provider), _intent())

    assert outcome.state == "success"
    assert outcome.request_count == len(transport.calls) == 3
    assert [urlsplit(request.full_url).path.endswith("/token") for request in transport.calls] == [
        False,
        True,
        False,
    ]


def test_lemana_empty_mirror_and_local_failures_make_no_http_attempts():
    empty_transport = LemanaTransport()
    empty_provider, _ = _lemana_provider(
        empty_transport,
        mirror=FakeLemanaMirror((_lemana_product(),)),
    )
    empty_provider.mirror.search = lambda intent, *, limit: []
    empty = _run(LemanaB2BExecutionAdapter(empty_provider), _intent())
    assert empty.state == "empty"
    assert empty.request_count == 0
    assert empty_transport.calls == []

    mismatch_transport = LemanaTransport()
    mismatch_provider, _ = _lemana_provider(mismatch_transport)
    mismatch_provider.mirror.region_id = 35
    mismatch = _run(LemanaB2BExecutionAdapter(mismatch_provider), _intent())
    assert mismatch.state == "failure"
    assert mismatch.failure_category == ProviderFailureCategory.MISCONFIGURED
    assert mismatch.request_count == 0
    assert mismatch_transport.calls == []

    unconfigured_transport = LemanaTransport()
    unconfigured_provider, _ = _lemana_provider(unconfigured_transport, configured=False)
    unconfigured = _run(LemanaB2BExecutionAdapter(unconfigured_provider), _intent())
    assert unconfigured.state == "failure"
    assert unconfigured.request_count == 0
    assert unconfigured_transport.calls == []


def test_lemana_legacy_search_equivalence_uses_independent_transports():
    legacy_transport = LemanaTransport()
    legacy_provider, _ = _lemana_provider(legacy_transport)
    expected = legacy_provider.search(_intent(), limit=5)

    adapter_transport = LemanaTransport()
    adapter_provider, _ = _lemana_provider(adapter_transport)
    outcome = _run(LemanaB2BExecutionAdapter(adapter_provider), _intent())

    assert _offer_dump(outcome.offers) == _offer_dump(expected)
    assert outcome.request_count == len(adapter_transport.calls) == 2
    assert len(legacy_transport.calls) == 2


class FakeEtmMirror:
    revision = "etm-rev-1"
    search_index_revision = "etm-rev-1"

    def __init__(self, records=(), *, has_catalog=True, manufacturer_code="BRAND-1"):
        self.records = tuple(records)
        self.has_catalog = has_catalog
        self.manufacturer_code = manufacturer_code

    def has_content(self):
        return self.has_catalog

    def resolve_manufacturer(self, manufacturer):
        return self.manufacturer_code if manufacturer and self.manufacturer_code else None

    def search(self, intent, *, limit, manufacturer_code=None):
        return list(self.records[:limit])


def _etm_record(source_id: str) -> EtmCatalogRecord:
    return EtmCatalogRecord(
        source_item_id=source_id,
        name=f"Valve {source_id}",
        brand="Acme",
        article=f"A-{source_id}",
    )


class EtmTransport:
    def __init__(self, *, price_statuses=()):
        self.calls = []
        self.price_statuses = list(price_statuses)

    def __call__(self, request, timeout):
        self.calls.append(request)
        parsed = urlsplit(request.full_url)
        path = unquote(parsed.path)
        if path.endswith("/user/login"):
            return FakeResponse({"data": {"session": "session-wire"}})
        if path.endswith("/price"):
            status = self.price_statuses.pop(0) if self.price_statuses else 200
            if status != 200:
                return FakeResponse({}, status=status)
            codes = path.split("/goods/", 1)[1].rsplit("/price", 1)[0].split(",")
            return FakeResponse(
                {
                    "data": [
                        {"gdscode": code, "pricewnds": "10", "price": "0", "price_tarif": "0", "price_retail": "0"}
                        for code in codes
                    ]
                }
            )
        if path.endswith("/remains"):
            code = path.split("/goods/", 1)[1].rsplit("/remains", 1)[0]
            return FakeResponse(
                {"data": {"gdscode": code, "InfoStores": [{"StoreCode": "WH-1", "StoreQuantRem": "1"}]}}
            )
        code = path.split("/goods/", 1)[1]
        if "type" in parsed.query and "mnf" in parsed.query:
            code = "100"
            return FakeResponse(
                {"data": {"gdscode": code, "name": "Valve 100", "art": "A-100", "mnf_name": "Acme", "edizm": "шт."}}
            )
        return FakeResponse(
            {"data": {"gdscode": code, "name": f"Valve {code}", "art": f"A-{code}", "mnf_name": "Acme", "edizm": "шт."}}
        )


def _etm_provider(transport, tmp_path, *, records=(), has_catalog=True, max_live_candidates=5):
    settings = EtmIproSettings(
        enabled=True,
        environment="test",
        warehouse_codes=["WH-1"],
        base_url_override="https://etm.example/api/v1",
        max_live_candidates=max_live_candidates,
    )
    client = EtmIproClient(
        settings,
        "login@example.test",
        "password-private",
        auth_state_path=tmp_path / "auth_quarantine.json",
        transport=transport,
        rate_limiter=EtmRateLimiter(interval_seconds=0),
    )
    provider = EtmIproProvider(
        settings,
        MemorySecretStore(),
        tmp_path,
        client=client,
        mirror=FakeEtmMirror(records, has_catalog=has_catalog),
    )
    return provider, client


def _warm_etm(client, transport):
    client.check_access()
    transport.calls.clear()


def test_etm_mirror_warm_and_cold_counts_include_login_only_when_sent(tmp_path):
    warm_transport = EtmTransport()
    warm_provider, warm_client = _etm_provider(
        warm_transport,
        tmp_path / "warm",
        records=[_etm_record("100")],
    )
    _warm_etm(warm_client, warm_transport)
    warm = _run(EtmIproExecutionAdapter(warm_provider), _intent())
    assert warm.state == "success"
    assert warm.request_count == len(warm_transport.calls) == 3

    cold_transport = EtmTransport()
    cold_provider, _ = _etm_provider(
        cold_transport,
        tmp_path / "cold",
        records=[_etm_record("100")],
    )
    cold = _run(EtmIproExecutionAdapter(cold_provider), _intent())
    assert cold.state == "success"
    assert cold.request_count == len(cold_transport.calls) == 4
    assert urlsplit(cold_transport.calls[0].full_url).path.endswith("/user/login")


def test_etm_direct_lookup_and_multiple_candidate_counts_match_existing_flow(tmp_path):
    direct_transport = EtmTransport()
    direct_provider, direct_client = _etm_provider(
        direct_transport,
        tmp_path / "direct",
        records=[_etm_record("100")],
    )
    _warm_etm(direct_client, direct_transport)
    direct = _run(
        EtmIproExecutionAdapter(direct_provider),
        _intent(article="A-100", manufacturer="Acme"),
    )
    assert direct.state == "success"
    assert direct.request_count == len(direct_transport.calls) == 3
    assert "/goods/A-100" in urlsplit(direct_transport.calls[0].full_url).path

    multi_transport = EtmTransport()
    multi_provider, multi_client = _etm_provider(
        multi_transport,
        tmp_path / "multi",
        records=[_etm_record("100"), _etm_record("101"), _etm_record("102")],
    )
    _warm_etm(multi_client, multi_transport)
    multi = _run(EtmIproExecutionAdapter(multi_provider), _intent(), limit=3)
    assert multi.state == "success"
    assert multi.request_count == len(multi_transport.calls) == 7
    assert sum(urlsplit(call.full_url).path.endswith("/price") for call in multi_transport.calls) == 1
    assert sum(urlsplit(call.full_url).path.endswith("/remains") for call in multi_transport.calls) == 3
    assert sum("/goods/" in urlsplit(call.full_url).path and not urlsplit(call.full_url).path.endswith(("/price", "/remains")) for call in multi_transport.calls) == 3


def test_etm_quarantine_suppresses_without_count_and_403_counts_once(tmp_path):
    blocked_transport = EtmTransport()
    blocked_provider, blocked_client = _etm_provider(
        blocked_transport,
        tmp_path / "blocked",
        records=[_etm_record("100")],
    )
    blocked_client._local_quarantine_until = blocked_client._wall_clock() + 3600
    blocked = _run(EtmIproExecutionAdapter(blocked_provider), _intent())
    assert blocked.state == "failure"
    assert blocked.failure_category == ProviderFailureCategory.AUTHENTICATION
    assert blocked.request_count == 0
    assert blocked_transport.calls == []

    forbidden_transport = EtmTransport(price_statuses=[403])
    forbidden_provider, forbidden_client = _etm_provider(
        forbidden_transport,
        tmp_path / "forbidden",
        records=[_etm_record("100")],
    )
    _warm_etm(forbidden_client, forbidden_transport)
    forbidden = _run(EtmIproExecutionAdapter(forbidden_provider), _intent())
    assert forbidden.state == "failure"
    assert forbidden.failure_category == ProviderFailureCategory.AUTHENTICATION
    assert forbidden.request_count == len(forbidden_transport.calls) == 1
    assert forbidden_client._auth_state_path.exists()
    blocked_again = _run(EtmIproExecutionAdapter(forbidden_provider), _intent())
    assert blocked_again.request_count == 0
    assert len(forbidden_transport.calls) == 1


def test_etm_unsynced_catalog_fails_locally_and_legacy_output_matches_adapter(tmp_path):
    unsynced_transport = EtmTransport()
    unsynced_provider, _ = _etm_provider(
        unsynced_transport,
        tmp_path / "unsynced",
        has_catalog=False,
    )
    unsynced = _run(EtmIproExecutionAdapter(unsynced_provider), _intent())
    assert unsynced.state == "failure"
    assert unsynced.failure_category == ProviderFailureCategory.MISCONFIGURED
    assert unsynced.request_count == 0
    assert unsynced_transport.calls == []

    legacy_transport = EtmTransport()
    legacy_provider, _ = _etm_provider(
        legacy_transport,
        tmp_path / "legacy",
        records=[_etm_record("100")],
    )
    expected = legacy_provider.search(_intent(), limit=5)

    adapter_transport = EtmTransport()
    adapter_provider, _ = _etm_provider(
        adapter_transport,
        tmp_path / "adapter",
        records=[_etm_record("100")],
    )
    outcome = _run(EtmIproExecutionAdapter(adapter_provider), _intent())
    assert _offer_dump(outcome.offers) == _offer_dump(expected)
    assert outcome.request_count == len(adapter_transport.calls) == 4
    assert len(legacy_transport.calls) == 4


def test_request_identities_track_affinity_without_exposing_secrets(tmp_path):
    repository = CatalogRepository(tmp_path / "identity.sqlite3")
    from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider

    local = LocalCatalogExecutionAdapter(LocalCatalogProvider(repository))
    first_local = local.request_identity(_intent(), limit=5, execution_scope_id="scope")
    repository.clear()
    second_local = local.request_identity(_intent(), limit=5, execution_scope_id="scope")
    assert first_local.request_fingerprint != second_local.request_fingerprint
    assert first_local.affinity.environment == first_local.affinity.region_id == ""

    lemana_transport = LemanaTransport()
    lemana_provider, _ = _lemana_provider(lemana_transport)
    lemana = LemanaB2BExecutionAdapter(lemana_provider)
    lemana_identity = lemana.request_identity(_intent(), limit=5, execution_scope_id="scope")
    lemana_provider.settings.region_id = 35
    lemana_region_identity = lemana.request_identity(_intent(), limit=5, execution_scope_id="scope")
    assert lemana_identity.request_fingerprint != lemana_region_identity.request_fingerprint
    assert lemana_identity.affinity.region_id == "34"
    assert lemana_region_identity.affinity.region_id == "35"

    etm_transport = EtmTransport()
    etm_provider, _ = _etm_provider(etm_transport, tmp_path / "identity-etm")
    etm = EtmIproExecutionAdapter(etm_provider)
    etm_identity = etm.request_identity(_intent(), limit=5, execution_scope_id="scope")
    etm_provider.settings.warehouse_codes = ["WH-2"]
    etm_warehouse_identity = etm.request_identity(_intent(), limit=5, execution_scope_id="scope")
    assert etm_identity.request_fingerprint != etm_warehouse_identity.request_fingerprint
    etm_provider.settings.max_live_candidates = 4
    etm_candidate_identity = etm.request_identity(_intent(), limit=5, execution_scope_id="scope")
    assert etm_warehouse_identity.request_fingerprint != etm_candidate_identity.request_fingerprint
    for identity in (first_local, lemana_identity, etm_identity):
        assert len(identity.request_fingerprint) == 64
        serialized = json.dumps(identity.model_dump(mode="json"), sort_keys=True)
        assert "client-secret" not in serialized
        assert "password-private" not in serialized
        assert "session-wire" not in serialized
        assert "fixture-token" not in serialized


@pytest.mark.parametrize(
    ("source_category", "expected"),
    [
        ("auth", ProviderFailureCategory.AUTHENTICATION),
        ("rate_limit", ProviderFailureCategory.RATE_LIMITED),
        ("rate_limited", ProviderFailureCategory.RATE_LIMITED),
        ("network", ProviderFailureCategory.TRANSPORT),
        ("timeout", ProviderFailureCategory.TIMEOUT),
        ("upstream_error", ProviderFailureCategory.UNAVAILABLE),
        ("storage_error", ProviderFailureCategory.UNAVAILABLE),
        ("invalid_response", ProviderFailureCategory.INVALID_RESPONSE),
        ("not_configured", ProviderFailureCategory.MISCONFIGURED),
        ("invalid_request", ProviderFailureCategory.MISCONFIGURED),
        ("health_error", ProviderFailureCategory.MISCONFIGURED),
        ("unrecognized-category", ProviderFailureCategory.UNKNOWN),
    ],
)
def test_sourcing_error_categories_map_to_bounded_outcomes(source_category, expected):
    error = SourcingProviderError(
        "sensitive message should not leave this mapping",
        category=source_category,
    )
    assert failure_category_for_sourcing_error(error) == expected


def test_optional_transport_observer_counts_only_calls_that_reach_transport():
    class Observer:
        def __init__(self):
            self.count = 0

        def record_outbound_attempt(self):
            self.count += 1

    observer = Observer()
    transport = LemanaTransport()
    settings = LemanaB2BSettings(enabled=True, environment="test", client_id="client-1", region_id=34)
    client = LemanaB2BClient(settings, "client-secret", transport=transport)
    with pytest.raises(SourcingProviderError):
        client.get_prices([str(index) for index in range(101)], region_id=34, outbound_attempt_observer=observer)
    assert observer.count == len(transport.calls) == 0
    client.get_prices(["82331508"], region_id=34, outbound_attempt_observer=observer)
    assert observer.count == len(transport.calls) == 2
