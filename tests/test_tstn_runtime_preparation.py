from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from averon_import.services.app_settings import AppSettingsService
from averon_import.services.secrets import (
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
    TSTN_X_SP_SECRET,
    MemorySecretStore,
)
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.contracts import (
    ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import (
    ProviderExecutionScope,
    ProviderRunner,
)
from averon_import.services.sourcing.providers.tstn import (
    TstnApiClient,
    TstnCatalogMirror,
    TstnHttpResponse,
    TstnItemDetail,
    TstnItemListResponse,
)
from averon_import.services.sourcing.runtime import create_sourcing_runtime


GENERATION = "a" * 32
ROTATED_GENERATION = "b" * 32
SYNTHETIC_SERVICE = "tn4b-test-service-value"
SYNTHETIC_KEY = "tn4b-test-client-key-value"
SYNTHETIC_XSP = "tn4b-test-xsp-value"
SYNTHETIC_TOKEN = "tn4b-test-access-token-value"


class TrackingMemorySecretStore(MemorySecretStore):
    def __init__(self):
        super().__init__()
        self.read_keys: list[str] = []

    def get(self, key: str) -> str | None:
        self.read_keys.append(key)
        return super().get(key)


class FakeCatalogClient:
    def __init__(self, items):
        self.items = tuple(items)
        self.calls: list[tuple[int | None, int | None]] = []

    def list_items(self, *, access_token, limit, offset):
        assert access_token == "tn4b-offline-catalog-token"
        self.calls.append((limit, offset))
        rows = self.items[offset : offset + limit]
        return TstnItemListResponse.model_validate({
            "count": len(rows),
            "count_all": len(self.items),
            "page_num": offset // limit + 1,
            "results": rows,
        })


def _item(*, item_id=101, article="000101", name="Крепёжный элемент"):
    return TstnItemDetail.model_validate({
        "id": item_id,
        "article": article,
        "name": name,
        "brand": "ТЕХНОНИКОЛЬ",
        "categories": [{"id": 10, "name": "Крепёж", "parent": 1}],
    })


def _sync_catalog(path, items):
    mirror = TstnCatalogMirror(path)
    result = mirror.sync(
        FakeCatalogClient(items),
        access_token="tn4b-offline-catalog-token",
        page_size=10,
        max_pages=2,
    )
    assert result.item_count == len(items)
    return mirror


def _settings(tmp_path, *, enabled=False):
    settings = AppSettingsService(tmp_path / "settings")
    if enabled:
        settings.update({"sourcing": {"tstn": {"enabled": True, "credential_generation": GENERATION}}})
    return settings


def _configured_store(*, generation=GENERATION, include_pair=True):
    store = TrackingMemorySecretStore()
    if include_pair:
        store.set(TSTN_SERVICE_IDENTIFIER, SYNTHETIC_SERVICE)
        store.set(TSTN_CLIENT_KEY, SYNTHETIC_KEY)
        store.set(TSTN_X_SP_SECRET, SYNTHETIC_XSP)
    if generation:
        store.set(TSTN_CREDENTIALS_CONFIGURED, generation)
    store.read_keys.clear()
    return store


def _tstn_read_keys(store):
    return [key for key in store.read_keys if key.startswith("tstn.")]


def _runtime(tmp_path, settings, store, *, monkeypatch):
    from averon_import.ai.service import AiCorrectionService

    monkeypatch.setattr(
        "averon_import.services.sourcing.runtime.create_sourcing_ai_transport",
        lambda *_args: AiCorrectionService(),
    )
    return create_sourcing_runtime(tmp_path / "data", settings, store)


def test_disabled_tstn_is_private_and_does_not_create_catalog_or_read_credentials(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = _configured_store()
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    preparation = runtime._tstn_runtime

    assert preparation is not None
    assert preparation.adapter is None
    assert preparation.readiness.credential_state == "disabled"
    assert preparation.readiness.catalog_state == "not_checked"
    assert preparation.readiness.live_verified is False
    assert not (tmp_path / "data" / "sourcing" / "providers" / "tstn").exists()
    assert "tstn" not in runtime.providers
    assert "tstn" not in runtime.execution_providers
    assert "tstn" not in runtime.provider_runner._registry
    assert "tstn" not in {item["key"] for item in runtime.service.public_config()["providers"]}
    assert _tstn_read_keys(store) == []


def test_missing_pair_stale_marker_and_missing_catalog_are_safe(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store(generation=None, include_pair=False)
    store.set(TSTN_SERVICE_IDENTIFIER, SYNTHETIC_SERVICE)
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    readiness = runtime._tstn_runtime.readiness

    assert readiness.credential_state == "authorization_pair_missing"
    assert readiness.configured is False
    assert readiness.catalog_state == "catalog_missing"
    assert readiness.catalog_ready is False
    assert set(_tstn_read_keys(store)) <= {TSTN_CREDENTIALS_CONFIGURED}
    assert not (tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3").exists()

    stale_store = _configured_store(generation=ROTATED_GENERATION)
    stale_runtime = _runtime(tmp_path / "stale", settings, stale_store, monkeypatch=monkeypatch)
    assert stale_runtime._tstn_runtime.readiness.credential_state == "credentials_marker_stale"
    assert stale_runtime._tstn_runtime.readiness.configured is False
    assert set(_tstn_read_keys(stale_store)) <= {TSTN_CREDENTIALS_CONFIGURED}


def test_existing_unsynchronized_catalog_is_reported_without_integrity_scan(tmp_path, monkeypatch):
    from averon_import.services.sourcing.providers.tstn import client as tstn_client_module

    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    TstnCatalogMirror(catalog_path)
    scan_calls = []

    def forbidden_scan(_self):
        scan_calls.append(True)
        raise AssertionError("passive readiness must not scan SQLite")

    def forbidden_transport(*_args, **_kwargs):
        scan_calls.append("http")
        raise AssertionError("passive readiness must not contact TSTN")

    monkeypatch.setattr(TstnCatalogMirror, "check_integrity", forbidden_scan)
    monkeypatch.setattr(tstn_client_module, "_default_transport", forbidden_transport)
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    readiness = runtime._tstn_runtime.readiness
    runtime.service.public_config()
    rebuilt = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    rebuilt._tstn_runtime.readiness

    assert readiness.credential_state == "configured"
    assert readiness.catalog_state == "catalog_unsynchronized"
    assert readiness.configured is True
    assert readiness.catalog_ready is False
    assert scan_calls == []
    assert set(_tstn_read_keys(store)) <= {TSTN_CREDENTIALS_CONFIGURED}
    diagnostic = repr(runtime._tstn_runtime) + repr(readiness) + json.dumps(
        runtime.service.public_config(), ensure_ascii=False,
    )
    for sensitive in (SYNTHETIC_SERVICE, SYNTHETIC_KEY, SYNTHETIC_XSP, SYNTHETIC_TOKEN, str(catalog_path)):
        assert sensitive not in diagnostic


def test_reopened_catalog_stays_unconfirmed_until_explicit_search_validates_it(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    original = _sync_catalog(catalog_path, [_item()])
    revision = original.revision
    scan_calls = []
    original_check = TstnCatalogMirror.check_integrity

    def counted_scan(self):
        scan_calls.append(self.path)
        return original_check(self)

    monkeypatch.setattr(TstnCatalogMirror, "check_integrity", counted_scan)
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    preparation = runtime._tstn_runtime

    assert preparation.readiness.catalog_state == "integrity_unconfirmed"
    assert preparation.readiness.catalog_ready is False
    assert scan_calls == []
    assert preparation._mirror.revision == revision
    records = preparation._mirror.search(article="000101", limit=3)
    assert len(records) == 1
    assert scan_calls == [catalog_path]
    assert preparation.readiness.catalog_state == "ready"
    assert preparation.readiness.catalog_ready is True
    assert preparation.readiness.live_verified is False
    assert set(_tstn_read_keys(store)) <= {TSTN_CREDENTIALS_CONFIGURED}


def test_detected_corrupt_catalog_fails_readiness_after_lazy_validation(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    _sync_catalog(catalog_path, [_item()])
    with sqlite3.connect(catalog_path) as connection:
        connection.execute("UPDATE tstn_catalog_products SET canonical_payload='{}'")
    original_check = TstnCatalogMirror.check_integrity
    scans = []

    def counted_scan(self):
        scans.append(True)
        return original_check(self)

    monkeypatch.setattr(TstnCatalogMirror, "check_integrity", counted_scan)
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    preparation = runtime._tstn_runtime

    assert preparation.readiness.catalog_state == "integrity_unconfirmed"
    assert scans == []
    with pytest.raises(Exception):
        preparation._mirror.search(article="000101", limit=3)
    assert scans == [True]
    assert preparation.readiness.catalog_state == "integrity_error"
    assert preparation.readiness.catalog_ready is False


def test_unreadable_existing_file_is_not_repaired_or_exposed_as_catalog(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    original_bytes = b"not a SQLite database"
    catalog_path.write_bytes(original_bytes)

    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)

    assert runtime._tstn_runtime.readiness.catalog_state == "storage_error"
    assert catalog_path.read_bytes() == original_bytes


def test_runtime_rebuild_reuses_catalog_without_changing_revision(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    first_mirror = _sync_catalog(catalog_path, [_item()])
    first_revision = first_mirror.revision
    first_stats = first_mirror.stats()

    first = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    second = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)

    assert first._tstn_runtime._mirror.revision == first_revision
    assert second._tstn_runtime._mirror.revision == first_revision
    assert second._tstn_runtime._mirror.stats()["item_count"] == first_stats["item_count"]
    assert second._tstn_runtime._mirror.stats()["last_synced_at"] == first_stats["last_synced_at"]


def test_credential_rotation_invalidates_old_prepared_adapter_without_secret_reads(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    _sync_catalog(catalog_path, [_item()])
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    old_preparation = runtime._tstn_runtime
    old_adapter = old_preparation.adapter
    intent = ProductIntent(
        source_row_id="row-1",
        source_text="Крепёжный элемент 000101",
        normalized_name="Крепёжный элемент",
        article="000101",
        required_attributes={},
        search_queries=["Крепёжный элемент 000101"],
    )
    identity = old_adapter.request_identity(intent, limit=1, execution_scope_id="tn4b-rotation")

    settings.update({"sourcing": {"tstn": {"credential_generation": ROTATED_GENERATION}}})
    store.set(TSTN_CREDENTIALS_CONFIGURED, ROTATED_GENERATION)
    store.read_keys.clear()
    rebuilt = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)

    assert rebuilt._tstn_runtime.adapter.credential_generation == ROTATED_GENERATION
    assert old_preparation.readiness.credential_state == "runtime_generation_stale"
    assert old_adapter.is_cached_identity_current(identity) is False
    from averon_import.services.sourcing.providers.execution import ProviderRequestCounter
    from averon_import.services.sourcing.providers.tstn import TstnApiClient

    transport_calls = []

    def forbidden_transport(*args, **kwargs):
        transport_calls.append((args, kwargs))
        raise AssertionError("stale adapter must fail before supplier HTTP")

    old_adapter.client_factory = lambda timeout, *, max_attempts, outbound_attempt_observer: TstnApiClient(
        timeout_seconds=timeout,
        max_attempts=max_attempts,
        outbound_attempt_observer=outbound_attempt_observer,
        transport=forbidden_transport,
    )
    outcome = old_adapter.execute_search(
        intent,
        limit=1,
        request_counter=ProviderRequestCounter(),
    )
    assert outcome.state.value == "failure"
    assert outcome.failure_category.value == "misconfigured"
    assert outcome.request_count == 0
    assert transport_calls == []
    assert set(_tstn_read_keys(store)) <= {TSTN_CREDENTIALS_CONFIGURED}


def test_prepared_adapter_supports_explicit_isolated_runner_with_fake_client(tmp_path, monkeypatch):
    settings = _settings(tmp_path, enabled=True)
    store = _configured_store()
    catalog_path = tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    _sync_catalog(catalog_path, [_item()])
    runtime = _runtime(tmp_path, settings, store, monkeypatch=monkeypatch)
    adapter = runtime._tstn_runtime.adapter
    calls = []
    responses = [
        {"access_token": SYNTHETIC_TOKEN},
        {"count": 1, "results": [{"article": "000101", "price": "12.50"}]},
        {"count": 1, "results": [{"article": "000101", "status": "В наличии"}]},
    ]

    def fake_transport(request, _timeout):
        calls.append(request.full_url.split("?", 1)[0])
        return TstnHttpResponse(
            status_code=200,
            body=json.dumps(responses.pop(0), ensure_ascii=False).encode("utf-8"),
        )

    adapter.client_factory = lambda timeout, *, max_attempts, outbound_attempt_observer: TstnApiClient(
        timeout_seconds=timeout,
        max_attempts=max_attempts,
        outbound_attempt_observer=outbound_attempt_observer,
        transport=fake_transport,
    )
    runner = ProviderRunner({"tstn": adapter})
    result = runner.run(
        selection=ProviderSelection(provider_keys=("tstn",)),
        intent=ProductIntent(
            source_row_id="row-1",
            source_text="Крепёжный элемент 000101",
            normalized_name="Крепёжный элемент",
            article="000101",
            required_attributes={},
            search_queries=["Крепёжный элемент 000101"],
        ),
        limit=1,
        scope=ProviderExecutionScope(execution_scope_id="tn4b-explicit-test-only"),
    )

    assert result.outcomes[0].state.value == "success", (
        result.outcomes[0].state,
        result.outcomes[0].failure_category,
        result.outcomes[0].request_count,
        calls,
    )
    assert result.outcomes[0].request_count == 3
    assert result.offers[0].price == Decimal("12.50")
    assert calls == [
        "https://tstn.ru/api/v1/user/",
        "https://tstn.ru/api/v1/catalog/prices/",
        "https://tstn.ru/api/v1/catalog/available/",
    ]
    assert "tstn" not in runtime.provider_runner._registry
    assert set(_tstn_read_keys(store)) == {
        TSTN_CREDENTIALS_CONFIGURED,
        TSTN_SERVICE_IDENTIFIER,
        TSTN_CLIENT_KEY,
    }
    assert TSTN_X_SP_SECRET not in store.read_keys
