from __future__ import annotations

import hashlib
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
from averon_import.services.sourcing.matching import OfferMatcher, recommended_offer
from averon_import.services.sourcing.models import (
    MatchDecision,
    MatchResult,
    Offer,
    ProductIntent,
    ProjectSourcingResult,
    SourcingRankingResult,
    SourcingNotice,
    SourcingResult,
    SourcingSourceMode,
    dedupe_sourcing_notices,
)
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
    extract_generic_attributes,
)
from averon_import.services.sourcing.runtime import create_sourcing_ai_transport
from averon_import.services.sourcing.providers.base import SourcingProvider
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.service import SourcingService, _price_unit_family, _units_compatible


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


class RowOffersProvider:
    key = "row-offers"
    label = "Тестовый поставщик"

    def __init__(self, offers_by_row):
        self.offers_by_row = offers_by_row

    def search(self, intent, *, limit=20):
        return list(self.offers_by_row.get(intent.source_row_id, ()))[:limit]

    def stats(self):
        return {"item_count": sum(len(items) for items in self.offers_by_row.values()), "catalog_version": "row-1"}


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
        "offer_order": ["second", "first"],
        "evidence": {"second": ["closer model"]},
    }))
    ai = SourcingAIService(FakeAIService(provider))
    ranked, warnings = ai.rank_matches(make_intent(), matches)
    assert not warnings
    assert [item.offer.offer_id for item in ranked] == ["second", "first"]
    assert ranked[0].offer.price == Decimal("20")
    assert ranked[0].offer.url == ""
    assert ranked[0].ai_evidence == {"reasons": ["closer model"]}


@pytest.mark.parametrize(
    ("response", "expected_ids", "invalid"),
    [
        ({"offer_order": ["b"]}, ["b", "a"], False),
        ({"offer_order": ["a", "a", "b"]}, ["a", "b"], True),
        ({"offer_order": ["a", "unknown", "b"]}, ["a", "b"], True),
        ({"offer_order": []}, ["a", "b"], True),
        ({"offer_order": "b"}, ["a", "b"], True),
        ({"offer_order": ["b"], "evidence": []}, ["a", "b"], True),
        ("not-json", ["a", "b"], True),
    ],
    ids=["partial-completes-deterministically", "duplicate-id", "unknown-id", "empty", "wrong-type", "wrong-evidence-type", "invalid-json"],
)
def test_m6b_ai_ranking_response_contract_falls_back_safely(response, expected_ids, invalid):
    intent = make_intent()
    offers = [
        make_offer(offer_id="a", price=Decimal("10")),
        make_offer(offer_id="b", price=Decimal("20")),
    ]
    deterministic = OfferMatcher().match(intent, offers)
    provider = FakeAIProvider(
        response if isinstance(response, str) else json.dumps(response)
    )

    result = SourcingAIService(FakeAIService(provider)).rank_matches(intent, deterministic)

    assert [item.offer.offer_id for item in result.matches] == expected_ids
    assert len({(item.offer.provider, item.offer.offer_id) for item in result.matches}) == 2
    assert {item.offer.offer_id: item.decision for item in result.matches} == {
        item.offer.offer_id: item.decision for item in deterministic
    }
    if invalid:
        assert result.notices[0].code == "AI_INVALID_RESPONSE"
        assert "детерминированный порядок" in result.warnings[0]
        assert all(item.ai_evidence == {} for item in result.matches)
    else:
        assert result.notices == []
        assert result.warnings == []


def test_m6b_ai_ranking_uses_composite_candidate_identity():
    intent = make_intent()
    etm = make_offer(offer_id="same-id", provider="etm_ipro")
    duplicate_etm = make_offer(
        offer_id="same-id", provider="etm_ipro", price=Decimal("999"),
    )
    lemana = make_offer(offer_id="same-id", provider="lemana_b2b")
    deterministic = OfferMatcher().match(intent, [etm, duplicate_etm, lemana])

    class ReverseCandidateProvider(FakeAIProvider):
        def complete(self, messages, **kwargs):
            request = json.loads(messages[-1]["content"])
            candidate_ids = [item["candidate_id"] for item in request["offers"]]
            return json.dumps({"offer_order": list(reversed(candidate_ids))})

    provider = ReverseCandidateProvider("")

    result = SourcingAIService(FakeAIService(provider)).rank_matches(intent, deterministic)

    assert [(item.offer.provider, item.offer.offer_id) for item in result.matches] == [
        ("lemana_b2b", "same-id"),
        ("etm_ipro", "same-id"),
    ]


def test_m6b_ai_ranking_does_not_change_deterministic_recommendation():
    intent = make_intent(model="K-100", required_attributes={"power": 1})
    stronger = make_offer(
        offer_id="a-strong", attributes={"model": "K-100", "power": 1},
    )
    weaker = make_offer(
        offer_id="b-weak", title="Valve K-100", attributes={"power": 1},
    )
    deterministic = OfferMatcher().match(intent, [stronger, weaker])
    assert recommended_offer(deterministic).offer_id == "a-strong"

    provider = RowOffersProvider({"row-1": [weaker, stronger]})
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
            "offer_order": ["b-weak", "a-strong"],
        })))),
        cache=SourcingCache(),
    )
    result = service.search_intent(intent, ai_rerank=True)

    assert [item.offer.offer_id for item in result.match_results] == ["b-weak", "a-strong"]
    assert result.recommended_offer.offer_id == "a-strong"


def test_ai_ranking_notices_are_request_local_without_last_notice_side_channel():
    class RaisingProvider(FakeAIProvider):
        def __init__(self, error):
            super().__init__("")
            self.error = error

        def complete(self, messages, **kwargs):
            raise self.error

    matches = OfferMatcher().match(
        make_intent(),
        [make_offer(offer_id="first"), make_offer(offer_id="second")],
    )
    invalid_result = SourcingAIService(
        FakeAIService(RaisingProvider(ValueError("AI response did not contain JSON")))
    ).rank_matches(make_intent(), matches)
    unavailable_ai = SourcingAIService(
        FakeAIService(RaisingProvider(RuntimeError("temporary provider outage")))
    )
    unavailable_result = unavailable_ai.rank_matches(make_intent(), matches)

    assert isinstance(invalid_result, SourcingRankingResult)
    assert invalid_result.notices[0].code == "AI_INVALID_RESPONSE"
    assert unavailable_result.notices[0].code == "AI_UNAVAILABLE"
    assert invalid_result.notices[0].code == "AI_INVALID_RESPONSE"
    assert not hasattr(unavailable_ai, "last_notices")


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


def test_q16_ai_inferred_preferred_attribute_does_not_downgrade_match():
    intent = make_intent(
        evidence={
            "attribute_origins": {
                "diameter": "source_deterministic",
                "pressure": "source_deterministic",
                "mounting_type": "ai_inferred",
            }
        },
        preferred_attributes={"mounting_type": "wall"},
    )
    result = OfferMatcher().match(intent, [make_offer()])[0]

    assert result.decision == MatchDecision.MATCH
    assert "mounting_type" not in result.deterministic_evidence["preferred_differences"]
    assert intent.preferred_attributes["mounting_type"] == "wall"


def test_q17_ai_inferred_required_attribute_is_advisory_not_missing():
    intent = make_intent(
        required_attributes={"diameter": 50, "pressure": 16, "mounting_type": "wall"},
        evidence={
            "attribute_origins": {
                "diameter": "source_deterministic",
                "pressure": "source_deterministic",
                "mounting_type": "ai_inferred",
            }
        },
    )
    result = OfferMatcher().match(intent, [make_offer()])[0]

    assert result.decision == MatchDecision.MATCH
    assert "mounting_type" not in result.missing_attributes


def test_q18_source_required_power_mismatch_still_rejects():
    intent = make_intent(
        attributes={"power": 0.75},
        required_attributes={"power": 0.75},
        evidence={"attribute_origins": {"power": "source_deterministic"}},
    )
    result = OfferMatcher().match(intent, [make_offer(attributes={"power": 1.0})])[0]

    assert result.decision == MatchDecision.REJECT
    assert "power" in result.conflicting_attributes


def test_q19_source_preferred_attribute_difference_stays_alternative():
    intent = make_intent(
        preferred_attributes={"material": "steel"},
        evidence={"attribute_origins": {"material": "source_deterministic"}},
    )
    result = OfferMatcher().match(intent, [make_offer()])[0]

    assert result.decision == MatchDecision.ALTERNATIVE
    assert "material" in result.deterministic_evidence["preferred_differences"]


def test_q20_ai_grounded_attribute_keeps_existing_matching_behavior():
    intent = make_intent(
        required_attributes={"diameter": 50, "pressure": 16, "mounting_type": "wall"},
        evidence={
            "attribute_origins": {
                "diameter": "source_deterministic",
                "pressure": "source_deterministic",
                "mounting_type": "ai_grounded",
            }
        },
    )
    result = OfferMatcher().match(
        intent,
        [make_offer(attributes={"diameter": 50, "pressure": 16, "mounting_type": "wall"})],
    )[0]

    assert result.decision == MatchDecision.MATCH
    assert "mounting_type" in result.matched_attributes


def test_q21_source_required_attribute_missing_still_reviews():
    intent = make_intent(
        required_attributes={"diameter": 50, "pressure": 16, "power": 11},
        evidence={
            "attribute_origins": {
                "diameter": "source_deterministic",
                "pressure": "source_deterministic",
                "power": "source_deterministic",
            }
        },
    )
    result = OfferMatcher().match(intent, [make_offer()])[0]

    assert result.decision == MatchDecision.REVIEW
    assert "power" in result.missing_attributes


def test_q22_cyrillic_latin_product_identifier_is_equivalent():
    intent = make_intent(
        model="КЭВ-9П2012Е",
        article="КЭВ-9П2012Е",
        manufacturer="Тепломаш",
        attributes={},
        required_attributes={},
    )
    offer = make_offer(
        article="KEV-9P2012E",
        manufacturer="Тепломаш",
        attributes={"model": "KEV-9P2012E"},
    )

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.MATCH
    assert result.conflicting_attributes == []
    assert set(result.matched_attributes) >= {"article", "model", "manufacturer"}


@pytest.mark.parametrize(
    ("key", "expected", "actual"),
    [
        ("dimensions", "20×30", "20×40"),
        ("dimensions", "32×3,2", "32×4"),
        ("material", "Сталь 20", "Алюминий 20"),
    ],
)
def test_m6a_composite_and_material_conflicts_reject(key, expected, actual):
    intent = make_intent(
        model="",
        normalized_name="",
        manufacturer="",
        article="",
        attributes={key: expected},
        required_attributes={key: expected},
        preferred_attributes={},
        evidence={"attribute_origins": {key: "source_deterministic"}},
    )
    offer = make_offer(attributes={key: actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.REJECT
    assert result.conflicting_attributes == [key]


@pytest.mark.parametrize(
    ("key", "expected", "actual"),
    [
        ("dimensions", "20×30", "20x30"),
        ("power", 0.75, "750 W"),
        ("power", {"value": 1, "unit": "kW"}, {"value": 1000, "unit": "Вт"}),
    ],
)
def test_m6a_safe_composite_and_unit_normalizations_still_match(key, expected, actual):
    intent = make_intent(
        model="",
        normalized_name="",
        manufacturer="",
        article="",
        attributes={key: expected},
        required_attributes={key: expected},
        preferred_attributes={},
    )
    offer = make_offer(attributes={key: actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.MATCH


@pytest.mark.parametrize(
    ("expected", "actual"),
    [("M200", "M2000"), ("ABC-100", "ABC-1000")],
)
def test_m6a_explicit_model_prefix_collision_is_an_alternative(expected, actual):
    intent = make_intent(
        model=expected,
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    offer = make_offer(attributes={"model": actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.ALTERNATIVE
    assert result.deterministic_evidence["preferred_differences"] == ["model"]


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("ABC-100", "ABC-100-PLUS"),
        ("ABC-100", "ABC-100 V2"),
        ("A-01", "B-A-01"),
    ],
)
def test_m6a_r1_structured_model_requires_complete_identity(expected, actual):
    intent = make_intent(
        model=expected,
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    offer = make_offer(attributes={"model": actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.ALTERNATIVE
    assert result.matched_attributes == []
    assert result.deterministic_evidence["preferred_differences"] == ["model"]


@pytest.mark.parametrize(
    ("expected", "actual"),
    [("ABC100", "ABC-100"), ("П17", "П 17"), ("КЭВ-9П2012Е", "KEV-9P2012E")],
)
def test_m6a_r1_structured_model_keeps_safe_normalization(expected, actual):
    intent = make_intent(
        model=expected,
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    offer = make_offer(attributes={"model": actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.MATCH
    assert result.matched_attributes == ["model"]


@pytest.mark.parametrize(
    ("expected", "actual", "decision"),
    [
        ("ABC-100", "ABC-100", MatchDecision.MATCH),
        ("П17", "П 17", MatchDecision.MATCH),
        ("ABC-100", "ABC-100-PLUS", MatchDecision.REJECT),
        ("ABC-100", "ABC-100 V2", MatchDecision.REJECT),
        ("A-01", "B-A-01", MatchDecision.REJECT),
    ],
)
def test_m6a_r1_required_model_attribute_uses_strict_identity(expected, actual, decision):
    intent = make_intent(
        model="",
        attributes={},
        required_attributes={"model": expected},
        preferred_attributes={},
    )
    offer = make_offer(attributes={"model": actual})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == decision
    if decision == MatchDecision.REJECT:
        assert result.conflicting_attributes == ["model"]


@pytest.mark.parametrize(
    ("model", "title", "decision", "source"),
    [
        ("ABC-100", "Насос ABC-100", MatchDecision.LIKELY_MATCH, "title"),
        ("П17", "Смеситель П 17", MatchDecision.LIKELY_MATCH, "title"),
        ("КЭВ-9П2012Е", "Завеса KEV-9P2012E", MatchDecision.LIKELY_MATCH, "title"),
        ("ABC-100", "Насос ABC-100, 2 кВт", MatchDecision.LIKELY_MATCH, "title"),
        ("ABC-100", "Насос ABC-100, 2 кВт, 220 В", MatchDecision.LIKELY_MATCH, "title"),
        ("ABC-100", "Насос ABC-100, 2 кВт; 220 В", MatchDecision.LIKELY_MATCH, "title"),
        ("ABC-100", "Насос ABC-100, 2 кВт / 220 В", MatchDecision.LIKELY_MATCH, "title"),
        ("ABC-100", "Насос ABC-100, 2 кВт, ABC-200", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100, 2 кВт, Type X", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100, 2 кВт, ревизия B", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100-220 В", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100 V2", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100-PLUS", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100 -PLUS", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100 / V2", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100-", MatchDecision.REVIEW, ""),
        ("A-01", "Механизм B-A-01", MatchDecision.REVIEW, ""),
        ("ABC-100", "Насос ABC-100 Type X", MatchDecision.REVIEW, ""),
    ],
)
def test_m6a_r2_title_model_requires_safe_context(model, title, decision, source):
    intent = make_intent(
        model=model,
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    offer = make_offer(title=title, attributes={})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == decision
    assert "model" not in result.matched_attributes
    assert result.conflicting_attributes == []
    if source:
        assert result.supporting_attributes == ["model"]
        assert result.deterministic_evidence["model_evidence_source"] == source
    else:
        assert result.supporting_attributes == []
        assert result.missing_attributes == ["model"]


def test_m6a_r3_policy_revision_invalidates_r2_search_cache(tmp_path):
    intent = make_intent()
    old_payload = intent.model_dump(mode="json")
    old_payload.pop("evidence", None)
    old_payload.pop("uncertainties", None)
    origins = intent.evidence.get("attribute_origins", {})
    decision_attributes = sorted(set(intent.required_attributes) | set(intent.preferred_attributes))
    old_payload["attribute_origins"] = {
        key: ("advisory" if str(origins.get(key) or "").strip() == "ai_inferred" else "enforced")
        for key in decision_attributes
    }
    old_payload["matcher_policy_revision"] = "m6a-r2-title-model-variant-safety-v1"
    old_fingerprint = hashlib.sha256(
        json.dumps(old_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = tmp_path / "m6a-search-cache.json"
    stale_offer = make_offer(offer_id="old-policy-result")
    SourcingCache(cache_path).set(
        f"search:stub:stub-1:{old_fingerprint}:20",
        SourcingResult(
            intent=intent,
            offers=[stale_offer],
            match_results=OfferMatcher().match(intent, [stale_offer]),
        ),
    )

    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, request_intent, *, limit=20):
            self.calls += 1
            return super().search(request_intent, limit=limit)

    provider = CountingProvider([make_offer(offer_id="fresh-policy-result")])
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )

    result = service.search_intent(intent)

    assert result.offers[0].offer_id == "fresh-policy-result"
    assert result.timings["search_cache_hit"] == 0.0
    assert provider.calls == 1


@pytest.mark.parametrize("routed", [False, True], ids=["provider-only", "routed-fallback"])
def test_m6a_r2_title_variant_never_enters_project_confirmed_total(tmp_path, routed):
    offer = make_offer(
        offer_id="abc-100-v2",
        title="Насос ABC-100 V2",
        attributes={},
        price=Decimal("1250"),
    )
    provider = RowOffersProvider({"abc-row": [offer]})
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / f"cache-{routed}.json"),
    )
    rows = [{
        "id": "abc-row",
        "row_type": "item",
        "name": "Насос",
        "type_mark": "ABC-100",
        "quantity": "2",
        "unit": "шт.",
    }]

    if routed:
        result = service.search_project_routed(
            rows,
            source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
            ai_rerank=False,
        )
    else:
        result = service.search_project(rows, ai_rerank=False)

    assert result.positions_matched == 0
    assert result.positions_review == 1
    assert result.confirmed_totals == {}
    assert result.confirmed_total is None
    assert result.results[0].recommended_offer is None
    assert result.results[0].match_results[0].decision == MatchDecision.REVIEW
    if routed:
        assert result.results[0].route.final_source_kind == "provider"


@pytest.mark.parametrize("routed", [False, True], ids=["provider-only", "routed-fallback"])
@pytest.mark.parametrize(
    "title",
    [
        "Насос ABC-100, 2 кВт, ABC-200",
        "Насос ABC-100, 2 кВт, Type X",
        "Насос ABC-100, 2 кВт, ревизия B",
        "Насос ABC-100-220 В",
    ],
)
def test_m6a_r3_unvalidated_title_suffix_never_enters_confirmed_total(
    tmp_path, routed, title
):
    offer = make_offer(
        offer_id="abc-100-suffix-ambiguous",
        title=title,
        attributes={},
        price=Decimal("1250"),
    )
    provider = RowOffersProvider({"abc-row": [offer]})
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / f"r3-cache-{routed}-{abs(hash(title))}.json"),
    )
    rows = [{
        "id": "abc-row",
        "row_type": "item",
        "name": "Насос",
        "type_mark": "ABC-100",
        "quantity": "2",
        "unit": "шт.",
    }]

    if routed:
        result = service.search_project_routed(
            rows,
            source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
            ai_rerank=False,
        )
    else:
        result = service.search_project(rows, ai_rerank=False)

    assert result.positions_matched == 0
    assert result.positions_review == 1
    assert result.confirmed_totals == {}
    assert result.confirmed_total is None
    assert result.results[0].recommended_offer is None
    assert result.results[0].match_results[0].decision == MatchDecision.REVIEW


def test_m6a_ai_ranking_cannot_promote_technical_conflict():
    intent = make_intent(
        model="",
        normalized_name="",
        attributes={"dimensions": "20x30"},
        required_attributes={"dimensions": "20x30"},
        preferred_attributes={},
    )
    rejected = make_offer(offer_id="rejected", attributes={"dimensions": "20x40"})
    safe = make_offer(offer_id="safe", attributes={"dimensions": "20x30"})
    matches = OfferMatcher().match(intent, [rejected, safe])
    ai = SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
        "offer_order": ["rejected", "safe"],
    }))))

    ranked = ai.rank_matches(intent, matches).matches

    assert [item.offer.offer_id for item in ranked] == ["safe", "rejected"]
    assert ranked[-1].decision == MatchDecision.REJECT
    assert ranked[-1].conflicting_attributes == ["dimensions"]


def test_m6a_model_in_title_requires_complete_token_sequence():
    intent = make_intent(model="M200", attributes={}, required_attributes={}, preferred_attributes={})
    offer = make_offer(title="Насос M2000", attributes={})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.REVIEW
    assert result.missing_attributes == ["model"]


def test_m6a_model_separators_remain_normalized_without_prefix_matching():
    intent = make_intent(model="ABC-100", attributes={}, required_attributes={}, preferred_attributes={})
    offer = make_offer(attributes={"model": "abc 100"})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.MATCH


def test_m6a_title_model_allows_separator_inside_complete_identifier():
    intent = make_intent(model="П17", attributes={}, required_attributes={}, preferred_attributes={})
    offer = make_offer(title="Смеситель П 17", attributes={})

    result = OfferMatcher().match(intent, [offer])[0]

    assert result.decision == MatchDecision.LIKELY_MATCH
    assert result.deterministic_evidence["model_evidence_source"] == "title"


def test_m6a_fingerprint_includes_only_decision_relevant_attribute_origins():
    source = make_intent(
        required_attributes={"mounting_type": "wall"},
        evidence={"attribute_origins": {"mounting_type": "source_deterministic"}},
    )
    inferred = source.model_copy(update={
        "evidence": {"attribute_origins": {"mounting_type": "ai_inferred"}},
    })
    unrelated = source.model_copy(update={"evidence": {"debug_note": "not used for matching"}})

    assert source.fingerprint != inferred.fingerprint
    assert source.fingerprint == unrelated.fingerprint


def test_m6a_matcher_policy_revision_does_not_reuse_legacy_search_cache(tmp_path):
    intent = make_intent()
    old_payload = intent.model_dump(mode="json")
    old_payload.pop("evidence", None)
    old_payload.pop("uncertainties", None)
    old_fingerprint = hashlib.sha256(
        json.dumps(old_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cache_path = tmp_path / "legacy-search-cache.json"
    legacy_offer = make_offer(offer_id="legacy-cached-offer")
    SourcingCache(cache_path).set(
        f"search:stub:stub-1:{old_fingerprint}:20",
        SourcingResult(
            intent=intent,
            offers=[legacy_offer],
            match_results=OfferMatcher().match(intent, [legacy_offer]),
        ),
    )

    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, request_intent, *, limit=20):
            self.calls += 1
            return super().search(request_intent, limit=limit)

    provider = CountingProvider([make_offer(offer_id="fresh-offer")])
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )

    result = service.search_intent(intent)

    assert result.offers[0].offer_id == "fresh-offer"
    assert provider.calls == 1


def test_m6a_search_cache_separates_origins_cold_warm_and_after_restart(tmp_path):
    offer = make_offer(
        attributes={"model": "DN50 PN16", "mounting_type": "ceiling"},
    )

    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, intent, *, limit=20):
            self.calls += 1
            return super().search(intent, limit=limit)

    cache_path = tmp_path / "m6a-search-cache.json"
    inferred = make_intent(
        required_attributes={"mounting_type": "wall"},
        evidence={"attribute_origins": {"mounting_type": "ai_inferred"}},
    )
    source = inferred.model_copy(update={
        "evidence": {"attribute_origins": {"mounting_type": "source_deterministic"}},
    })
    first_provider = CountingProvider([offer])
    first_service = SourcingService(
        {first_provider.key: first_provider},
        default_provider=first_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )

    cold = first_service.search_intent(inferred)
    assert cold.match_results[0].decision == MatchDecision.MATCH
    assert first_provider.calls == 1

    warm_provider = CountingProvider([offer])
    warm_service = SourcingService(
        {warm_provider.key: warm_provider},
        default_provider=warm_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )
    warm = warm_service.search_intent(source)
    assert warm.match_results[0].decision == MatchDecision.REJECT
    assert warm_provider.calls == 1

    restarted_provider = CountingProvider([offer])
    restarted_service = SourcingService(
        {restarted_provider.key: restarted_provider},
        default_provider=restarted_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )
    restarted = restarted_service.search_intent(source)
    assert restarted.match_results[0].decision == MatchDecision.REJECT
    assert restarted.timings["search_cache_hit"] == 1.0
    assert restarted_provider.calls == 0


@pytest.mark.parametrize(
    ("cold_rerank", "warm_rerank", "cold_order", "warm_order"),
    [
        (True, False, ["b", "a"], ["a", "b"]),
        (False, True, ["a", "b"], ["b", "a"]),
    ],
    ids=["ai-then-deterministic", "deterministic-then-ai"],
)
def test_m6b_search_cache_keeps_ai_ranking_request_local_across_restart(
    tmp_path, cold_rerank, warm_rerank, cold_order, warm_order
):
    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, intent, *, limit=20):
            self.calls += 1
            return super().search(intent, limit=limit)

    offers = [
        make_offer(offer_id="a", price=Decimal("10")),
        make_offer(offer_id="b", price=Decimal("20")),
    ]
    cache_path = tmp_path / "m6b-ranking-cache.json"
    first_provider = CountingProvider(offers)
    first_ai = SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
        "offer_order": ["b", "a"],
    }))))
    first_service = SourcingService(
        {first_provider.key: first_provider},
        default_provider=first_provider.key,
        ai=first_ai,
        cache=SourcingCache(cache_path),
    )
    cold = first_service.search_intent(make_intent(), ai_rerank=cold_rerank)

    restarted_provider = CountingProvider(offers)
    warm_ai = SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
        "offer_order": ["b", "a"],
    })))) if warm_rerank else SourcingAIService()
    restarted_service = SourcingService(
        {restarted_provider.key: restarted_provider},
        default_provider=restarted_provider.key,
        ai=warm_ai,
        cache=SourcingCache(cache_path),
    )
    warm = restarted_service.search_intent(make_intent(), ai_rerank=warm_rerank)

    assert [item.offer.offer_id for item in cold.match_results] == cold_order
    assert [item.offer.offer_id for item in warm.match_results] == warm_order
    assert first_provider.calls == 1
    assert restarted_provider.calls == 0
    assert warm.timings["search_cache_hit"] == 1.0
    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    search_entries = [
        value for key, value in persisted.items() if key.startswith("search:v2:")
    ]
    assert len(search_entries) == 1
    assert [item["offer"]["offer_id"] for item in search_entries[0]["match_results"]] == ["a", "b"]
    assert all(item["ai_evidence"] == {} for item in search_entries[0]["match_results"])


def test_m6b_ai_failure_is_request_local_and_restored_on_warm_cache(tmp_path):
    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, intent, *, limit=20):
            self.calls += 1
            return super().search(intent, limit=limit)

    class RaisingRankingProvider(FakeAIProvider):
        def complete(self, messages, **kwargs):
            self.calls += 1
            raise RuntimeError("temporary provider outage")

    offers = [make_offer(offer_id="a"), make_offer(offer_id="b")]
    cache_path = tmp_path / "m6b-ai-recovery-cache.json"

    def service(ai):
        provider = CountingProvider(offers)
        value = SourcingService(
            {provider.key: provider},
            default_provider=provider.key,
            ai=ai,
            cache=SourcingCache(cache_path),
        )
        return value, provider

    successful_ai = SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
        "offer_order": ["b", "a"],
    }))))
    successful_service, first_provider = service(successful_ai)
    success = successful_service.search_intent(make_intent(), ai_rerank=True)

    unavailable_ai = SourcingAIService(FakeAIService(RaisingRankingProvider("")))
    unavailable_service, unavailable_search_provider = service(unavailable_ai)
    unavailable = unavailable_service.search_intent(make_intent(), ai_rerank=True)

    restored_ai = SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
        "offer_order": ["a", "b"],
    }))))
    restored_service, restored_search_provider = service(restored_ai)
    restored = restored_service.search_intent(make_intent(), ai_rerank=True)

    assert [item.offer.offer_id for item in success.match_results] == ["b", "a"]
    assert [item.offer.offer_id for item in unavailable.match_results] == ["a", "b"]
    assert unavailable.notices[-1].code == "AI_UNAVAILABLE"
    assert all(item.ai_evidence == {} for item in unavailable.match_results)
    assert [item.offer.offer_id for item in restored.match_results] == ["a", "b"]
    assert not any(notice.code == "AI_UNAVAILABLE" for notice in restored.notices)
    assert not any("Qwen недоступен" in warning for warning in restored.warnings)
    assert first_provider.calls == 1
    assert unavailable_search_provider.calls == restored_search_provider.calls == 0
    assert unavailable.timings["search_cache_hit"] == restored.timings["search_cache_hit"] == 1.0


def test_m6b_search_cache_rebinds_current_understanding_warnings_and_ai_mode(tmp_path):
    from averon_import.services.sourcing.models import (
        ProductUnderstandingProvenance,
        ProductUnderstandingResult,
    )

    intent = make_intent()
    cache_path = tmp_path / "m6b-current-understanding-cache.json"
    prior_notice = SourcingNotice(code="AI_TIMEOUT", message="old AI warning")
    current_notice = SourcingNotice(code="AI_NOT_CONFIGURED", message="current AI warning")
    qwen_understanding = ProductUnderstandingResult(
        baseline_intent=intent,
        resolved_intent=intent,
        warnings=["old AI understanding warning"],
        notices=[prior_notice],
        mode="qwen",
        provenance=ProductUnderstandingProvenance(
            provider="test", parser_revision="m6b-test",
        ),
    )
    fallback_understanding = ProductUnderstandingResult(
        baseline_intent=intent,
        resolved_intent=intent,
        warnings=["current AI understanding warning"],
        notices=[current_notice],
        mode="fallback",
        provenance=ProductUnderstandingProvenance(
            provider="test", parser_revision="m6b-test",
        ),
    )
    first_provider = StubProvider([make_offer(offer_id="cached-a")])
    first = SourcingService(
        {first_provider.key: first_provider},
        default_provider=first_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    ).search_intent(
        intent,
        understanding=qwen_understanding,
        warnings=qwen_understanding.warnings,
        notices=qwen_understanding.notices,
        ai_rerank=False,
    )
    warm_provider = StubProvider([make_offer(offer_id="must-not-be-read")])
    warm = SourcingService(
        {warm_provider.key: warm_provider},
        default_provider=warm_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    ).search_intent(
        intent,
        understanding=fallback_understanding,
        warnings=fallback_understanding.warnings,
        notices=fallback_understanding.notices,
        ai_rerank=False,
    )

    assert first.ai_mode == "qwen"
    assert warm.ai_mode == "fallback"
    assert "current AI understanding warning" in warm.warnings
    assert "old AI understanding warning" not in warm.warnings
    assert all(item.code != "AI_TIMEOUT" for item in warm.notices)
    assert any(item.code == "AI_NOT_CONFIGURED" for item in warm.notices)
    assert warm.timings["search_cache_hit"] == 1.0
    assert warm.offers[0].offer_id == "cached-a"


def test_m6b_ai_and_cache_modes_do_not_promote_review_into_confirmed_total(tmp_path):
    class CountingRowProvider(RowOffersProvider):
        def __init__(self, offers_by_row):
            super().__init__(offers_by_row)
            self.calls = 0

        def search(self, intent, *, limit=20):
            self.calls += 1
            return super().search(intent, limit=limit)

    offer = make_offer(
        offer_id="ambiguous-review",
        title="Насос ABC-100 V2",
        attributes={},
        price=Decimal("500"),
    )
    cache_path = tmp_path / "m6b-confirmed-total-cache.json"
    rows = [{
        "id": "abc-row",
        "row_type": "item",
        "name": "Насос",
        "type_mark": "ABC-100",
        "quantity": "2",
        "unit": "шт.",
    }]
    first_provider = CountingRowProvider({"abc-row": [offer]})
    first_service = SourcingService(
        {first_provider.key: first_provider},
        default_provider=first_provider.key,
        ai=SourcingAIService(FakeAIService(FakeAIProvider(json.dumps({
            "offer_order": ["ambiguous-review"],
        })))),
        cache=SourcingCache(cache_path),
    )
    first = first_service.search_project(rows, ai_rerank=True)

    warm_provider = CountingRowProvider({"abc-row": [offer]})
    warm_service = SourcingService(
        {warm_provider.key: warm_provider},
        default_provider=warm_provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    )
    warm = warm_service.search_project(rows, ai_rerank=False)

    for result in (first, warm):
        assert result.positions_matched == 0
        assert result.positions_review == 1
        assert result.confirmed_totals == {}
        assert result.confirmed_total is None
        assert result.results[0].recommended_offer is None
        assert result.results[0].match_results[0].decision == MatchDecision.REVIEW
    assert first_provider.calls == 1
    assert warm_provider.calls == 0
    assert warm.timings["search_cache_hits"] == 1.0


def test_m6b_search_cache_separates_catalog_version_and_intent(tmp_path):
    class VersionedProvider(StubProvider):
        def __init__(self, offers, version):
            super().__init__(offers)
            self.version = version
            self.calls = 0

        def search(self, intent, *, limit=20):
            self.calls += 1
            return super().search(intent, limit=limit)

        def stats(self):
            return {"item_count": len(self.offers), "catalog_version": self.version}

    cache = SourcingCache(tmp_path / "m6b-version-cache.json")
    intent = make_intent()
    first_provider = VersionedProvider([make_offer(offer_id="catalog-v1")], "catalog-v1")
    first = SourcingService(
        {first_provider.key: first_provider},
        default_provider=first_provider.key,
        ai=SourcingAIService(),
        cache=cache,
    ).search_intent(intent, ai_rerank=False)
    warm_provider = VersionedProvider([make_offer(offer_id="ignored-warm")], "catalog-v1")
    warm = SourcingService(
        {warm_provider.key: warm_provider},
        default_provider=warm_provider.key,
        ai=SourcingAIService(),
        cache=cache,
    ).search_intent(intent, ai_rerank=False)
    changed_catalog_provider = VersionedProvider([make_offer(offer_id="catalog-v2")], "catalog-v2")
    changed_catalog = SourcingService(
        {changed_catalog_provider.key: changed_catalog_provider},
        default_provider=changed_catalog_provider.key,
        ai=SourcingAIService(),
        cache=cache,
    ).search_intent(intent, ai_rerank=False)
    changed_intent_provider = VersionedProvider([make_offer(offer_id="changed-intent")], "catalog-v2")
    changed_intent = SourcingService(
        {changed_intent_provider.key: changed_intent_provider},
        default_provider=changed_intent_provider.key,
        ai=SourcingAIService(),
        cache=cache,
    ).search_intent(intent.model_copy(update={"article": "NEW-ARTICLE"}), ai_rerank=False)

    assert first.offers[0].offer_id == warm.offers[0].offer_id == "catalog-v1"
    assert warm.timings["search_cache_hit"] == 1.0
    assert warm_provider.calls == 0
    assert changed_catalog.offers[0].offer_id == "catalog-v2"
    assert changed_catalog_provider.calls == 1
    assert changed_intent.offers[0].offer_id == "changed-intent"
    assert changed_intent_provider.calls == 1


def test_m6b_search_cache_uses_new_versioned_namespace(tmp_path):
    intent = make_intent()
    cache_path = tmp_path / "m6b-namespace-cache.json"
    stale_offer = make_offer(offer_id="stale-ai-ranked")
    stale_result = SourcingResult(
        intent=intent,
        offers=[stale_offer],
        match_results=OfferMatcher().match(intent, [stale_offer]),
    )
    SourcingCache(cache_path).set(
        f"search:stub:stub-1:{intent.fingerprint}:20",
        stale_result,
    )

    class CountingProvider(StubProvider):
        def __init__(self, offers):
            super().__init__(offers)
            self.calls = 0

        def search(self, request_intent, *, limit=20):
            self.calls += 1
            return super().search(request_intent, limit=limit)

    provider = CountingProvider([make_offer(offer_id="fresh-v2")])
    result = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(cache_path),
    ).search_intent(intent, ai_rerank=False)

    assert result.offers[0].offer_id == "fresh-v2"
    assert result.timings["search_cache_hit"] == 0.0
    assert provider.calls == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Бетон В15 П3 F200 W6", None),
        ("Мощность 200 Вт", 0.2),
        ("Насос 1,5 кВт", 1.5),
        ("Светильник 200 W", 0.2),
    ],
)
def test_m6a_power_extraction_respects_complete_unit_tokens(text, expected):
    attributes = extract_generic_attributes(text)

    assert attributes.get("power") == expected


def test_model_evidence_uses_explicit_model_then_title_then_article():
    intent = make_intent(
        model="Model-1",
        normalized_name="Устройство",
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )

    explicit = OfferMatcher().match(
        intent,
        [make_offer(offer_id="explicit", attributes={"model": "Model-1"})],
    )[0]
    assert explicit.decision == MatchDecision.MATCH
    assert explicit.matched_attributes == ["model"]
    assert explicit.supporting_attributes == []
    assert explicit.deterministic_evidence["model_evidence_source"] == "explicit_model"

    title = OfferMatcher().match(
        intent,
        [make_offer(offer_id="title", title="Устройство Model-1", article="ET-1", attributes={})],
    )[0]
    assert title.decision == MatchDecision.LIKELY_MATCH
    assert title.matched_attributes == []
    assert title.supporting_attributes == ["model"]
    assert title.deterministic_evidence["model_evidence_source"] == "title"

    article = OfferMatcher().match(
        intent,
        [make_offer(offer_id="article", title="Устройство", article="Model-1", attributes={})],
    )[0]
    assert article.decision == MatchDecision.LIKELY_MATCH
    assert article.supporting_attributes == ["model"]
    assert article.deterministic_evidence["model_evidence_source"] == "article"

    different = OfferMatcher().match(
        intent,
        [make_offer(offer_id="different", title="Устройство", article="OTHER", attributes={})],
    )[0]
    assert different.decision == MatchDecision.REVIEW
    assert different.missing_attributes == ["model"]
    assert different.deterministic_evidence["preferred_differences"] == []


def test_model_title_support_does_not_hide_ambiguity():
    model = "ПКУ-15-21.121-54У2"
    intent = make_intent(
        source_row_id="etm-row",
        source_text="Пост кнопочный ПКУ-15-21.121-54У2",
        normalized_name="Пост кнопочный",
        model=model,
        manufacturer="",
        article="",
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    offers = [
        make_offer(
            offer_id=offer_id,
            title=f"Пост кнопочный {model}",
            article=article,
            manufacturer="",
            attributes={},
        )
        for offer_id, article in (
            ("2667167", "A-1"),
            ("3394481", "A-2"),
            ("9396002", "A-3"),
            ("9536092", "ET054487"),
            ("1327846", "A-5"),
        )
    ]

    matches = OfferMatcher().match(intent, offers)

    assert all(item.decision == MatchDecision.LIKELY_MATCH for item in matches)
    assert all(item.supporting_attributes == ["model"] for item in matches)
    assert all(item.deterministic_evidence["model_evidence_source"] == "title" for item in matches)
    assert recommended_offer(matches) is None


def test_unique_explicit_model_evidence_beats_supporting_title_evidence():
    intent = make_intent(
        model="Model-1",
        normalized_name="Устройство",
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    supporting = make_offer(offer_id="supporting", title="Устройство Model-1", attributes={})
    explicit = make_offer(offer_id="explicit", title="Устройство", attributes={"model": "Model-1"})

    matches = OfferMatcher().match(intent, [supporting, explicit])

    assert matches[0].offer.offer_id == "explicit"
    assert matches[0].decision == MatchDecision.MATCH
    assert recommended_offer(matches).offer_id == "explicit"


def test_zero_deterministic_evidence_is_review_not_likely():
    intent = make_intent(
        model="",
        normalized_name="",
        manufacturer="",
        article="",
        attributes={},
        required_attributes={},
        preferred_attributes={},
    )
    matches = OfferMatcher().match(
        intent,
        [make_offer(title="Неподтвержденное предложение", attributes={})],
    )

    assert matches[0].decision == MatchDecision.REVIEW
    assert recommended_offer(matches) is None


def test_project_ambiguous_model_titles_remain_review_and_unresolved(tmp_path):
    model = "ПКУ-15-21.121-54У2"
    offers = [
        make_offer(
            offer_id=offer_id,
            title=f"Пост кнопочный {model}",
            article=article,
            attributes={},
        )
        for offer_id, article in (("2667167", "A-1"), ("9536092", "ET054487"))
    ]
    provider = RowOffersProvider({"etm-row": offers})
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )

    result = service.search_project([{
        "id": "etm-row",
        "row_type": "item",
        "name": "Пост кнопочный",
        "type_mark": model,
        "quantity": "1",
    }])

    assert result.positions_total == result.positions_processed == 1
    assert result.positions_matched == 0
    assert result.positions_review == 1
    assert result.unresolved_count == 1
    assert result.confirmed_totals == {}
    assert result.results[0].recommended_offer is None


def test_q23_same_alternative_class_prefers_more_supported_candidate():
    intent = make_intent(
        model="Model 10",
        manufacturer="NOBO",
        attributes={"power": 1},
        required_attributes={"power": 1},
        preferred_attributes={"features": "waterproof"},
    )
    weaker = make_offer(
        offer_id="adax-10",
        manufacturer="ADAX",
        title="Клапан фланцевый Model 10",
        attributes={"power": 1},
    )
    stronger = make_offer(
        offer_id="nobo-10",
        manufacturer="NOBO",
        attributes={"model": "Model 10", "power": 1},
    )

    results = OfferMatcher().match(intent, [weaker, stronger])

    assert [result.offer.offer_id for result in results] == ["nobo-10", "adax-10"]
    assert all(result.decision == MatchDecision.ALTERNATIVE for result in results)


def test_s15_quantity_times_price_is_calculated_without_mutating_row(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("10"))])
    row = {"id": "q1", "row_type": "item", "name": "Клапан", "quantity": "3", "unit": "шт."}
    result = service.search_project([row])
    assert result.estimated_total == Decimal("30")
    assert result.currency == "RUB"
    assert row["quantity"] == "3"


@pytest.mark.parametrize(
    ("source_unit", "price_unit"),
    [
        ("шт.", "шт"),
        ("штуки", "шт."),
        ("м", "m"),
        ("м²", "m2"),
        ("м3", "м³"),
        ("кг", "kg"),
        ("т.", "tonne"),
        ("комплект", "компл."),
        ("упаковка", "упак"),
        ("л", "litre"),
    ],
)
def test_price_unit_compatibility_accepts_only_explicit_equivalent_families(source_unit, price_unit):
    assert _units_compatible(source_unit, price_unit) is True


def test_shared_unit_normalization_preserves_every_legacy_sourcing_alias():
    expected = {
        "шт": "piece", "штука": "piece", "штуки": "piece", "штук": "piece",
        "м": "meter", "m": "meter", "м2": "square_meter", "m2": "square_meter",
        "м3": "cubic_meter", "m3": "cubic_meter", "кг": "kilogram", "kg": "kilogram",
        "т": "tonne", "ton": "tonne", "tonne": "tonne", "тонна": "tonne",
        "тонны": "tonne", "тонн": "tonne", "компл": "set", "комплект": "set",
        "упак": "pack", "упаковка": "pack", "л": "litre", "l": "litre",
        "litre": "litre", "литр": "litre", "литра": "litre", "литров": "litre",
    }
    assert {unit: _price_unit_family(unit) for unit in expected} == expected
    assert _price_unit_family("м²") == "square_meter"
    assert _price_unit_family("㎏") is None


@pytest.mark.parametrize(
    ("source_unit", "price_unit"),
    [
        ("шт.", "упак."),
        ("шт.", "компл."),
        ("м", "м²"),
        ("м²", "м³"),
        ("кг", "т"),
        ("неизвестно", "шт."),
        ("шт.", ""),
    ],
)
def test_price_unit_compatibility_rejects_mismatch_unknown_and_missing(source_unit, price_unit):
    assert _units_compatible(source_unit, price_unit) is False


def test_project_total_with_incompatible_units_is_withheld_without_changing_match(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("100"), price_unit="шт.")])
    result = service.search_project([{
        "id": "pack-row",
        "row_type": "item",
        "name": "Клапан",
        "quantity": "10",
        "unit": "упак.",
    }])

    assert result.positions_matched == 1
    assert result.results[0].match_results[0].decision in {MatchDecision.MATCH, MatchDecision.LIKELY_MATCH}
    assert result.confirmed_totals == {}
    assert result.confirmed_total is None
    assert result.unit_confirmation_count == 1
    assert [notice.code for notice in result.notices].count("PRICE_UNIT_REQUIRES_CONFIRMATION") == 1


def test_project_totals_accept_meter_equivalence_and_reject_square_meter(tmp_path):
    service = SourcingService(
        {RowOffersProvider.key: RowOffersProvider({
            "meter": [make_offer(offer_id="meter", price=Decimal("50"), price_unit="м")],
            "area": [make_offer(offer_id="area", price=Decimal("50"), price_unit="м²")],
        })},
        default_provider=RowOffersProvider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    result = service.search_project([
        {"id": "meter", "row_type": "item", "name": "Клапан", "quantity": "100", "unit": "m"},
        {"id": "area", "row_type": "item", "name": "Клапан", "quantity": "100", "unit": "м"},
    ])

    assert result.confirmed_totals == {"RUB": Decimal("5000")}
    assert result.unit_confirmation_count == 1


def test_project_total_with_missing_price_unit_requires_confirmation(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("100"), price_unit="")])
    result = service.search_project([{
        "id": "missing-price-unit",
        "row_type": "item",
        "name": "Клапан",
        "quantity": "10",
        "unit": "шт.",
    }])

    assert result.confirmed_totals == {}
    assert result.unit_confirmation_count == 1
    assert result.matched_unpriced_count == 0


def test_alternative_total_with_incompatible_units_is_withheld(tmp_path):
    service = service_for(tmp_path, [make_offer(
        manufacturer="Other",
        price=Decimal("100"),
        price_unit="шт.",
    )])
    result = service.search_project([{
        "id": "alternative-pack",
        "row_type": "item",
        "name": "Клапан",
        "manufacturer": "Preferred",
        "quantity": "10",
        "unit": "упак.",
    }])

    assert result.positions_alternatives == 1
    assert result.alternative_totals == {}
    assert result.alternative_total is None
    assert result.unit_confirmation_count == 1


def test_untrusted_quantity_is_not_double_counted_as_unit_confirmation(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("100"), price_unit="шт.")])
    result = service.search_project([{
        "id": "untrusted-quantity",
        "row_type": "item",
        "name": "Клапан",
        "quantity": "10",
        "unit": "упак.",
        "quantity_trusted": False,
    }])

    assert result.confirmed_totals == {}
    assert result.unit_confirmation_count == 0
    assert result.matched_unpriced_count == 0


def test_s16_unresolved_quantity_never_creates_trusted_total(tmp_path):
    service = service_for(tmp_path, [make_offer(price=Decimal("10"))])
    row = {"id": "q2", "row_type": "item", "name": "Клапан", "quantity": "", "status": "review"}
    result = service.search_project([row])
    assert result.estimated_total is None
    assert any("quantity requires confirmation" in warning for warning in result.warnings)
    assert [notice.code for notice in result.notices].count("QUANTITY_REQUIRES_CONFIRMATION") == 1
    assert result.notices[-1].user_visible is True


def test_s17_multiple_currencies_are_not_summed(tmp_path):
    rub = make_offer(offer_id="rub", title="Клапан RUB", price=Decimal("10"), currency="RUB")
    eur = make_offer(offer_id="eur", title="Насос EUR", price=Decimal("20"), currency="EUR")
    service = service_for(tmp_path, [rub, eur])
    rows = [
        {"id": "r", "row_type": "item", "name": "Клапан", "quantity": "1", "unit": "шт."},
        {"id": "e", "row_type": "item", "name": "Насос", "quantity": "1", "unit": "шт."},
    ]
    result = service.search_project(rows)
    assert result.estimated_total is None
    assert set(result.estimated_totals) == {"EUR", "RUB"}
    assert result.confirmed_total is None
    assert result.confirmed_totals == {"EUR": Decimal("20"), "RUB": Decimal("10")}
    assert any(
        notice.code == "MULTIPLE_CURRENCIES" and notice.severity == "warning"
        for notice in result.notices
    )


def test_sourcing_notice_dedupe_is_deterministic_by_stable_identity():
    first = SourcingNotice(code="TEST", severity="info", message="Первое")
    duplicate = SourcingNotice(code="TEST", severity="info", message="Первое")
    different_visibility = SourcingNotice(
        code="TEST", severity="info", message="Первое", user_visible=False,
    )

    assert dedupe_sourcing_notices([first, duplicate, different_visibility]) == [
        first, different_visibility,
    ]


def test_legacy_serialized_sourcing_models_without_notices_still_validate():
    understanding = SourcingAIService().understand_with_audit(
        {"id": "legacy", "name": "Клапан", "quantity": "1"},
        build_fallback_intent({"id": "legacy", "name": "Клапан", "quantity": "1"}),
    )
    sourcing = SourcingResult(intent=understanding.resolved_intent)
    project = ProjectSourcingResult(results=[sourcing])

    for model in (understanding, sourcing, project):
        payload = model.model_dump(mode="json")
        payload.pop("notices")
        restored = type(model).model_validate(payload)
        assert restored.notices == []


def test_legacy_understanding_cache_json_without_notices_still_reads(tmp_path):
    understanding = SourcingAIService().understand_with_audit(
        {"id": "legacy-cache", "name": "Клапан", "quantity": "1"},
        build_fallback_intent({"id": "legacy-cache", "name": "Клапан", "quantity": "1"}),
    )
    payload = understanding.model_dump(mode="json")
    payload.pop("notices")
    cache_path = tmp_path / "legacy-cache.json"
    cache_path.write_text(json.dumps({
        "__intents__": {
            "legacy-key": {
                "intent": understanding.resolved_intent.model_dump(mode="json"),
                "warnings": [],
                "understanding": payload,
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    restored = SourcingCache(cache_path).get_understanding("legacy-key")
    assert restored is not None
    assert restored.notices == []



def test_project_totals_separate_confirmed_alternative_and_unpriced(tmp_path):
    provider = RowOffersProvider({
        "confirmed": [make_offer(offer_id="confirmed", price=Decimal("10"))],
        "alternative": [make_offer(offer_id="alternative", manufacturer="Other", price=Decimal("5"))],
        "unpriced": [make_offer(offer_id="unpriced", title="Насос", price=None)],
        "untrusted": [make_offer(offer_id="untrusted", price=Decimal("20"))],
    })
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    rows = [
        {"id": "confirmed", "row_type": "item", "name": "Клапан", "quantity": "2", "unit": "шт."},
        {"id": "alternative", "row_type": "item", "name": "Клапан", "manufacturer": "Preferred", "quantity": "3", "unit": "шт."},
        {"id": "unpriced", "row_type": "item", "name": "Насос", "quantity": "1", "unit": "шт."},
        {"id": "untrusted", "row_type": "item", "name": "Клапан", "quantity": "2", "unit": "шт.", "status": "review"},
    ]

    result = service.search_project(rows)

    assert result.confirmed_total == Decimal("20")
    assert result.confirmed_totals == {"RUB": Decimal("20")}
    assert result.alternative_total == Decimal("15")
    assert result.alternative_totals == {"RUB": Decimal("15")}
    assert result.estimated_total == Decimal("20")
    assert result.estimated_totals == {"RUB": Decimal("20")}
    assert result.matched_unpriced_count == 1
    assert result.alternative_unpriced_count == 0
    assert result.unresolved_count == 0
    assert any("quantity requires confirmation" in warning for warning in result.warnings)


def test_review_identity_candidate_is_separate_from_weak_alternative(tmp_path):
    provider = RowOffersProvider({
        "identity": [
            make_offer(
                offer_id="identity-review",
                manufacturer="",
                attributes={"model": "Model-1"},
            ),
            make_offer(
                offer_id="weak-alternative",
                manufacturer="Other",
                title="Клапан фланцевый Model-1",
                attributes={"power": 1},
            ),
        ],
    })
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    intent = make_intent(
        source_row_id="identity",
        model="Model-1",
        manufacturer="Maker",
        attributes={"power": 1},
        required_attributes={"power": 1},
        preferred_attributes={},
    )

    result = service.search_intent(intent)

    assert result.recommended_offer.offer_id == "weak-alternative"
    assert result.review_candidate is not None
    assert result.review_candidate.offer.offer_id == "identity-review"
    assert result.review_candidate.decision == MatchDecision.REVIEW
    assert any(item.offer.offer_id == "weak-alternative" and item.decision == MatchDecision.ALTERNATIVE for item in result.match_results)


def test_match_remains_primary_over_review_candidate_and_alternative(tmp_path):
    provider = RowOffersProvider({
        "mixed": [
            make_offer(offer_id="review", manufacturer="", attributes={"model": "M-1"}),
            make_offer(offer_id="alternative", manufacturer="Other", attributes={"power": 1}),
            make_offer(offer_id="match", manufacturer="Maker", attributes={"model": "M-1", "power": 1}),
        ],
    })
    service = SourcingService({provider.key: provider}, default_provider=provider.key, ai=SourcingAIService())
    intent = make_intent(
        source_row_id="mixed",
        model="M-1",
        manufacturer="Maker",
        attributes={"power": 1},
        required_attributes={"power": 1},
        preferred_attributes={},
    )

    result = service.search_intent(intent)

    assert result.recommended_offer.offer_id == "match"
    assert result.review_candidate is not None
    assert result.review_candidate.offer.offer_id == "review"


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
        {"id": "1", "row_type": "item", "name": "Клапан", "quantity": "2", "unit": "шт."},
        {"id": "2", "row_type": "item", "name": "Насос", "quantity": "3", "unit": "шт."},
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
