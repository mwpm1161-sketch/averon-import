from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import unquote, urlsplit

import pytest

from averon_import.services.app_settings import AppSettingsService
from averon_import.services.secrets import (
    ETM_IPRO_LOGIN,
    ETM_IPRO_PASSWORD,
    LEMANA_B2B_CLIENT_SECRET,
    MemorySecretStore,
)
from averon_import.services.sourcing.models import Offer, ProductIntent, SourcingProviderRuntimeState
from averon_import.services.sourcing.providers.contracts import ProviderSelection
from averon_import.services.sourcing.providers import demo_store_http as demo_store_module
from averon_import.services.sourcing.providers.demo_store_http import DemoStoreHttpProvider
from averon_import.services.sourcing.providers.etm_ipro import client as etm_client_module
from averon_import.services.sourcing.providers.etm_ipro.client import EtmIproClient
from averon_import.services.sourcing.providers.etm_ipro.provider import EtmIproProvider
from averon_import.services.sourcing.providers.execution import (
    ProviderExecutionScope,
    ProviderRunnerConfigurationError,
)
from averon_import.services.sourcing.providers.lemana_b2b import client as lemana_client_module
from averon_import.services.sourcing.providers.lemana_b2b.client import LemanaB2BClient
from averon_import.services.sourcing.providers.lemana_b2b.models import (
    LemanaProductRecord,
    LemanaProductsPage,
)
from averon_import.services.sourcing.providers.lemana_b2b.provider import LemanaB2BProvider
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.runtime import create_sourcing_runtime
from averon_import.services.sourcing.service import SourcingService


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def close(self) -> None:
        return None


class LemanaTransport:
    def __init__(self, *, price_status: int = 200) -> None:
        self.calls = []
        self.price_status = price_status

    def __call__(self, request, timeout):
        self.calls.append(request)
        path = urlsplit(request.full_url).path
        if path.endswith("/token"):
            return FakeResponse(
                {"access_token": "runtime-fixture-token", "expires_in": 3600, "token_type": "Bearer"}
            )
        if self.price_status != 200:
            return FakeResponse({}, status=self.price_status)
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


class EtmTransport:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        parsed = urlsplit(request.full_url)
        path = unquote(parsed.path)
        if path.endswith("/user/login"):
            return FakeResponse({"data": {"session": "runtime-fixture-session"}})
        if path.endswith("/price"):
            source_ids = path.split("/goods/", 1)[1].rsplit("/price", 1)[0].split(",")
            return FakeResponse(
                {
                    "data": [
                        {
                            "gdscode": source_id,
                            "pricewnds": "10",
                            "price": "0",
                            "price_tarif": "0",
                            "price_retail": "0",
                        }
                        for source_id in source_ids
                    ]
                }
            )
        if path.endswith("/remains"):
            source_id = path.split("/goods/", 1)[1].rsplit("/remains", 1)[0]
            return FakeResponse(
                {
                    "data": {
                        "gdscode": source_id,
                        "InfoStores": [{"StoreCode": "WH-1", "StoreQuantRem": "3"}],
                    }
                }
            )
        source_id = path.split("/goods/", 1)[1]
        return FakeResponse(
            {
                "data": {
                    "gdscode": source_id,
                    "name": "Valve K-100",
                    "art": "A-100",
                    "mnf_name": "Acme",
                    "edizm": "шт.",
                }
            }
        )


class LemanaMirrorSeed:
    def __init__(self, product: LemanaProductRecord) -> None:
        self.product = product

    def get_products(self, *, region_id, page, per_page, if_modified_since):
        return LemanaProductsPage(
            products=(self.product,),
            page=page,
            per_page=per_page,
            total_count=1,
        )


def _intent(row_id: str = "runtime-row") -> ProductIntent:
    return ProductIntent(
        source_row_id=row_id,
        source_text="Valve K-100",
        normalized_name="Valve K-100",
        model="K-100",
        search_queries=["Valve K-100"],
    )


def _product() -> LemanaProductRecord:
    return LemanaProductRecord(
        product_item="82331508",
        product_available=True,
        product_name="Valve K-100",
        product_description="Steel valve",
        product_url="https://supplier.example/products/82331508",
        product_model="K-100",
        product_brand="Acme",
        product_unit_sale={"name": "шт."},
    )


def _offer(provider_key: str, offer_id: str) -> Offer:
    return Offer(
        offer_id=offer_id,
        provider=provider_key,
        source_item_id=offer_id,
        title="Valve K-100",
        article="K-100",
        manufacturer="Acme",
        price=Decimal("10"),
        currency="RUB",
    )


def _runtime(
    tmp_path,
    monkeypatch,
    *,
    lemana_transport: LemanaTransport | None = None,
    etm_transport: EtmTransport | None = None,
    enable_lemana: bool = True,
    enable_etm: bool = True,
):
    for env_name in (
        "AVERON_LEMANA_B2B_CLIENT_SECRET",
        "AVERON_ETM_IPRO_LOGIN",
        "AVERON_ETM_IPRO_PASSWORD",
    ):
        monkeypatch.delenv(env_name, raising=False)
    settings = AppSettingsService(tmp_path / "settings")
    settings.settings.sourcing.lemana_b2b.enabled = enable_lemana
    settings.settings.sourcing.lemana_b2b.client_id = "runtime-client"
    settings.settings.sourcing.lemana_b2b.environment = "test"
    settings.settings.sourcing.lemana_b2b.region_id = 34
    settings.settings.sourcing.etm_ipro.enabled = enable_etm
    settings.settings.sourcing.etm_ipro.environment = "test"
    settings.settings.sourcing.etm_ipro.warehouse_codes = ["WH-1"]
    settings.settings.sourcing.etm_ipro.base_url_override = "https://etm.example/api/v1"
    secret_store = MemorySecretStore()
    secret_store.set(LEMANA_B2B_CLIENT_SECRET, "runtime-client-secret")
    secret_store.set(ETM_IPRO_LOGIN, "runtime-login")
    secret_store.set(ETM_IPRO_PASSWORD, "runtime-password")
    lemana_transport = lemana_transport or LemanaTransport()
    etm_transport = etm_transport or EtmTransport()
    monkeypatch.setattr(lemana_client_module, "_default_transport", lemana_transport)
    monkeypatch.setattr(etm_client_module, "_default_transport", etm_transport)
    runtime = create_sourcing_runtime(tmp_path / "data", settings, secret_store)
    return runtime, lemana_transport, etm_transport


def _seed_lemana(runtime) -> None:
    provider = runtime.providers["lemana_b2b"]
    provider.mirror.sync(
        LemanaMirrorSeed(_product()),
        region_id=34,
        environment="test",
    )


def _seed_etm(runtime) -> None:
    provider = runtime.providers["etm_ipro"]
    provider.mirror.sync_snapshot(
        [
            {
                "gdscode": "9536092",
                "name": "Valve K-100",
                "art": "A-100",
                "mnf_name": "Acme",
                "edizm": "шт.",
            }
        ]
    )


def _execute(runtime, keys, *, row_id="runtime-row", limit=5):
    return runtime.service.execute_provider_selection(
        _intent(row_id),
        selection=ProviderSelection(provider_keys=tuple(keys)),
        limit=limit,
        execution_scope=ProviderExecutionScope(execution_scope_id=f"scope-{row_id}"),
    )


def test_runtime_composition_is_silent_and_uses_same_provider_instances(tmp_path, monkeypatch):
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runtime composition must not probe or search providers")

    monkeypatch.setattr(LocalCatalogProvider, "search", unexpected)
    monkeypatch.setattr(LocalCatalogProvider, "stats", unexpected)
    monkeypatch.setattr(LemanaB2BProvider, "search", unexpected)
    monkeypatch.setattr(LemanaB2BProvider, "stats", unexpected)
    monkeypatch.setattr(LemanaB2BClient, "check_access", unexpected)
    monkeypatch.setattr(EtmIproProvider, "search", unexpected)
    monkeypatch.setattr(EtmIproProvider, "stats", unexpected)
    monkeypatch.setattr(EtmIproProvider, "health", unexpected)
    monkeypatch.setattr(EtmIproClient, "check_access", unexpected)
    monkeypatch.setattr(DemoStoreHttpProvider, "search", unexpected)
    monkeypatch.setattr(DemoStoreHttpProvider, "stats", unexpected)
    monkeypatch.setattr(demo_store_module, "_default_transport", unexpected)
    runtime, lemana_transport, etm_transport = _runtime(tmp_path, monkeypatch)

    assert calls == []
    assert lemana_transport.calls == []
    assert etm_transport.calls == []
    assert runtime.execution_provider_keys == (
        "etm_ipro",
        "lemana_b2b",
        "local_catalog",
    )
    assert "demo_store_http" not in runtime.execution_provider_keys
    assert runtime.service.providers is runtime.providers
    assert runtime.service.provider_runner is runtime.provider_runner
    assert runtime.providers["local_catalog"].repository is runtime.repository
    assert runtime.execution_providers["local_catalog"].provider is runtime.providers["local_catalog"]
    assert runtime.execution_providers["lemana_b2b"].provider is runtime.providers["lemana_b2b"]
    assert runtime.execution_providers["etm_ipro"].provider is runtime.providers["etm_ipro"]
    assert runtime.execution_providers["lemana_b2b"].provider.client is runtime.providers["lemana_b2b"].client
    assert runtime.execution_providers["etm_ipro"].provider.client is runtime.providers["etm_ipro"].client


def test_internal_retrieval_local_only_skips_matcher_ai_and_legacy_cache(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path, monkeypatch)
    runtime.repository.upsert(_offer("local_catalog", "local-1"))
    monkeypatch.setattr(runtime.providers["local_catalog"], "stats", lambda: (_ for _ in ()).throw(AssertionError("stats called")))
    monkeypatch.setattr(runtime.service.matcher, "match", lambda *args: (_ for _ in ()).throw(AssertionError("matcher called")))
    monkeypatch.setattr(runtime.service.ai, "rank_matches", lambda *args: (_ for _ in ()).throw(AssertionError("AI ranking called")))
    monkeypatch.setattr(runtime.service.cache, "get", lambda *args: (_ for _ in ()).throw(AssertionError("legacy cache read")))
    monkeypatch.setattr(runtime.service.cache, "set", lambda *args: (_ for _ in ()).throw(AssertionError("legacy cache write")))

    result = _execute(runtime, ("local_catalog",))

    assert result.selection.provider_keys == ("local_catalog",)
    assert len(result.outcomes) == 1
    assert result.outcomes[0].request_count == 0
    assert result.outcomes[0].state == "success"
    assert [offer.offer_id for offer in result.offers] == ["local-1"]
    assert not (tmp_path / "data" / "sourcing" / "cache.json").exists()


def test_internal_retrieval_lemana_only_counts_fake_transport(tmp_path, monkeypatch):
    transport = LemanaTransport()
    runtime, transport, _ = _runtime(tmp_path, monkeypatch, lemana_transport=transport, enable_etm=False)
    _seed_lemana(runtime)
    monkeypatch.setattr(runtime.providers["lemana_b2b"], "stats", lambda: (_ for _ in ()).throw(AssertionError("stats called")))

    result = _execute(runtime, ("lemana_b2b",))

    outcome = result.outcomes[0]
    assert outcome.state == "success"
    assert outcome.request_count == len(transport.calls) == 2
    assert len(result.offers) == 1
    assert result.offers[0].provider == "lemana_b2b"


def test_internal_retrieval_etm_only_counts_fake_transport(tmp_path, monkeypatch):
    transport = EtmTransport()
    runtime, _, transport = _runtime(tmp_path, monkeypatch, etm_transport=transport, enable_lemana=False)
    _seed_etm(runtime)
    monkeypatch.setattr(runtime.providers["etm_ipro"], "stats", lambda: (_ for _ in ()).throw(AssertionError("stats called")))
    monkeypatch.setattr(runtime.providers["etm_ipro"], "health", lambda: (_ for _ in ()).throw(AssertionError("health called")))

    result = _execute(runtime, ("etm_ipro",))

    outcome = result.outcomes[0]
    assert outcome.state == "success"
    assert outcome.request_count == len(transport.calls) == 4
    assert len(result.offers) == 1
    assert result.offers[0].provider == "etm_ipro"


def test_selection_order_is_canonical_and_local_and_lemana_outcomes_are_preserved(tmp_path, monkeypatch):
    runtime, transport, _ = _runtime(tmp_path, monkeypatch, enable_etm=False)
    runtime.repository.upsert(_offer("local_catalog", "local-1"))
    _seed_lemana(runtime)
    monkeypatch.setattr(runtime.service.matcher, "match", lambda *args: (_ for _ in ()).throw(AssertionError("matcher called")))
    monkeypatch.setattr(runtime.service.ai, "rank_matches", lambda *args: (_ for _ in ()).throw(AssertionError("AI ranking called")))

    result = _execute(runtime, ("local_catalog", "lemana_b2b"))

    assert result.selection.provider_keys == ("lemana_b2b", "local_catalog")
    assert tuple(outcome.provider_key for outcome in result.outcomes) == (
        "lemana_b2b",
        "local_catalog",
    )
    assert {offer.provider for offer in result.offers} == {"lemana_b2b", "local_catalog"}
    assert next(outcome for outcome in result.outcomes if outcome.provider_key == "lemana_b2b").request_count == len(transport.calls) == 2
    assert next(outcome for outcome in result.outcomes if outcome.provider_key == "local_catalog").request_count == 0


def test_lemana_failure_isolated_while_local_success_survives(tmp_path, monkeypatch):
    transport = LemanaTransport(price_status=503)
    runtime, transport, _ = _runtime(tmp_path, monkeypatch, lemana_transport=transport, enable_etm=False)
    runtime.repository.upsert(_offer("local_catalog", "local-1"))
    _seed_lemana(runtime)

    result = _execute(runtime, ("lemana_b2b", "local_catalog"))

    lemana = next(outcome for outcome in result.outcomes if outcome.provider_key == "lemana_b2b")
    local = next(outcome for outcome in result.outcomes if outcome.provider_key == "local_catalog")
    assert lemana.state == "failure"
    assert lemana.request_count == len(transport.calls) == 2
    assert lemana.failure_category.value == "unavailable"
    assert local.state == "success"
    assert local.request_count == 0
    assert [offer.provider for offer in result.offers] == ["local_catalog"]
    assert result.partial_failure is True


@pytest.mark.parametrize("provider_key", ["missing_provider", "demo_store_http"])
def test_non_execution_provider_selection_fails_before_any_provider_call(
    tmp_path,
    monkeypatch,
    provider_key,
):
    runtime, lemana_transport, etm_transport = _runtime(tmp_path, monkeypatch)
    calls = []
    for provider in runtime.providers.values():
        monkeypatch.setattr(provider, "search", lambda *args, **kwargs: calls.append("search"))
        monkeypatch.setattr(provider, "stats", lambda *args, **kwargs: calls.append("stats"))
    with pytest.raises(ValueError):
        _execute(runtime, (provider_key,))
    assert calls == []
    assert lemana_transport.calls == []
    assert etm_transport.calls == []


def test_demo_store_stays_usable_on_legacy_path_but_has_no_execution_adapter(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path, monkeypatch)
    demo = runtime.providers["demo_store_http"]
    calls = []
    monkeypatch.setattr(demo, "stats", lambda: {"configured": True, "reachable": True, "catalog_version": "demo-v1"})
    monkeypatch.setattr(demo, "search", lambda intent, *, limit: calls.append((intent, limit)) or [_offer("demo_store_http", "demo-1")])

    result = runtime.service.search_intent(_intent(), provider_key="demo_store_http", ai_rerank=False)

    assert result.offers[0].offer_id == "demo-1"
    assert len(calls) == 1
    assert "demo_store_http" not in runtime.execution_providers
    with pytest.raises(ValueError):
        _execute(runtime, ("demo_store_http",), row_id="demo-rejected")


@pytest.mark.parametrize("provider_key", ["local_catalog", "lemana_b2b", "etm_ipro", "demo_store_http"])
def test_search_intent_provider_key_remains_on_legacy_provider_path(tmp_path, monkeypatch, provider_key):
    runtime, _, _ = _runtime(tmp_path, monkeypatch, enable_lemana=False, enable_etm=False)
    provider = runtime.providers[provider_key]
    stats_calls = []
    search_calls = []
    monkeypatch.setattr(
        provider,
        "stats",
        lambda: stats_calls.append(provider_key)
        or SourcingProviderRuntimeState(catalog_version="legacy-fixture"),
    )
    monkeypatch.setattr(
        provider,
        "search",
        lambda intent, *, limit: search_calls.append((intent.source_row_id, limit))
        or [_offer(provider_key, f"{provider_key}-legacy")],
    )
    adapter = runtime.execution_providers.get(provider_key)
    if adapter is not None:
        monkeypatch.setattr(adapter, "execute_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ProviderRunner used by legacy search")))

    result = runtime.service.search_intent(_intent(), provider_key=provider_key, ai_rerank=False)

    assert result.offers[0].provider == provider_key
    assert stats_calls == [provider_key]
    assert search_calls == [("runtime-row", 20)]


def test_search_project_and_provider_only_routing_remain_legacy(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path, monkeypatch, enable_lemana=False, enable_etm=False)
    provider = runtime.providers["local_catalog"]
    calls = []
    monkeypatch.setattr(provider, "stats", lambda: {"reachable": True, "catalog_version": "legacy-project"})
    monkeypatch.setattr(provider, "search", lambda intent, *, limit: calls.append(intent.source_row_id) or [_offer("local_catalog", "legacy-project")])
    adapter = runtime.execution_providers["local_catalog"]
    monkeypatch.setattr(adapter, "execute_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ProviderRunner used by legacy routing")))

    project = runtime.service.search_project(
        [{"id": "project-row", "row_type": "item", "name": "Valve K-100", "quantity": "1"}],
        ai_rerank=False,
    )
    provider_only = runtime.service.search_intent_routed(
        _intent("provider-only-row"),
        provider_key="local_catalog",
        ai_rerank=False,
    )

    assert project.positions_processed == 1
    assert provider_only.offers[0].offer_id == "legacy-project"
    assert calls == ["project-row", "provider-only-row"]


def test_service_without_optional_runner_fails_closed():
    service = SourcingService({})
    with pytest.raises(ProviderRunnerConfigurationError, match="runner is not configured"):
        service.execute_provider_selection(
            _intent(),
            selection=ProviderSelection(provider_keys=("local_catalog",)),
            limit=5,
            execution_scope=ProviderExecutionScope(execution_scope_id="no-runner"),
        )
