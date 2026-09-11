from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SuggestionResolution,
)
from averon_import.services.sourcing.product_understanding import (
    PRODUCT_UNDERSTANDING_REVISION,
    SourcingAIService,
    build_fallback_intent,
    extract_generic_attributes,
    normalize_attribute_key,
)
from averon_import.services.sourcing.service import SourcingService


class FakeQwenProvider:
    configured = True

    def __init__(self, response: str, *, model: str = "gpt://folder/qwen/test"):
        self.response = response
        self.model = model
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeAITransport:
    def __init__(self, provider: FakeQwenProvider):
        self.provider = provider

    def ensure_provider(self, key):
        assert key == "yandex"
        return self.provider


class EmptyCatalogProvider:
    key = "empty"
    label = "Empty"

    def search(self, intent, *, limit=20):
        return []

    def stats(self):
        return {"item_count": 0, "catalog_version": "empty-1"}


class CountingCatalogProvider:
    key = "counting"
    label = "Counting catalog"

    def __init__(self):
        self.calls = 0
        self.offer = Offer(
            offer_id="cached-offer",
            provider=self.key,
            title="Конвектор",
            price=Decimal("10"),
            currency="RUB",
        )

    def search(self, intent, *, limit=20):
        self.calls += 1
        return [self.offer]

    def stats(self):
        return {"item_count": 1, "catalog_version": "counting-1"}


def ai_for(payload: dict | str | Exception, *, model: str = "gpt://folder/qwen/test"):
    response = payload if isinstance(payload, (str, Exception)) else json.dumps(payload, ensure_ascii=False)
    provider = FakeQwenProvider(response, model=model)
    return SourcingAIService(FakeAITransport(provider)), provider


def source_row(**updates):
    row = {
        "id": "reviewed-58-1",
        "source_text": "Конвектор NOBO NFK4N 07 Q=750 Вт",
        "name": "Электрический конвектор влагозащищенный Q=750 Вт",
        "type_mark": "NFK4N 07",
        "code": "SRC-07",
        "manufacturer": "NOBO",
        "quantity": "4",
        "unit": "шт.",
        "note": "",
    }
    row.update(updates)
    return row


def understand(row: dict, proposal: dict):
    ai, _ = ai_for(proposal)
    baseline = build_fallback_intent(row)
    return ai.understand_with_audit(row, baseline)


@pytest.mark.parametrize(
    ("field", "proposal_key", "proposal_value"),
    [
        ("source_row_id", "source_row_id", "changed-id"),
        ("source_text", "source_text", "rewritten source"),
        ("quantity", "quantity", "6"),
        ("unit", "unit", "компл."),
        ("manufacturer", "manufacturer", "Nobo normalized"),
        ("model", "model", "NFK4N 10"),
        ("article", "article", "AI-ARTICLE"),
    ],
)
def test_p1_p7_explicit_source_fields_are_locked(field, proposal_key, proposal_value):
    row = source_row()
    result = understand(row, {proposal_key: proposal_value, "normalized_name": "Конвектор"})

    assert getattr(result.resolved_intent, field) == getattr(result.baseline_intent, field)
    suggestion = next(item for item in result.suggestions if item.field == field)
    assert suggestion.resolution == SuggestionResolution.SOURCE_LOCKED


def test_p8_grounded_technical_ai_attribute_is_required_only_with_source_span():
    row = source_row(source_text="Электрический конвектор влагозащищенный")
    result = understand(row, {
        "attributes": {"features": "влагозащищенный"},
        "required_attributes": {"features": "влагозащищенный"},
        "evidence": {"features": "влагозащищенный"},
    })

    assert result.resolved_intent.required_attributes["features"] == "влагозащищенный"
    suggestion = next(item for item in result.suggestions if item.field == "attributes.features")
    assert suggestion.resolution == SuggestionResolution.ACCEPTED_GROUNDED
    assert suggestion.grounded is True


def test_p9_ungrounded_ai_attribute_is_never_required():
    row = source_row(source_text="Электрический конвектор")
    result = understand(row, {
        "attributes": {"protection_class": "IP24"},
        "required_attributes": {"protection_class": "IP24"},
    })

    assert "protection_class" not in result.resolved_intent.required_attributes
    assert result.resolved_intent.preferred_attributes["protection_class"] == "IP24"
    suggestion = next(item for item in result.suggestions if item.field == "attributes.protection_class")
    assert suggestion.resolution == SuggestionResolution.PREFERRED_AI_INFERENCE
    assert suggestion.grounded is False


@pytest.mark.parametrize("commercial_field", ["price", "currency", "availability", "supplier", "url", "offer_id", "stock", "delivery", "discount"])
def test_p10_commercial_fields_from_qwen_fail_strict_validation(commercial_field):
    row = source_row()
    result = understand(row, {commercial_field: "invented", "normalized_name": "Конвектор"})

    assert result.mode == "fallback"
    assert result.ai_proposal is None
    assert result.resolved_intent == result.baseline_intent


def test_p10_commercial_fields_are_rejected_even_when_nested_in_evidence():
    result = understand(source_row(), {
        "normalized_name": "Конвектор",
        "evidence": {"supplier": "invented"},
    })
    assert result.mode == "fallback"
    assert result.ai_proposal is None


def test_p11_invalid_json_returns_deterministic_fallback():
    row = source_row()
    ai, _ = ai_for("not-json")
    result = ai.understand_with_audit(row, build_fallback_intent(row))
    assert result.mode == "fallback"
    assert result.resolved_intent == result.baseline_intent
    assert result.warnings


def test_p12_provider_error_returns_sanitized_fallback():
    row = source_row()
    ai, _ = ai_for(RuntimeError("HTTP 503 Authorization secret-token"))
    result = ai.understand_with_audit(row, build_fallback_intent(row))
    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False).casefold()
    assert result.mode == "fallback"
    assert "503" not in encoded
    assert "secret-token" not in encoded


def test_p13_fallback_result_is_usable_when_ai_is_not_configured():
    row = source_row()
    result = SourcingAIService().understand_with_audit(row, build_fallback_intent(row))
    assert result.mode == "fallback"
    assert result.resolved_intent.normalized_name
    assert result.resolved_intent.quantity == "4"


def test_p14_common_measurements_are_normalized_to_existing_matcher_contract():
    first = extract_generic_attributes("Конвектор Q=750 Вт 230 В IP24")
    second = extract_generic_attributes("Насос 11 kW 380 V DN50 PN16")
    cable_ascii = extract_generic_attributes("Кабель ВВГнг-LS 3x2.5")
    cable_unicode = extract_generic_attributes("Кабель ВВГнг-LS 3×2,5 мм²")

    assert first == {"voltage": 230, "power": 0.75, "protection_class": "IP24"}
    assert second == {"diameter": 50, "pressure": 16, "voltage": 380, "power": 11}
    assert cable_ascii == {"cores": 3, "cable_section": 2.5}
    assert cable_unicode == cable_ascii


def test_p15_attribute_aliases_have_one_deterministic_canonical_key():
    assert {normalize_attribute_key(key) for key in ("power", "power_w", "wattage", "rated_power")} == {"power"}
    assert {normalize_attribute_key(key) for key in ("diameter", "diameter_mm", "dn")} == {"diameter"}
    assert {normalize_attribute_key(key) for key in ("cable_section", "cable_section_mm2")} == {"cable_section"}


def test_p16_audit_distinguishes_baseline_proposal_and_resolved():
    result = understand(source_row(), {
        "manufacturer": "Other",
        "product_class": "электрический конвектор",
        "attributes": {"protection_class": "IP24"},
    })
    assert result.baseline_intent.manufacturer == "NOBO"
    assert result.ai_proposal.manufacturer == "Other"
    assert result.resolved_intent.manufacturer == "NOBO"
    assert result.resolved_intent.product_class == "электрический конвектор"


def test_p17_locked_proposal_is_visible_in_audit():
    result = understand(source_row(), {"model": "NFK4N 10"})
    suggestion = next(item for item in result.suggestions if item.field == "model")
    assert suggestion.source_value == "NFK4N 07"
    assert suggestion.proposed_value == "NFK4N 10"
    assert suggestion.resolution == SuggestionResolution.SOURCE_LOCKED


def test_p18_product_intent_remains_frozen():
    intent = build_fallback_intent(source_row())
    with pytest.raises(ValidationError):
        intent.quantity = "99"


def make_service(tmp_path, ai):
    provider = EmptyCatalogProvider()
    return SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai,
        cache=SourcingCache(tmp_path / "sourcing-cache.json"),
    )


def test_p19_parser_prompt_and_revision_participate_in_cache_identity(tmp_path, monkeypatch):
    import averon_import.services.sourcing.product_understanding as module

    ai, _ = ai_for({"normalized_name": "Конвектор"})
    service = make_service(tmp_path, ai)
    key = service._intent_cache_key(source_row())
    changed_source_key = service._intent_cache_key(source_row(source_text="Другой исходный текст"))
    monkeypatch.setattr(
        module,
        "PRODUCT_UNDERSTANDING_SYSTEM_PROMPT",
        module.PRODUCT_UNDERSTANDING_SYSTEM_PROMPT + "\nrevision-test",
    )
    changed_prompt_key = service._intent_cache_key(source_row())
    assert PRODUCT_UNDERSTANDING_REVISION in json.dumps(ai.cache_identity())
    assert key.startswith("intent:")
    assert changed_source_key != key
    assert changed_prompt_key != key


def test_p20_changing_model_does_not_reuse_stale_intent_cache(tmp_path):
    first_ai, first_provider = ai_for({"product_class": "first"}, model="model-a")
    first = make_service(tmp_path, first_ai).understand_row_result(source_row())
    second_ai, second_provider = ai_for({"product_class": "second"}, model="model-b")
    second = make_service(tmp_path, second_ai).understand_row_result(source_row())

    assert first_provider.calls == second_provider.calls == 1
    assert first.provenance.model == "model-a"
    assert second.provenance.model == "model-b"


def test_p21_changing_parser_revision_does_not_reuse_stale_cache(tmp_path, monkeypatch):
    import averon_import.services.sourcing.product_understanding as module

    first_ai, first_provider = ai_for({"product_class": "first"})
    make_service(tmp_path, first_ai).understand_row_result(source_row())
    monkeypatch.setattr(module, "PRODUCT_UNDERSTANDING_REVISION", "test-next")
    second_ai, second_provider = ai_for({"product_class": "second"})
    result = make_service(tmp_path, second_ai).understand_row_result(source_row())

    assert first_provider.calls == second_provider.calls == 1
    assert result.provenance.parser_revision == "test-next"


def test_p22_api_key_never_appears_in_public_config_or_audit():
    ai, provider = ai_for({"normalized_name": "Конвектор"})
    provider.api_key = "do-not-expose-this-key"
    result = ai.understand_with_audit(source_row(), build_fallback_intent(source_row()))
    public = json.dumps(ai.public_config(), ensure_ascii=False)
    audit = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "do-not-expose-this-key" not in public
    assert "do-not-expose-this-key" not in audit
    assert "api_key" not in public


def test_p23_existing_search_consumes_only_resolved_product_intent(tmp_path):
    ai, _ = ai_for({"manufacturer": "Other", "normalized_name": "Конвектор"})
    result = make_service(tmp_path, ai).search_row(source_row())
    assert result.intent == result.understanding.resolved_intent
    assert result.intent.manufacturer == "NOBO"
    assert result.offers == []


def test_search_cache_reuses_facts_but_attaches_current_understanding_and_mode(tmp_path):
    cache_path = tmp_path / "search-cache.json"
    provider = CountingCatalogProvider()
    ai_a, provider_a = ai_for({"model": "NFK4N 10"}, model="model-a")
    service_a = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai_a,
        cache=SourcingCache(cache_path),
    )
    first = service_a.search_row(source_row())

    ai_b, provider_b = ai_for({"model": "NFK4N 05"}, model="model-b")
    service_b = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai_b,
        cache=SourcingCache(cache_path),
    )
    second = service_b.search_row(source_row())

    assert provider_a.calls == provider_b.calls == 1
    assert provider.calls == 1
    assert first.intent == second.intent
    assert first.understanding.provenance.model == "model-a"
    assert second.understanding.provenance.model == "model-b"
    assert first.understanding.suggestions != second.understanding.suggestions
    assert second.ai_mode == "qwen"
    assert [item.offer.offer_id for item in first.match_results] == ["cached-offer"]
    assert [item.offer.offer_id for item in second.match_results] == ["cached-offer"]

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_payload = next(value for key, value in payload.items() if key.startswith("search:"))
    assert cached_payload["understanding"] is None
    assert cached_payload["ai_mode"] == "fallback"
    assert cached_payload["warnings"] == []
    assert "baseline_intent" not in cached_payload


def test_search_cache_hit_ai_mode_follows_current_understanding(tmp_path):
    cache_path = tmp_path / "search-cache.json"
    provider = CountingCatalogProvider()
    ai, _ = ai_for({"model": "NFK4N 10"}, model="model-a")
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai,
        cache=SourcingCache(cache_path),
    )
    first = service.search_row(source_row())
    fallback_understanding = SourcingAIService().understand_with_audit(
        source_row(), build_fallback_intent(source_row())
    )
    second = service.search_intent(
        first.intent,
        understanding=fallback_understanding,
    )

    assert first.ai_mode == "qwen"
    assert second.ai_mode == "fallback"
    assert second.understanding == fallback_understanding
    assert provider.calls == 1


def test_search_intent_without_understanding_does_not_leak_previous_audit(tmp_path):
    cache_path = tmp_path / "search-cache.json"
    provider = CountingCatalogProvider()
    ai, _ = ai_for({"model": "NFK4N 10"}, model="model-a")
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai,
        cache=SourcingCache(cache_path),
    )
    first = service.search_row(source_row())

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    for key, value in payload.items():
        if key.startswith("search:"):
            value["understanding"] = first.understanding.model_dump(mode="json")
            value["ai_mode"] = "qwen"
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    direct = service.search_intent(first.intent)
    assert direct.understanding is None
    assert direct.ai_mode == "fallback"
    assert provider.calls == 1


def test_p24_p58_reviewed_quantity_and_unit_survive_product_understanding():
    row = source_row(quantity="4", unit="шт.")
    result = understand(row, {"quantity": "6", "unit": "компл.", "model": "NFK4N 10"})
    assert result.resolved_intent.quantity == "4"
    assert result.resolved_intent.unit == "шт."
    assert result.resolved_intent.model == "NFK4N 07"


def test_understand_api_shape_is_backwards_compatible(monkeypatch):
    from averon_import import main

    audit = SourcingAIService().understand_with_audit(source_row(), build_fallback_intent(source_row()))

    class StubService:
        def understand_row_result(self, row):
            return audit

    monkeypatch.setattr(main, "sourcing_service", StubService())
    payload = main.sourcing_understand(main.SourcingRowRequest(row=source_row()))
    assert payload["intent"] == audit.resolved_intent.model_dump(mode="json")
    assert payload["understanding"]["baseline_intent"]
    assert payload["understanding"]["resolved_intent"]
    assert payload["warnings"] == audit.warnings
