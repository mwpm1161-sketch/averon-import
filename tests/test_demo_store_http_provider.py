from __future__ import annotations

import json
import urllib.error

import pytest

from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.demo_store_http import (
    DemoStoreHttpProvider,
    DemoStoreProviderError,
)


def make_intent() -> ProductIntent:
    return ProductIntent(
        source_row_id="row-1",
        source_text="NOBO NFK4N 07 750 W",
        normalized_name="Конвектор",
        manufacturer="NOBO",
        model="NFK4N 07",
        attributes={"power": 0.75},
        required_attributes={"power": 0.75},
        quantity="1",
        unit="шт.",
    )


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.status = status
        self.code = status
        self.payload = payload
        self.closed = False

    def read(self, limit=None):
        raw = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return raw if limit is None else raw[:limit]

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_base_url_is_normalized_and_only_http_schemes_are_allowed():
    provider = DemoStoreHttpProvider(" https://store.example/// ")
    assert provider.base_url == "https://store.example"
    with pytest.raises(ValueError):
        DemoStoreHttpProvider("ftp://store.example")
    with pytest.raises(ValueError):
        DemoStoreHttpProvider("https://user:password@store.example")


def test_stats_maps_store_shape_and_uses_short_bounded_timeout():
    transport = FakeTransport([FakeResponse({"items": 36, "catalog_version": "demo-v4"})])
    provider = DemoStoreHttpProvider("http://127.0.0.1:8877/", transport=transport)
    assert provider.stats() == {
        "configured": True,
        "reachable": True,
        "item_count": 36,
        "catalog_version": "demo-v4",
        "latency_ms": provider.last_request_diagnostics["latency_ms"],
    }
    request, timeout = transport.calls[0]
    assert request.full_url == "http://127.0.0.1:8877/api/v1/catalog/stats"
    assert timeout <= 3.0


def test_offline_stats_is_safe_and_does_not_raise():
    transport = FakeTransport([urllib.error.URLError(TimeoutError())])
    provider = DemoStoreHttpProvider("http://127.0.0.1:8877", transport=transport)
    stats = provider.stats()
    assert stats["configured"] is True
    assert stats["reachable"] is False
    assert stats["catalog_version"] == "unavailable"
    assert "ошибка" in stats["error"] or "ожидания" in stats["error"]


def test_search_sends_product_intent_and_maps_provider_owned_offer():
    transport = FakeTransport([FakeResponse({
        "provider": "averon_demo_store",
        "catalog_version": "demo-v4",
        "items": [{
            "id": "nobo-07",
            "title": "NOBO NFK4N 07",
            "article": "NFK4N 07",
            "manufacturer": "NOBO",
            "brand": "NOBO",
            "model": "NFK4N 07",
            "price": 11990,
            "currency": "rub",
            "price_unit": "шт.",
            "availability": True,
            "availability_text": "В наличии",
            "url": "http://127.0.0.1:8877/products/nobo-07",
            "retrieval_score": 0.99,
            "decision": "REJECT",
            "attributes": {
                "power_kw": 0.75,
                "voltage_v": 230,
                "diameter_mm": 160,
                "pressure_pn": 16,
                "cable_section_mm2": 1.5,
                "core_count": 3,
                "ip": "IP24",
                "material": "сталь",
            },
            "provenance": {"index": "demo"},
        }],
    })])
    provider = DemoStoreHttpProvider(transport=transport)
    offers = provider.search(make_intent(), limit=999)
    assert len(offers) == 1
    offer = offers[0]
    request, timeout = transport.calls[0]
    assert request.full_url.endswith("/api/v1/search?limit=100")
    assert timeout == provider.timeout_seconds
    assert json.loads(request.data.decode()) == make_intent().model_dump(mode="json")
    assert offer.offer_id == offer.source_item_id == "nobo-07"
    assert offer.provider == "demo_store_http"
    assert offer.price == 11990
    assert offer.currency == "RUB"
    assert offer.availability is True
    assert offer.availability_text == "В наличии"
    assert offer.url == "http://127.0.0.1:8877/products/nobo-07"
    assert offer.attributes["model"] == "NFK4N 07"
    assert offer.attributes["power"] == 0.75
    assert offer.attributes["voltage"] == 230
    assert offer.attributes["diameter"] == 160
    assert offer.attributes["pressure"] == 16
    assert offer.attributes["cable_section"] == 1.5
    assert offer.attributes["cores"] == 3
    assert offer.attributes["protection_class"] == "IP24"
    assert offer.data_provenance["source"] == "averon_demo_store"
    assert offer.data_provenance["retrieval_score"] == 0.99
    assert offer.data_provenance["remote_provenance"] == {"index": "demo"}
    assert "decision" not in offer.model_dump(mode="json")


def test_power_w_is_converted_to_kw_and_canonical_values_are_preserved():
    transport = FakeTransport([FakeResponse({"items": [{
        "id": "power-w",
        "title": "Heater",
        "model": "H-1",
        "attributes": {
            "power_w": 750,
            "power": 0.75,
            "voltage": 230,
            "current_a": 2,
            "dn": 50,
            "pn": 10,
        },
    }]})])
    offer = DemoStoreHttpProvider(transport=transport).search(make_intent())[0]
    assert offer.attributes["power"] == 0.75
    assert offer.attributes["voltage"] == 230
    assert offer.attributes["current"] == 2
    assert offer.attributes["diameter"] == 50
    assert offer.attributes["pressure"] == 10


def test_conflicting_provider_aliases_are_rejected_not_overwritten():
    transport = FakeTransport([FakeResponse({"items": [{
        "id": "conflict",
        "title": "Conflicting item",
        "attributes": {"power": 1, "power_w": 500},
    }]})])
    with pytest.raises(DemoStoreProviderError, match="конфликт атрибутов power"):
        DemoStoreHttpProvider(transport=transport).search(make_intent())


@pytest.mark.parametrize("payload", [
    {"items": "not-a-list"},
    {"items": [{}]},
    {"items": [{"id": "missing-title"}]},
    {"items": [{"title": "missing-id"}]},
    {"items": [{"id": "bad-attributes", "title": "Bad", "attributes": []}]},
    {"items": [{"id": "bad-price", "title": "Bad", "price": "not-a-price"}]},
    {"items": [{"id": "bad-availability", "title": "Bad", "availability": {}}]},
])
def test_malformed_remote_response_is_rejected_without_partial_offers(payload):
    transport = FakeTransport([FakeResponse(payload)])
    provider = DemoStoreHttpProvider(transport=transport)
    with pytest.raises(DemoStoreProviderError):
        provider.search(make_intent())


def test_other_http_status_and_redirect_are_not_accepted():
    transport = FakeTransport([FakeResponse({"items": []}, status=404)])
    with pytest.raises(DemoStoreProviderError, match="HTTP 404"):
        DemoStoreHttpProvider(transport=transport).search(make_intent())


def test_store_match_decision_is_not_used_by_averon_matcher():
    transport = FakeTransport([FakeResponse({"items": [{
        "id": "remote-1",
        "title": "NOBO NFK4N 07",
        "model": "NFK4N 07",
        "manufacturer": "NOBO",
        "attributes": {"power_kw": 0.75},
        "decision": "REJECT",
    }]})])
    offer = DemoStoreHttpProvider(transport=transport).search(make_intent())[0]
    assert offer.provider == "demo_store_http"
    assert not hasattr(offer, "decision")
