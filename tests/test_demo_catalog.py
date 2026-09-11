from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.demo_catalog import (
    DEMO_CATALOG_NOTICE,
    DEMO_CATALOG_SOURCE,
    seed_demo_catalog,
)
from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import MatchDecision, Offer
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.service import SourcingService


@pytest.fixture()
def demo_repository(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    result = seed_demo_catalog(repository)
    assert 30 <= result["demo_item_count"] <= 45
    return repository


def nobo_row(*, model="NFK4N 07", power="750", quantity="1"):
    return {
        "id": f"nobo-{model}",
        "row_type": "item",
        "name": f"Электрический конвектор влагозащищенный Q={power} Вт",
        "type_mark": model,
        "manufacturer": "NOBO",
        "unit": "шт.",
        "quantity": quantity,
        "status": "verified",
        "quantity_trusted": True,
    }


def storefront_request(app, path: str) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("test", 50000),
        "server": ("testserver", 80),
        "app": app,
        "router": app.router,
    })


def test_demo_fixture_imports_and_seed_is_idempotent(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    first = seed_demo_catalog(repository)
    second = seed_demo_catalog(repository)
    assert first["imported_count"] == 36
    assert first["demo_item_count"] == second["demo_item_count"] == 36
    assert first["catalog_item_count"] == second["catalog_item_count"] == 36
    assert second["source"] == DEMO_CATALOG_SOURCE
    assert repository.stats()["item_count"] > 0


def test_demo_seed_preserves_existing_non_demo_offers(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.upsert(Offer(
        offer_id="production-owned-offer",
        provider="external",
        title="Existing supplier offer",
        price="100",
        currency="RUB",
        url="https://supplier.example/existing",
        data_provenance={"source": "external_supplier"},
    ))
    result = seed_demo_catalog(repository)
    assert result["demo_item_count"] == 36
    assert result["catalog_item_count"] == 37
    existing = repository.get_by_id("production-owned-offer")
    assert existing is not None
    assert existing.url == "https://supplier.example/existing"
    assert existing.data_provenance["source"] == "external_supplier"


def test_exact_nobo_model_is_strongest_and_conflicting_power_is_rejected(demo_repository):
    intent = build_fallback_intent(nobo_row())
    offers = LocalCatalogProvider(demo_repository).search(intent, limit=20)
    matches = OfferMatcher().match(intent, offers)
    exact = next(item for item in matches if item.offer.offer_id == "demo-nobo-nfk4n-07")
    conflicting = next(item for item in matches if item.offer.offer_id == "demo-nobo-nfk4n-10")
    assert exact.decision == MatchDecision.MATCH
    assert exact.matched_attributes == ["model", "power", "manufacturer"]
    assert conflicting.decision == MatchDecision.REJECT
    assert "power" in conflicting.conflicting_attributes
    assert exact.rank < conflicting.rank


def test_quoted_manufacturer_and_extended_model_keep_exact_offer_first(demo_repository):
    row = nobo_row()
    row["manufacturer"] = "“NOBO”"
    intent = build_fallback_intent(row)
    matches = OfferMatcher().match(
        intent,
        LocalCatalogProvider(demo_repository).search(intent, limit=20),
    )
    assert matches[0].offer.offer_id == "demo-nobo-nfk4n-07"
    assert matches[0].decision == MatchDecision.MATCH
    assert {"model", "manufacturer"}.issubset(matches[0].matched_attributes)

    kev_row = {
        "id": "kev-live-shape",
        "row_type": "item",
        "name": "Воздушная тепловая завеса",
        "type_mark": 'КЭВ-9П2012Е\nСерия 200Е "Оптима"',
        "manufacturer": "Тепломаш",
        "unit": "шт.",
        "quantity": "1",
        "status": "verified",
        "quantity_trusted": True,
    }
    kev_intent = build_fallback_intent(kev_row)
    kev_matches = OfferMatcher().match(
        kev_intent,
        LocalCatalogProvider(demo_repository).search(kev_intent, limit=20),
    )
    assert kev_matches[0].offer.offer_id == "demo-aircurtain-kev-9p2012e"
    assert kev_matches[0].decision == MatchDecision.MATCH
    assert next(
        item for item in kev_matches
        if item.offer.offer_id == "demo-aircurtain-kev-12p3041e"
    ).decision == MatchDecision.ALTERNATIVE


def test_unconstrained_retrieval_can_remain_likely_match(demo_repository):
    from averon_import.services.sourcing.models import ProductIntent

    intent = ProductIntent(
        source_row_id="generic-convector",
        source_text="Конвектор",
        normalized_name="Конвектор",
        search_queries=["Конвектор"],
    )
    matches = OfferMatcher().match(
        intent,
        LocalCatalogProvider(demo_repository).search(intent, limit=5),
    )
    assert matches
    assert all(item.decision == MatchDecision.LIKELY_MATCH for item in matches)


def test_demo_offer_url_and_provenance_survive_repository_roundtrip(demo_repository):
    offer = demo_repository.get_by_id("demo-nobo-nfk4n-07")
    assert offer is not None
    assert offer.url == "/demo-catalog/products/demo-nobo-nfk4n-07"
    assert offer.data_provenance["source"] == DEMO_CATALOG_SOURCE
    assert offer.price == Decimal("14990")


def test_demo_storefront_reads_same_sqlite_offer_and_unknown_returns_404(
    demo_repository, monkeypatch
):
    from averon_import import main

    monkeypatch.setattr(main, "sourcing_repository", demo_repository)
    catalog = main.demo_catalog(storefront_request(main.app, "/demo-catalog"))
    product = main.demo_catalog_product(
        storefront_request(main.app, "/demo-catalog/products/demo-nobo-nfk4n-07"),
        "demo-nobo-nfk4n-07",
    )
    with pytest.raises(HTTPException) as missing:
        main.demo_catalog_product(
            storefront_request(main.app, "/demo-catalog/products/demo-missing"),
            "demo-missing",
        )
    catalog_html = catalog.body.decode("utf-8")
    product_html = product.body.decode("utf-8")
    assert catalog.status_code == product.status_code == 200
    assert missing.value.status_code == 404
    assert DEMO_CATALOG_NOTICE in catalog_html
    assert DEMO_CATALOG_NOTICE in product_html
    assert "Электрический конвектор NOBO NFK4N 07, 750 Вт" in product_html
    assert "14990.00" in product_html
    assert DEMO_CATALOG_SOURCE in product_html


def test_catalog_stats_endpoint_uses_seeded_repository(demo_repository, monkeypatch):
    from averon_import import main

    monkeypatch.setattr(main, "sourcing_repository", demo_repository)
    payload = main.sourcing_catalog_stats()
    assert payload["item_count"] == 36
    assert payload["source_count"] == 1


def test_external_provider_url_is_not_rewritten(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    external_url = "https://supplier.example/products/item?id=42&utm_source=averon"
    repository.upsert(Offer(
        offer_id="external-42",
        provider="external",
        title="External offer",
        price="10",
        currency="RUB",
        url=external_url,
        data_provenance={"source": "external"},
    ))
    assert repository.get_by_id("external-42").url == external_url


def test_qwen_commercial_fields_are_rejected_and_never_become_offer_facts():
    class Provider:
        key = "yandex"
        model = "test-qwen"
        configured = True

        def complete(self, messages):
            return json.dumps({
                "normalized_name": "Конвектор",
                "price": 1,
                "currency": "RUB",
                "availability": True,
                "url": "https://invented.example",
            })

    class Transport:
        provider = Provider()

        def ensure_provider(self, key):
            return self.provider

        def public_config(self):
            return {"providers": {"yandex": {"configured": True, "model": "test-qwen"}}}

    row = nobo_row()
    fallback = build_fallback_intent(row)
    audit = SourcingAIService(Transport()).understand_with_audit(row, fallback)
    assert audit.mode == "fallback"
    assert audit.ai_proposal is None
    assert audit.resolved_intent == fallback


def test_project_total_uses_trusted_quantity_times_provider_price(demo_repository, tmp_path):
    provider = LocalCatalogProvider(demo_repository)
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    result = service.search_project([nobo_row(quantity="2")])
    assert result.positions_matched == 1
    assert result.estimated_total == Decimal("29980")
    assert result.currency == "RUB"
    assert result.results[0].recommended_offer.offer_id == "demo-nobo-nfk4n-07"


def test_representative_p58_subset_uses_provider_prices(demo_repository, tmp_path):
    rows = [
        nobo_row(model="NFK4N 07", power="750", quantity="4"),
        nobo_row(model="NFK4N 10", power="1000", quantity="6"),
        {
            "id": "p58-cable",
            "row_type": "item",
            "name": "Кабель нагревательный Q=1200Bm",
            "type_mark": "Tropix ТЛБЭ 1200",
            "manufacturer": "Теплолюкс",
            "unit": "шт.",
            "quantity": "1",
            "status": "recognized",
        },
        {
            "id": "p58-regulator",
            "row_type": "item",
            "name": "Терморегулятор для теплого пола",
            "type_mark": "TP510",
            "manufacturer": "Теплолюкс",
            "unit": "шт.",
            "quantity": "17",
            "status": "recognized",
        },
        {
            "id": "p58-kev",
            "row_type": "item",
            "name": "Воздушная тепловая завеса",
            "type_mark": 'КЭВ-9П2012Е\nСерия 200Е "Оптима"',
            "manufacturer": "Тепломаш",
            "unit": "шт.",
            "quantity": "1",
            "status": "verified",
        },
    ]
    provider = LocalCatalogProvider(demo_repository)
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    result = service.search_project(rows)
    assert result.positions_total == result.positions_processed == 5
    assert result.positions_matched == 3
    assert result.positions_alternatives == 2
    assert result.positions_review == result.positions_without_offers == 0
    assert result.estimated_total == Decimal("357180")
    assert result.currency == "RUB"
    assert [
        item.recommended_offer.offer_id for item in result.results
    ] == [
        "demo-nobo-nfk4n-07",
        "demo-nobo-nfk4n-10",
        "demo-cable-brim17-20",
        "demo-regulator-devireg130",
        "demo-aircurtain-kev-9p2012e",
    ]
