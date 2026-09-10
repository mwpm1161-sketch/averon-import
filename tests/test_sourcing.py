from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import (
    MatchDecision,
    Offer,
    ProductIntent,
)
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.providers.base import SourcingProvider
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.service import SourcingService


def make_intent(**updates) -> ProductIntent:
    value = {
        "source_row_id": "row-1",
        "source_text": "Клапан DN50 PN16",
        "product_class": "арматура",
        "normalized_name": "Клапан",
        "manufacturer": "",
        "brand": "",
        "model": "DN50 PN16",
        "article": "",
        "attributes": {"diameter": 50, "pressure": 16},
        "required_attributes": {"diameter": 50, "pressure": 16},
        "preferred_attributes": {},
        "quantity": "2",
        "unit": "шт.",
        "search_queries": ["клапан DN50 PN16"],
        "evidence": {"mode": "test"},
        "uncertainties": [],
    }
    value.update(updates)
    return ProductIntent(**value)


def make_offer(**updates) -> Offer:
    value = {
        "offer_id": "offer-1",
        "provider": "local_catalog",
        "source_item_id": "source-1",
        "title": "Клапан фланцевый DN50 PN16",
        "article": "",
        "manufacturer": "",
        "brand": "",
        "price": Decimal("100.00"),
        "currency": "RUB",
        "price_unit": "шт.",
        "availability": True,
        "availability_text": "В наличии",
        "url": "https://catalog.example/offer-1",
        "attributes": {"diameter": 50, "pressure": 16},
        "data_provenance": {"source": "fixture", "source_item_id": "source-1"},
    }
    value.update(updates)
    return Offer(**value)


class FakeAIProvider:
    configured = True

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return self.response


class FakeAIService:
    def __init__(self, provider):
        self.provider = provider

    def ensure_provider(self, key):
        assert key == "yandex"
        return self.provider


class StubProvider:
    key = "stub"
    label = "Тестовый поставщик"

    def __init__(self, offers):
        self.offers = offers

    def search(self, intent, *, limit=20):
        return list(self.offers)[:limit]

    def stats(self):
        return {"item_count": len(self.offers), "catalog_version": "stub-1"}


def service_for(tmp_path, offers=()):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    repository.bulk_upsert(offers)
    provider = LocalCatalogProvider(repository)
    return SourcingService(
        {provider.key: provider},
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "sourcing-cache.json"),
    )


def test_s1_product_intent_serialization_and_immutability():
    intent = make_intent()
    serialized = intent.model_dump(mode="json")
    assert serialized["attributes"]["diameter"] == 50
    assert ProductIntent.model_validate(serialized).fingerprint == intent.fingerprint
    with pytest.raises(ValidationError):
        intent.normalized_name = "other"


def test_s2_deterministic_fallback_preserves_source_and_extracts_attributes():
    row = {
        "id": "ocr-7",
        "name": "Насос",
        "type_mark": "Ду50 Ру16 11 кВт",
        "manufacturer": "Аква",
        "quantity": "2",
        "unit": "шт.",
    }
    intent = build_fallback_intent(row)
    assert intent.source_row_id == "ocr-7"
    assert intent.quantity == "2"
    assert intent.attributes["diameter"] == 50
    assert intent.attributes["pressure"] == 16
    assert intent.attributes["power"] == 11
    assert intent.evidence["mode"] == "deterministic_fallback"


def test_s3_local_catalog_sqlite_insert_and_search(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    offer = make_offer()
    assert repository.upsert(offer).offer_id == offer.offer_id
    found = repository.search(make_intent(), limit=5)
    assert [item.offer_id for item in found] == [offer.offer_id]
    assert repository.get_by_id(offer.offer_id).price == Decimal("100.00")


def test_s4_article_exact_retrieval_ranks_exact_article_first(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    exact = make_offer(offer_id="exact", article="K-50", title="Клапан общий")
    other = make_offer(offer_id="other", article="K-80", title="Клапан общий")
    repository.bulk_upsert([other, exact])
    intent = make_intent(article="K-50", search_queries=["K-50"])
    assert repository.search(intent, limit=2)[0].offer_id == "exact"


def test_s5_token_order_case_and_punctuation_are_tolerated(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    offer = make_offer(title="НАСОС, циркуляционный 25/60")
    repository.upsert(offer)
    intent = make_intent(
        normalized_name="25/60 насос циркуляционный",
        model="",
        search_queries=["циркуляционный 25/60 насос"],
    )
    assert repository.search(intent, limit=1)[0].offer_id == offer.offer_id


def test_s6_generic_attributes_participate_in_retrieval(tmp_path):
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    offer = make_offer(title="Фланцевый корпус", attributes={"diameter": 50, "pressure": 16})
    repository.upsert(offer)
    intent = make_intent(
        normalized_name="корпус",
        search_queries=["корпус"],
        attributes={"diameter": 50, "pressure": 16, "material": "сталь"},
        required_attributes={"diameter": 50, "pressure": 16},
    )
    assert repository.search(intent, limit=1)[0].offer_id == offer.offer_id


def test_s7_qwen_structured_response_is_validated_into_product_intent():
    provider = FakeAIProvider(json.dumps({
        "normalized_name": "Клапан",
        "product_class": "арматура",
        "attributes": {"diameter": 50, "pressure": 16},
        "required_attributes": {"diameter": 50, "pressure": 16},
        "search_queries": ["клапан DN50 PN16"],
        "uncertainties": [],
    }, ensure_ascii=False))
    service = SourcingAIService(FakeAIService(provider))
    fallback = build_fallback_intent({"id": "r1", "name": "Клапан", "quantity": "1"})
    result, warnings = service.understand({"name": "Клапан", "quantity": "1"}, fallback)
    assert not warnings
    assert result.source_row_id == "r1"
    assert result.attributes["diameter"] == 50
    assert provider.calls == 1


def test_s8_invalid_qwen_json_falls_back_with_warning():
    provider = FakeAIProvider("not json")
    service = SourcingAIService(FakeAIService(provider))
    fallback = build_fallback_intent({"id": "r2", "name": "Насос", "quantity": "3"})
    result, warnings = service.understand({"name": "Насос", "quantity": "3"}, fallback)
    assert result.fingerprint == fallback.fingerprint
    assert warnings and "fallback" in warnings[0]


def test_s9_matcher_cannot_change_provider_price():
    offer = make_offer(price=Decimal("123.45"))
    result = OfferMatcher().match(make_intent(), [offer])[0]
    assert result.offer.price == Decimal("123.45")


def test_s10_matcher_does_not_invent_url():
    offer = make_offer(url="")
    result = OfferMatcher().match(make_intent(), [offer])[0]
    assert result.offer.url == ""


def test_s10b_ai_ranking_reorders_known_candidates_only():
    first = make_offer(offer_id="first", price=Decimal("10"), url="https://supplier.example/first")
    second = make_offer(offer_id="second", price=Decimal("20"), url="")
    matches = OfferMatcher().match(make_intent(), [first, second])
    provider = FakeAIProvider(json.dumps({
        "offer_order": ["second", "unknown", "first"],
        "evidence": {"second": ["closer model"], "unknown": ["ignored"]},
    }))
    ai = SourcingAIService(FakeAIService(provider))
    ranked, warnings = ai.rank_matches(make_intent(), matches)
    assert not warnings
    assert [item.offer.offer_id for item in ranked] == ["second", "first"]
    assert ranked[0].offer.price == Decimal("20")
    assert ranked[0].offer.url == ""
    assert ranked[0].ai_evidence == {"reasons": ["closer model"]}


def test_s11_dn_mismatch_prevents_match():
    offer = make_offer(attributes={"diameter": 80, "pressure": 16})
    result = OfferMatcher().match(make_intent(), [offer])[0]
    assert result.decision == MatchDecision.REJECT
    assert "diameter" in result.conflicting_attributes


def test_s12_voltage_mismatch_prevents_match():
    intent = make_intent(
        attributes={"voltage": 380},
        required_attributes={"voltage": 380},
    )
    offer = make_offer(attributes={"voltage": 220})
    result = OfferMatcher().match(intent, [offer])[0]
    assert result.decision == MatchDecision.REJECT
    assert "voltage" in result.conflicting_attributes


def test_s13_matching_required_attributes_permits_match():
    offer = make_offer()
    result = OfferMatcher().match(make_intent(), [offer])[0]
    assert result.decision == MatchDecision.MATCH
    assert set(result.matched_attributes) >= {"diameter", "pressure"}


def test_s14_preferred_manufacturer_difference_is_alternative():
    intent = make_intent(
        manufacturer="Preferred",
        preferred_attributes={"manufacturer": "Preferred"},
    )
    offer = make_offer(manufacturer="Other")
    result = OfferMatcher().match(intent, [offer])[0]
    assert result.decision == MatchDecision.ALTERNATIVE
    assert "manufacturer" in result.deterministic_evidence["preferred_differences"]


def test_s15_quantity_times_price_is_calculated_without_mutating_row(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("10"))])
    row = {"id": "q1", "row_type": "item", "name": "Клапан", "quantity": "3", "unit": "шт."}
    result = service.search_project([row])
    assert result.estimated_total == Decimal("30")
    assert result.currency == "RUB"
    assert row["quantity"] == "3"


def test_s16_unresolved_quantity_never_creates_trusted_total(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("10"))])
    row = {"id": "q2", "row_type": "item", "name": "Клапан", "quantity": "", "status": "review"}
    result = service.search_project([row])
    assert result.estimated_total is None
    assert any("quantity requires confirmation" in warning for warning in result.warnings)


def test_s17_multiple_currencies_are_not_summed(tmp_path):
    rub = make_offer(offer_id="rub", title="Клапан RUB", price=Decimal("10"), currency="RUB")
    eur = make_offer(offer_id="eur", title="Насос EUR", price=Decimal("20"), currency="EUR")
    service = service_for(tmp_path, [rub, eur])
    rows = [
        {"id": "r", "row_type": "item", "name": "Клапан", "quantity": "1"},
        {"id": "e", "row_type": "item", "name": "Насос", "quantity": "1"},
    ]
    result = service.search_project(rows)
    assert result.estimated_total is None
    assert set(result.estimated_totals) == {"EUR", "RUB"}


def test_s18_provider_swap_keeps_domain_result_contract():
    provider = StubProvider([make_offer(provider="stub")])
    service = SourcingService({provider.key: provider}, default_provider=provider.key, ai=SourcingAIService())
    result = service.search_intent(make_intent(), provider_key="stub")
    assert result.intent.source_row_id == "row-1"
    assert result.offers[0].provider == "stub"
    assert result.match_results[0].decision == MatchDecision.MATCH


def test_s19_empty_catalog_returns_no_offers(tmp_path):
    service = service_for(tmp_path)
    result = service.search_intent(make_intent())
    assert result.offers == []
    assert result.recommended_offer is None


def test_s20_project_sourcing_aggregates_positions(tmp_path):
    first = make_offer(offer_id="first", title="Клапан", price=Decimal("10"))
    second = make_offer(offer_id="second", title="Насос", price=Decimal("20"))
    service = service_for(tmp_path, [first, second])
    rows = [
        {"id": "1", "row_type": "item", "name": "Клапан", "quantity": "2"},
        {"id": "2", "row_type": "item", "name": "Насос", "quantity": "3"},
        {"id": "3", "row_type": "note", "name": "Примечание", "quantity": "1"},
    ]
    result = service.search_project(rows)
    assert result.positions_total == 2
    assert result.positions_processed == 2
    assert result.positions_matched == 2
    assert result.estimated_total == Decimal("80")


def test_s21_sourcing_public_api_never_exposes_ai_credentials():
    from averon_import import main

    payload = main.sourcing_providers()
    encoded = json.dumps(payload, ensure_ascii=False).casefold()
    assert "api_key" not in encoded
    assert "authorization" not in encoded
    assert "secret" not in encoded
    assert set(payload) >= {"provider", "providers", "catalog_item_count", "ai_available"}


def test_s22_source_ocr_row_is_not_mutated_by_sourcing(tmp_path):
    original = {"id": "immutable", "row_type": "item", "name": "Клапан", "quantity": "2", "unit": "шт."}
    before = dict(original)
    service = service_for(tmp_path, [make_offer()])
    service.search_row(original)
    assert original == before


def test_catalog_import_rejects_invalid_commercial_facts(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps([{"title": "Клапан", "price": "not-a-price"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="price"):
        CatalogRepository(tmp_path / "catalog.sqlite3").import_file(path)


def test_catalog_import_supports_json_and_csv_without_repairing_url(tmp_path):
    json_path = tmp_path / "offers.json"
    json_path.write_text(json.dumps([{
        "id": "json-1",
        "title": "Клапан DN50",
        "price": "10.50",
        "currency": "rub",
        "url": "https://supplier.example/item?id=1",
        "attributes": {"diameter": 50},
    }], ensure_ascii=False), encoding="utf-8")
    csv_path = tmp_path / "offers.csv"
    csv_path.write_text(
        "id,title,price,currency,availability\ncsv-1,Насос,20,EUR,true\n",
        encoding="utf-8",
    )
    repository = CatalogRepository(tmp_path / "catalog.sqlite3")
    assert repository.import_file(json_path) == 1
    assert repository.import_file(csv_path) == 1
    imported = repository.get_by_id("json-1")
    assert imported.currency == "RUB"
    assert imported.url == "https://supplier.example/item?id=1"
    assert repository.get_by_id("csv-1").availability is True
