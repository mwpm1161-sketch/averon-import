from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ConfigDict, ValidationError

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import (
    MatchDecision,
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderInfo,
)
from averon_import.services.app_settings import LemanaB2BSettings
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.providers.base import get_provider_capabilities
from averon_import.services.sourcing.providers.demo_store_http import DemoStoreHttpProvider
from averon_import.services.sourcing.providers.local_catalog import (
    LemanaB2BProvider,
    LocalCatalogProvider,
)
from averon_import.services.secrets import MemorySecretStore
from averon_import.services.sourcing.service import SourcingService


CAPABILITY_FIELDS = (
    "supports_price",
    "supports_availability",
    "supports_product_url",
    "supports_article_search",
    "supports_model_search",
    "supports_batch_search",
    "supports_catalog_version",
    "supports_stock_quantity",
)

EXPECTED_CATALOG_CAPABILITIES = {
    field: field in {
        "supports_price",
        "supports_availability",
        "supports_product_url",
        "supports_article_search",
        "supports_model_search",
        "supports_catalog_version",
    }
    for field in CAPABILITY_FIELDS
}


def test_capabilities_default_to_all_false_and_are_frozen():
    capabilities = SourcingProviderCapabilities()

    assert capabilities.model_dump() == {field: False for field in CAPABILITY_FIELDS}
    with pytest.raises(ValidationError):
        capabilities.supports_price = True
    with pytest.raises(ValidationError):
        SourcingProviderCapabilities(unsupported=True)


def test_old_provider_info_payload_gets_empty_capability_default():
    info = SourcingProviderInfo.model_validate({
        "key": "legacy",
        "label": "Старый адаптер",
        "catalog_item_count": 3,
        "configured": True,
    })

    assert info.capabilities == SourcingProviderCapabilities()


def test_builtin_provider_capability_matrices(tmp_path):
    local = LocalCatalogProvider(tmp_path / "catalog.sqlite3")
    demo = DemoStoreHttpProvider(transport=lambda *_args: None)
    leman = LemanaB2BProvider(LemanaB2BSettings(), MemorySecretStore(), tmp_path)

    assert local.capabilities.model_dump() == EXPECTED_CATALOG_CAPABILITIES
    assert demo.capabilities.model_dump() == EXPECTED_CATALOG_CAPABILITIES
    assert leman.key == "lemana_b2b"
    assert leman.capabilities.model_dump() == EXPECTED_CATALOG_CAPABILITIES
    assert local.capabilities.supports_batch_search is False
    assert local.capabilities.supports_stock_quantity is False
    assert demo.capabilities is not local.capabilities


class LegacyStubProvider:
    key = "legacy"
    label = "Legacy provider"

    def stats(self):
        return {"item_count": 0, "catalog_version": "legacy-1"}

    def search(self, intent, *, limit=20):
        return []


class CapableStubProvider(LegacyStubProvider):
    key = "capable"
    label = "Capable provider"
    capabilities = SourcingProviderCapabilities(supports_price=True)


class MalformedCapabilitiesProvider(LegacyStubProvider):
    key = "malformed"
    capabilities = {"supports_price": True, "api_key": "must-not-escape"}


class ExtendedCapabilities(SourcingProviderCapabilities):
    model_config = ConfigDict(extra="allow", frozen=True)

    api_key: str
    internal_note: str = "private"


class ExtendedCapabilitiesProvider(LegacyStubProvider):
    key = "extended"
    capabilities = ExtendedCapabilities(
        supports_price=True,
        api_key="must-not-escape",
        internal_note="private",
    )


def test_public_config_adds_safe_capabilities_for_legacy_and_typed_providers():
    service = SourcingService(
        {
            "legacy": LegacyStubProvider(),
            "capable": CapableStubProvider(),
        },
        default_provider="capable",
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )

    payload = service.public_config()
    rows = {row["key"]: row for row in payload["providers"]}

    assert rows["legacy"]["capabilities"] == {
        field: False for field in CAPABILITY_FIELDS
    }
    assert rows["capable"]["capabilities"] == {
        field: field == "supports_price" for field in CAPABILITY_FIELDS
    }
    assert payload["provider"]["capabilities"] == rows["capable"]["capabilities"]
    assert set(payload) >= {"provider", "providers", "catalog_item_count", "ai_available", "ai"}


def test_malformed_capabilities_fall_back_without_serializing_provider_data():
    service = SourcingService(
        {"malformed": MalformedCapabilitiesProvider()},
        default_provider="malformed",
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )

    payload = service.public_config()
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["provider"]["capabilities"] == {
        field: False for field in CAPABILITY_FIELDS
    }
    assert "must-not-escape" not in encoded
    assert "api_key" not in encoded.casefold()


def test_typed_capability_subclass_is_whitelist_normalized_for_api():
    provider = ExtendedCapabilitiesProvider()
    normalized = get_provider_capabilities(provider)
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )

    payload = service.public_config()
    row_capabilities = payload["providers"][0]["capabilities"]
    active_capabilities = payload["provider"]["capabilities"]

    assert type(normalized) is SourcingProviderCapabilities
    assert normalized.supports_price is True
    assert set(normalized.model_dump()) == set(CAPABILITY_FIELDS)
    assert normalized.model_dump()["supports_price"] is True
    assert "api_key" not in normalized.model_dump()
    assert "internal_note" not in normalized.model_dump()
    assert row_capabilities == active_capabilities == normalized.model_dump()
    encoded = json.dumps(payload, ensure_ascii=False).casefold()
    assert "must-not-escape" not in encoded
    assert "api_key" not in encoded
    assert "internal_note" not in encoded


def _intent() -> ProductIntent:
    return ProductIntent(
        source_row_id="row-1",
        source_text="Клапан DN50",
        normalized_name="Клапан",
        model="DN50",
        attributes={"diameter": 50},
        required_attributes={"diameter": 50},
        quantity="1",
        unit="шт.",
    )


def _offer() -> Offer:
    return Offer(
        offer_id="offer-1",
        provider="capability-test",
        title="Клапан DN50",
        price=Decimal("10"),
        currency="RUB",
        availability=None,
        url="",
        attributes={"diameter": 50},
    )


class BehaviorProvider:
    key = "behavior"
    label = "Behavior provider"

    def __init__(self, capabilities):
        self.capabilities = capabilities

    def stats(self):
        return {"item_count": 1, "catalog_version": "behavior-1"}

    def search(self, intent, *, limit=20):
        return [_offer()]


def test_capabilities_are_metadata_only_and_nullable_offer_values_remain_nullable():
    no_capabilities = SourcingService(
        {"behavior": BehaviorProvider(SourcingProviderCapabilities())},
        default_provider="behavior",
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )
    all_capabilities = SourcingService(
        {"behavior": BehaviorProvider(SourcingProviderCapabilities(
            supports_price=True,
            supports_availability=True,
            supports_product_url=True,
            supports_article_search=True,
            supports_model_search=True,
            supports_batch_search=True,
            supports_catalog_version=True,
            supports_stock_quantity=True,
        ))},
        default_provider="behavior",
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )

    without_metadata = no_capabilities.search_intent(_intent(), ai_rerank=False)
    with_metadata = all_capabilities.search_intent(_intent(), ai_rerank=False)

    assert without_metadata.recommended_offer.offer_id == with_metadata.recommended_offer.offer_id
    assert [item.decision for item in without_metadata.match_results] == [MatchDecision.MATCH]
    assert [item.decision for item in with_metadata.match_results] == [MatchDecision.MATCH]
    assert without_metadata.offers[0].price == with_metadata.offers[0].price == Decimal("10")
    assert without_metadata.offers[0].availability is None
    assert with_metadata.offers[0].availability is None


def test_legacy_provider_without_capabilities_remains_usable():
    result = SourcingService(
        {"legacy": LegacyStubProvider()},
        default_provider="legacy",
        ai=SourcingAIService(),
        cache=SourcingCache(),
    ).search_intent(_intent(), ai_rerank=False)

    assert result.offers == []
    assert get_provider_capabilities(LegacyStubProvider()) == SourcingProviderCapabilities()
