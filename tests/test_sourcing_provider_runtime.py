from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ConfigDict, ValidationError

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.providers.base import normalize_provider_runtime_state
from averon_import.services.sourcing.service import SourcingService


RUNTIME_FIELDS = {
    "configured",
    "reachable",
    "item_count",
    "catalog_version",
    "latency_ms",
    "error",
}


def test_runtime_state_defaults_is_frozen_and_strict():
    state = SourcingProviderRuntimeState()

    assert state.model_dump() == {
        "configured": True,
        "reachable": True,
        "item_count": 0,
        "catalog_version": "unknown",
        "latency_ms": None,
        "error": "",
    }
    with pytest.raises(ValidationError):
        state.reachable = False
    with pytest.raises(ValidationError):
        SourcingProviderRuntimeState(item_count=-1)
    with pytest.raises(ValidationError):
        SourcingProviderRuntimeState(latency_ms=-1)
    with pytest.raises(ValidationError):
        SourcingProviderRuntimeState(debug_payload="private")


def test_runtime_state_error_is_bounded_and_safe():
    assert SourcingProviderRuntimeState(error="HTTP 503 authorization token=secret").error == (
        "Проверка каталога поставщика не выполнена"
    )
    assert SourcingProviderRuntimeState(error="  Каталог временно недоступен  ").error == (
        "Каталог временно недоступен"
    )
    with pytest.raises(ValidationError):
        SourcingProviderRuntimeState(error={"raw_response": "private"})


def test_legacy_stats_dict_is_whitelist_normalized():
    normalized = normalize_provider_runtime_state({
        "configured": False,
        "reachable": True,
        "item_count": 12,
        "catalog_version": "catalog-7",
        "latency_ms": 4.5,
        "error": "",
        "api_key": "must-not-escape",
        "raw_response": {"secret": "private"},
    })

    assert type(normalized) is SourcingProviderRuntimeState
    assert normalized.model_dump() == {
        "configured": False,
        "reachable": True,
        "item_count": 12,
        "catalog_version": "catalog-7",
        "latency_ms": 4.5,
        "error": "",
    }


def test_typed_runtime_state_is_rebuilt_as_exact_base_model():
    original = SourcingProviderRuntimeState(
        configured=False,
        reachable=True,
        item_count=7,
        catalog_version="typed-1",
        latency_ms=2.0,
        error="Каталог доступен",
    )
    normalized = normalize_provider_runtime_state(original)

    assert type(normalized) is SourcingProviderRuntimeState
    assert normalized is not original
    assert normalized == original


class ExtendedRuntimeState(SourcingProviderRuntimeState):
    model_config = ConfigDict(extra="allow", frozen=True)

    api_key: str
    raw_response: str = "private"


def test_runtime_subclass_secrets_are_removed_by_whitelist_reconstruction():
    extended = ExtendedRuntimeState(
        reachable=True,
        item_count=5,
        api_key="must-not-escape",
        raw_response="private-body",
    )

    normalized = normalize_provider_runtime_state(extended)
    encoded = json.dumps(normalized.model_dump(), ensure_ascii=False)

    assert type(normalized) is SourcingProviderRuntimeState
    assert set(normalized.model_dump()) == RUNTIME_FIELDS
    assert normalized.item_count == 5
    assert "must-not-escape" not in encoded
    assert "api_key" not in encoded
    assert "raw_response" not in encoded


def test_malformed_or_unknown_runtime_stats_use_safe_default():
    malformed = normalize_provider_runtime_state({
        "item_count": -1,
        "latency_ms": "not-a-number",
        "raw_response": "private",
    })
    unknown = normalize_provider_runtime_state(object())

    assert malformed == SourcingProviderRuntimeState()
    assert unknown == SourcingProviderRuntimeState()


class StatsProvider:
    key = "stats"
    label = "Stats provider"
    capabilities = SourcingProviderCapabilities(supports_price=True)

    def stats(self):
        return {
            "configured": True,
            "reachable": True,
            "item_count": 3,
            "catalog_version": "stats-1",
            "latency_ms": 1.25,
            "debug_payload": {"token": "private"},
        }

    def search(self, intent, *, limit=20):
        return []


class RaisingStatsProvider:
    key = "raising"
    label = "Raising provider"

    def stats(self):
        raise RuntimeError("HTTP 503 authorization token=internal-secret")

    def search(self, intent, *, limit=20):
        return []


class NoStatsProvider:
    key = "no-stats"
    label = "No stats provider"

    def search(self, intent, *, limit=20):
        return []


def _service(providers, *, default_provider):
    return SourcingService(
        providers,
        default_provider=default_provider,
        ai=SourcingAIService(),
        cache=SourcingCache(),
    )


def test_public_config_uses_normalized_runtime_state_without_internal_fields():
    service = _service(
        {
            "stats": StatsProvider(),
            "raising": RaisingStatsProvider(),
            "no-stats": NoStatsProvider(),
        },
        default_provider="stats",
    )

    payload = service.public_config()
    rows = {row["key"]: row for row in payload["providers"]}
    encoded = json.dumps(payload, ensure_ascii=False).casefold()

    assert rows["stats"]["catalog_item_count"] == 3
    assert rows["stats"]["reachable"] is True
    assert rows["raising"]["configured"] is True
    assert rows["raising"]["reachable"] is False
    assert rows["raising"]["error"] == "Проверка каталога поставщика не выполнена"
    assert rows["no-stats"]["reachable"] is True
    assert "internal-secret" not in encoded
    assert "authorization" not in encoded
    assert "debug_payload" not in encoded
    assert "token" not in encoded
    assert "supports_price" not in rows["stats"].get("runtime", {})


def test_runtime_state_and_capabilities_remain_separate():
    state = normalize_provider_runtime_state(StatsProvider().stats())

    assert set(state.model_dump()) == RUNTIME_FIELDS
    assert "supports_price" not in state.model_dump()
    assert StatsProvider.capabilities.supports_price is True


def _intent(row_id: str) -> ProductIntent:
    return ProductIntent(
        source_row_id=row_id,
        source_text="Клапан",
        normalized_name="Клапан",
        quantity="1",
        unit="шт.",
    )


def _offer(row_id: str) -> Offer:
    return Offer(
        offer_id=f"offer-{row_id}",
        provider="row-provider",
        title="Клапан",
        price=Decimal("10"),
        currency="RUB",
    )


class ProjectProvider:
    key = "project"
    label = "Project provider"

    def __init__(self, failed_rows=()):
        self.failed_rows = set(failed_rows)
        self.search_calls = []

    def stats(self):
        return {"reachable": True, "catalog_version": "project-1"}

    def search(self, intent, *, limit=20):
        self.search_calls.append(intent.source_row_id)
        if intent.source_row_id in self.failed_rows:
            raise RuntimeError("temporary provider failure")
        return [_offer(intent.source_row_id)]


def test_healthy_project_provider_error_does_not_stop_remaining_rows():
    provider = ProjectProvider(failed_rows={"row-1"})
    result = _service({"project": provider}, default_provider="project").search_project([
        {"id": "row-1", "row_type": "item", "name": "Клапан", "quantity": "1"},
        {"id": "row-2", "row_type": "item", "name": "Клапан", "quantity": "1"},
    ])

    assert result.positions_processed == 2
    assert result.results[0].offers == []
    assert result.results[1].offers
    assert provider.search_calls == ["row-1", "row-2"]


def test_unreachable_project_provider_fails_before_processing_rows():
    class UnreachableProvider(ProjectProvider):
        def stats(self):
            return {
                "reachable": False,
                "catalog_version": "unavailable",
                "error": "store offline",
            }

    provider = UnreachableProvider()
    with pytest.raises(ValueError, match="store offline"):
        _service({"project": provider}, default_provider="project").search_project([
            {"id": "row-1", "row_type": "item", "name": "Клапан", "quantity": "1"},
        ])
    assert provider.search_calls == []
