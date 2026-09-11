from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from averon_import.services.app_settings import AppSettingsService
from averon_import.services.secrets import (
    YANDEX_AI_API_KEY,
    YANDEX_API_KEY,
    InsecureFileSecretStore,
    MemorySecretStore,
)
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.matching import OfferMatcher
from averon_import.services.sourcing.models import (
    MatchDecision,
    MatchResult,
    Offer,
    ProductIntent,
)
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)
from averon_import.services.sourcing.runtime import create_sourcing_ai_transport
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

    def complete(self, messages, **kwargs):
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


def test_project_alternative_is_not_counted_as_confirmed_match(tmp_path):
    offer = make_offer(manufacturer="Other", title="Клапан")
    service = service_for(tmp_path, [offer])
    row = {
        "id": "alternative-row",
        "row_type": "item",
        "name": "Клапан",
        "manufacturer": "Preferred",
        "quantity": "1",
    }
    result = service.search_project([row])
    assert result.positions_matched == 0
    assert result.positions_alternatives == 1
    assert result.positions_review == 0


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


def test_q1_app_settings_llm_model_configures_sourcing_qwen(tmp_path, monkeypatch):
    from averon_import.services.app_settings import AppSettingsService
    from averon_import.services.secrets import MemorySecretStore

    for name in ("AVERON_YANDEX_AI_MODEL", "AVERON_YANDEX_AI_BASE_URL", "AVERON_YANDEX_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"folder_id": "folder-1", "llm_model": "gpt://folder-1/custom/latest"}})
    store = MemorySecretStore()
    store.set("yandex.api_key", "key-from-store")
    transport = create_sourcing_ai_transport(settings, store)
    assert transport.settings.yandex.model == "gpt://folder-1/custom/latest"
    assert transport.settings.yandex.base_url == "https://ai.api.cloud.yandex.net/v1"


def test_q2_environment_model_overrides_app_settings(tmp_path, monkeypatch):
    from averon_import.services.app_settings import AppSettingsService
    from averon_import.services.secrets import MemorySecretStore

    monkeypatch.setenv("AVERON_YANDEX_AI_MODEL", "gpt://env/model/latest")
    monkeypatch.delenv("AVERON_YANDEX_AI_BASE_URL", raising=False)
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"llm_model": "gpt://settings/model/latest"}})
    transport = create_sourcing_ai_transport(settings, MemorySecretStore())
    assert transport.settings.yandex.model == "gpt://env/model/latest"


def test_q3_secret_store_yandex_key_configures_sourcing_ai(tmp_path, monkeypatch):
    from averon_import.services.app_settings import AppSettingsService
    from averon_import.services.secrets import MemorySecretStore, YANDEX_AI_API_KEY

    for name in ("AVERON_YANDEX_AI_MODEL", "AVERON_YANDEX_AI_BASE_URL", "AVERON_YANDEX_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"llm_model": "gpt://folder/qwen/latest"}})
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, "stored-key")
    transport = create_sourcing_ai_transport(settings, store)
    assert transport.providers["yandex"].configured is True


def test_q_dedicated_ai_does_not_reuse_vision_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"folder_id": "folder-1", "llm_model": "gpt://folder-1/qwen/latest"}})
    store = MemorySecretStore()
    store.set(YANDEX_API_KEY, "vision-only")
    transport = create_sourcing_ai_transport(settings, store)
    provider = transport.providers["yandex"]
    assert provider.configured is False
    assert provider.settings.api_key == ""


def test_q_dedicated_ai_secret_configures_sourcing_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    settings_dir = tmp_path / "settings"
    secrets_dir = tmp_path / "secrets"
    settings = AppSettingsService(settings_dir)
    settings.update({"yandex": {"folder_id": "folder-1", "llm_model": "gpt://folder-1/qwen/latest"}})
    store = InsecureFileSecretStore(secrets_dir)
    store.set(YANDEX_AI_API_KEY, "dedicated-ai")
    first = create_sourcing_ai_transport(settings, store)
    restarted_settings = AppSettingsService(settings_dir)
    restarted_store = InsecureFileSecretStore(secrets_dir)
    second = create_sourcing_ai_transport(restarted_settings, restarted_store)
    assert first.providers["yandex"].configured is True
    assert second.providers["yandex"].configured is True
    assert second.providers["yandex"].settings.api_key == "dedicated-ai"


def test_q_dedicated_ai_environment_overrides_stored_ai_key(tmp_path, monkeypatch):
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"folder_id": "folder-1", "llm_model": "gpt://folder-1/qwen/latest"}})
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, "stored-ai")
    monkeypatch.setenv("AVERON_YANDEX_AI_API_KEY", "environment-ai")
    transport = create_sourcing_ai_transport(settings, store)
    assert transport.providers["yandex"].settings.api_key == "environment-ai"


def test_q_dedicated_ai_secret_never_appears_in_public_config_or_health(tmp_path):
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"folder_id": "folder-1", "llm_model": "gpt://folder-1/qwen/latest"}})
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, "private-ai-secret")
    transport = create_sourcing_ai_transport(settings, store)
    ai = SourcingAIService(transport)
    public = json.dumps({"config": ai.public_config(), "health": transport.health()}, ensure_ascii=False)
    assert "private-ai-secret" not in public
    assert "api_key" not in public


def test_q4_sourcing_public_config_never_exposes_secret(tmp_path, monkeypatch):
    from averon_import.services.app_settings import AppSettingsService
    from averon_import.services.secrets import MemorySecretStore

    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    settings = AppSettingsService(tmp_path)
    settings.update({"yandex": {"llm_model": "gpt://folder/qwen/latest"}})
    store = MemorySecretStore()
    store.set("yandex.api_key", "super-secret-key")
    ai = SourcingAIService(create_sourcing_ai_transport(settings, store))
    encoded = json.dumps(ai.public_config(), ensure_ascii=False)
    assert "super-secret-key" not in encoded
    assert "api_key" not in encoded


def test_q5_match_cannot_be_ranked_below_alternative():
    intent = make_intent(manufacturer="Preferred", preferred_attributes={"manufacturer": "Preferred"})
    match = OfferMatcher().match(intent, [make_offer(offer_id="match", manufacturer="Preferred")])[0]
    alternative = OfferMatcher().match(intent, [make_offer(offer_id="alternative", manufacturer="Other")])[0]
    assert match.decision == MatchDecision.MATCH
    assert alternative.decision == MatchDecision.ALTERNATIVE
    provider = FakeAIProvider(json.dumps({"offer_order": ["alternative", "match"]}))
    ranked, _ = SourcingAIService(FakeAIService(provider)).rank_matches(intent, [alternative, match])
    assert [item.offer.offer_id for item in ranked] == ["match", "alternative"]


def test_q6_likely_match_cannot_be_ranked_below_review():
    offer_likely = make_offer(offer_id="likely")
    offer_review = make_offer(offer_id="review")
    likely = MatchResult(offer=offer_likely, decision=MatchDecision.LIKELY_MATCH, rank=1)
    review = MatchResult(offer=offer_review, decision=MatchDecision.REVIEW, rank=2)
    provider = FakeAIProvider(json.dumps({"offer_order": ["review", "likely"]}))
    ranked, _ = SourcingAIService(FakeAIService(provider)).rank_matches(make_intent(), [review, likely])
    assert [item.offer.offer_id for item in ranked] == ["likely", "review"]


def test_q7_qwen_may_reorder_two_match_offers():
    first = MatchResult(offer=make_offer(offer_id="first"), decision=MatchDecision.MATCH, rank=1)
    second = MatchResult(offer=make_offer(offer_id="second"), decision=MatchDecision.MATCH, rank=2)
    provider = FakeAIProvider(json.dumps({"offer_order": ["second", "first"]}))
    ranked, _ = SourcingAIService(FakeAIService(provider)).rank_matches(make_intent(), [first, second])
    assert [item.offer.offer_id for item in ranked] == ["second", "first"]


def test_q8_explicit_source_article_cannot_be_replaced_by_ai():
    provider = FakeAIProvider(json.dumps({"article": "HALLUCINATED", "normalized_name": "Клапан"}))
    row = {"id": "article-row", "name": "Клапан", "code": "SRC-42", "quantity": "1"}
    fallback = build_fallback_intent(row)
    intent, _ = SourcingAIService(FakeAIService(provider)).understand(row, fallback)
    assert intent.article == "SRC-42"


def test_q9_explicit_source_manufacturer_cannot_be_replaced_by_ai():
    provider = FakeAIProvider(json.dumps({"manufacturer": "Unrelated", "normalized_name": "Клапан"}))
    row = {"id": "manufacturer-row", "name": "Клапан", "manufacturer": "Источник", "quantity": "1"}
    fallback = build_fallback_intent(row)
    intent, _ = SourcingAIService(FakeAIService(provider)).understand(row, fallback)
    assert intent.manufacturer == "Источник"


def test_q10_unsupported_ai_numeric_attribute_is_not_hard_required():
    provider = FakeAIProvider(json.dumps({
        "normalized_name": "Насос",
        "attributes": {"voltage": 380},
        "required_attributes": {"voltage": 380},
    }))
    row = {"id": "numeric-row", "name": "Насос", "quantity": "1"}
    fallback = build_fallback_intent(row)
    intent, _ = SourcingAIService(FakeAIService(provider)).understand(row, fallback)
    assert "voltage" not in intent.required_attributes
    assert intent.preferred_attributes["voltage"] == 380
    assert "ai_inferred_attribute:voltage" in intent.uncertainties


def test_q11_deterministic_dn50_remains_hard_required():
    row = {"id": "dn-row", "name": "Клапан Ду50", "quantity": "1"}
    fallback = build_fallback_intent(row)
    assert fallback.required_attributes["diameter"] == 50


def test_q12_qwen_ranking_preserves_all_offer_commercial_facts():
    offer = make_offer(
        offer_id="commercial",
        source_item_id="source-commercial",
        price=Decimal("77.70"),
        currency="RUB",
        availability=False,
        availability_text="Под заказ",
        url="https://supplier.example/real",
        article="REAL-1",
        manufacturer="Real maker",
    )
    match = MatchResult(offer=offer, decision=MatchDecision.MATCH, rank=1)
    other = MatchResult(offer=make_offer(offer_id="other"), decision=MatchDecision.MATCH, rank=2)
    provider = FakeAIProvider(json.dumps({"offer_order": ["other", "commercial"]}))
    ranked, _ = SourcingAIService(FakeAIService(provider)).rank_matches(make_intent(), [match, other])
    preserved = next(item.offer for item in ranked if item.offer.offer_id == "commercial")
    assert preserved.model_dump(mode="json")["price"] == "77.70"
    assert preserved.url == "https://supplier.example/real"
    assert preserved.availability is False
    assert preserved.article == "REAL-1"
    assert preserved.manufacturer == "Real maker"


def test_q13_malformed_qwen_json_uses_safe_fallback():
    provider = FakeAIProvider("{broken")
    row = {"id": "malformed", "name": "Насос", "quantity": "1"}
    fallback = build_fallback_intent(row)
    intent, warnings = SourcingAIService(FakeAIService(provider)).understand(row, fallback)
    assert intent == fallback
    assert warnings and "fallback" in warnings[0]


def test_q14_qwen_outage_uses_safe_fallback_without_raw_error():
    class OutageProvider(FakeAIProvider):
        def complete(self, messages, **kwargs):
            raise RuntimeError("HTTP 503 Authorization secret should not leak")

    row = {"id": "outage", "name": "Насос", "quantity": "1"}
    fallback = build_fallback_intent(row)
    intent, warnings = SourcingAIService(FakeAIService(OutageProvider(""))).understand(row, fallback)
    assert intent == fallback
    assert warnings and "503" not in warnings[0] and "secret" not in warnings[0]


def test_q15_source_row_remains_unchanged_after_ai_enrichment(tmp_path):
    provider = FakeAIProvider(json.dumps({"normalized_name": "Клапан", "attributes": {"diameter": 50}}))
    row = {"id": "q15", "name": "Клапан Ду50", "quantity": "2", "unit": "шт."}
    before = json.loads(json.dumps(row, ensure_ascii=False))
    service = SourcingService(
        {"stub": StubProvider([])},
        default_provider="stub",
        ai=SourcingAIService(FakeAIService(provider)),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    service.understand_row(row)
    assert row == before
