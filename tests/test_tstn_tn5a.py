from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from threading import Event, Thread

from pydantic import SecretStr

from averon_import.ai.service import AiCorrectionService
from averon_import.services.app_settings import AppSettingsService
from averon_import.services.secrets import (
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
    MemorySecretStore,
)
from averon_import.services.sourcing.models import ProductIntent
from averon_import.services.sourcing.providers.contracts import ProviderSelection
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
from averon_import.services.sourcing.providers.tstn.authorization import TSTN_CREDENTIAL_LOCK
from averon_import.services.sourcing.runtime import create_sourcing_runtime


GENERATION = "a" * 32
ROTATED_GENERATION = "b" * 32
SYNTHETIC_TOKEN = "tn5a-offline-access-token"
SYNTHETIC_SERVICE = "tn5a-offline-service"
SYNTHETIC_KEY = "tn5a-offline-key"


def _item(*, item_id=101, article="000101", name="Крепёжный элемент"):
    return TstnItemDetail.model_validate({
        "id": item_id,
        "article": article,
        "name": name,
        "brand": "ТЕХНОНИКОЛЬ",
        "categories": [{"id": 10, "name": "Крепёж", "parent": 1}],
    })


class FakeCatalogClient:
    def __init__(self, items, *, on_page=None):
        self.items = tuple(items)
        self.calls: list[tuple[int, int]] = []
        self.on_page = on_page

    def list_items(self, *, access_token, limit, offset):
        assert isinstance(access_token, SecretStr)
        assert access_token.get_secret_value() == SYNTHETIC_TOKEN
        self.calls.append((limit, offset))
        if self.on_page is not None:
            self.on_page()
        rows = self.items[offset : offset + limit]
        return TstnItemListResponse.model_validate({
            "count": len(rows),
            "count_all": len(self.items),
            "page_num": offset // limit + 1,
            "results": rows,
        })


def _runtime(tmp_path, monkeypatch, *, generation=GENERATION, enabled=True):
    settings = AppSettingsService(tmp_path / "settings")
    settings.update({"sourcing": {"tstn": {
        "enabled": enabled,
        "credential_generation": generation,
    }}})
    store = MemorySecretStore()
    if enabled:
        store.set(TSTN_CREDENTIALS_CONFIGURED, generation)
        store.set(TSTN_SERVICE_IDENTIFIER, SYNTHETIC_SERVICE)
        store.set(TSTN_CLIENT_KEY, SYNTHETIC_KEY)
    monkeypatch.setattr(
        "averon_import.services.sourcing.runtime.create_sourcing_ai_transport",
        lambda *_args: AiCorrectionService(),
    )
    runtime = create_sourcing_runtime(tmp_path / "data", settings, store)
    return runtime, settings, store


def _sync(runtime, items, *, on_page=None, generation=GENERATION):
    preparation = runtime._tstn_runtime
    coordinator = preparation.create_catalog_sync_coordinator()
    assert coordinator is not None
    return coordinator.sync(
        client=FakeCatalogClient(items, on_page=on_page),
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=generation,
    )


def _intent():
    return ProductIntent(
        source_row_id="row-1",
        source_text="Крепёжный элемент 000101",
        normalized_name="Крепёжный элемент",
        article="000101",
        required_attributes={},
        search_queries=["Крепёжный элемент 000101"],
    )


def test_explicit_sync_creates_catalog_and_private_runner_stays_isolated(tmp_path, monkeypatch):
    runtime, _settings, store = _runtime(tmp_path, monkeypatch)
    preparation = runtime._tstn_runtime
    catalog_path = (
        tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    )
    assert preparation.create_private_provider_runner() is None
    assert not catalog_path.exists()

    coordinator = preparation.create_catalog_sync_coordinator()
    assert coordinator is not None
    assert not catalog_path.exists()
    synced = coordinator.sync(
        client=FakeCatalogClient([_item()]),
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=GENERATION,
    )
    assert synced.state == "success"
    assert synced.item_count == 1
    assert synced.page_count == 1
    assert synced.catalog_revision
    assert catalog_path.is_file()
    assert "token" not in repr(synced).casefold()
    assert SYNTHETIC_TOKEN not in repr(synced)
    persisted_bytes = catalog_path.read_bytes()
    for secret in (SYNTHETIC_TOKEN, SYNTHETIC_SERVICE, SYNTHETIC_KEY):
        assert secret.encode() not in persisted_bytes

    # Runtime preparation after publication is passive and opens the mirror
    # read-only. Explicit private-runner preparation validates it once.
    rebuilt = create_sourcing_runtime(tmp_path / "data", _settings, store)
    new_preparation = rebuilt._tstn_runtime
    assert new_preparation.readiness.catalog_state == "integrity_unconfirmed"
    assert "tstn" not in rebuilt.providers
    assert "tstn" not in rebuilt.execution_providers
    assert "tstn" not in rebuilt.provider_runner._registry

    network_calls = []
    responses = [
        {"access_token": "tn5a-synthetic-commercial-token"},
        {"count": 1, "results": [{"article": "000101", "price": "12.50"}]},
        {"count": 1, "results": [{"article": "000101", "status": "В наличии"}]},
        {"access_token": "tn5a-synthetic-commercial-token"},
        {"count": 1, "results": [{"article": "000101", "price": "12.50"}]},
        {"count": 1, "results": [{"article": "000101", "status": "В наличии"}]},
    ]

    def fake_transport(request, _timeout):
        network_calls.append(request.full_url.split("?", 1)[0])
        return TstnHttpResponse(
            status_code=200,
            body=json.dumps(responses.pop(0), ensure_ascii=False).encode("utf-8"),
        )

    private_runner = new_preparation.create_private_provider_runner()
    assert isinstance(private_runner, ProviderRunner)
    assert tuple(private_runner._registry) == ("tstn",)
    assert "tstn" not in rebuilt.provider_runner._registry
    assert network_calls == []
    new_preparation.adapter.client_factory = lambda timeout, *, max_attempts, outbound_attempt_observer: TstnApiClient(
        timeout_seconds=timeout,
        max_attempts=max_attempts,
        outbound_attempt_observer=outbound_attempt_observer,
        transport=fake_transport,
    )
    scope = ProviderExecutionScope(execution_scope_id="tn5a-private-test")
    selection = ProviderSelection(provider_keys=("tstn",))
    result = private_runner.run(
        selection=selection,
        intent=_intent(),
        limit=1,
        scope=scope,
    )
    assert result.outcomes[0].state.value == "success"
    assert result.outcomes[0].request_count == 3
    assert result.offers[0].price == Decimal("12.50")
    assert new_preparation.readiness.live_verified is False

    updated = coordinator.sync(
        client=FakeCatalogClient([_item(name="Обновлённый каталог")]),
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=GENERATION,
    )
    assert updated.state == "success"
    assert updated.catalog_revision != synced.catalog_revision
    repeated = private_runner.run(
        selection=selection,
        intent=_intent(),
        limit=1,
        scope=scope,
    )
    assert repeated.outcomes[0].catalog_version == updated.catalog_revision
    assert repeated.outcomes[0].request_count == 3
    assert repeated.reused_provider_keys == ()

    with TSTN_CREDENTIAL_LOCK:
        _settings.update({"sourcing": {"tstn": {
            "credential_generation": ROTATED_GENERATION,
        }}})
        store.set(TSTN_CREDENTIALS_CONFIGURED, ROTATED_GENERATION)
    stale = private_runner.run(
        selection=selection,
        intent=_intent(),
        limit=1,
        scope=scope,
    )
    assert stale.outcomes[0].state.value == "failure"
    assert stale.outcomes[0].request_count == 0
    assert stale.outcomes[0].offers == ()
    assert stale.reused_provider_keys == ()
    assert new_preparation.create_private_provider_runner() is None
    assert network_calls == [
        "https://tstn.ru/api/v1/user/",
        "https://tstn.ru/api/v1/catalog/prices/",
        "https://tstn.ru/api/v1/catalog/available/",
        "https://tstn.ru/api/v1/user/",
        "https://tstn.ru/api/v1/catalog/prices/",
        "https://tstn.ru/api/v1/catalog/available/",
    ]


def test_boot_status_and_runtime_rebuild_do_not_create_tstn_catalog_or_send_http(
    tmp_path, monkeypatch,
):
    from averon_import.services.sourcing.providers.tstn import client as client_module

    runtime, settings, store = _runtime(tmp_path, monkeypatch)
    calls = []

    def forbidden_transport(*_args, **_kwargs):
        calls.append("http")
        raise AssertionError("passive TSTN paths must not send HTTP")

    monkeypatch.setattr(client_module, "_default_transport", forbidden_transport)
    runtime.service.public_config()
    assert runtime._tstn_runtime.readiness.catalog_state == "catalog_missing"
    rebuilt = create_sourcing_runtime(tmp_path / "data", settings, store)
    rebuilt._tstn_runtime.readiness
    rebuilt.service.public_config()
    assert not (
        tmp_path / "data" / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    ).exists()
    assert calls == []


def test_stale_generation_does_not_create_a_writable_catalog(tmp_path, monkeypatch):
    runtime, _settings, store = _runtime(tmp_path, monkeypatch)
    coordinator = runtime._tstn_runtime.create_catalog_sync_coordinator()
    assert coordinator is not None
    store.set(TSTN_CREDENTIALS_CONFIGURED, ROTATED_GENERATION)
    client = FakeCatalogClient([_item()])
    outcome = coordinator.sync(
        client=client,
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=GENERATION,
    )
    assert outcome.state == "failure"
    assert outcome.error_code == "TSTN_SYNC_STALE_CONFIGURATION"
    assert client.calls == []
    assert not coordinator.catalog_path.exists()


def test_concurrent_syncs_are_serialized_per_catalog(tmp_path, monkeypatch):
    runtime, _settings, _store = _runtime(tmp_path, monkeypatch)
    first = _sync(runtime, [_item()])
    assert first.state == "success"
    coordinator_a = runtime._tstn_runtime.create_catalog_sync_coordinator()
    coordinator_b = runtime._tstn_runtime.create_catalog_sync_coordinator()
    assert coordinator_a is not None and coordinator_b is not None

    entered = Event()
    release = Event()

    def block_page():
        entered.set()
        assert release.wait(5)

    blocking_client = FakeCatalogClient([_item(name="Новая позиция")], on_page=block_page)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            coordinator_a.sync,
            client=blocking_client,
            access_token=SecretStr(SYNTHETIC_TOKEN),
            credential_generation=GENERATION,
        )
        assert entered.wait(5)
        second_client = FakeCatalogClient([_item(name="Конфликтующая позиция")])
        second = coordinator_b.sync(
            client=second_client,
            access_token=SecretStr(SYNTHETIC_TOKEN),
            credential_generation=GENERATION,
        )
        assert second.state == "busy"
        assert second_client.calls == []
        release.set()
        completed = future.result(timeout=5)

    assert completed.state == "success"
    mirror = TstnCatalogMirror(coordinator_a.catalog_path, initialize=False)
    assert mirror.search(article="000101", limit=5)[0].name == "Новая позиция"


def test_generation_rotation_during_sync_keeps_last_good_snapshot(tmp_path, monkeypatch):
    runtime, settings, store = _runtime(tmp_path, monkeypatch)
    first = _sync(runtime, [_item()])
    assert first.state == "success"
    coordinator = runtime._tstn_runtime.create_catalog_sync_coordinator()
    assert coordinator is not None
    calls = []

    def rotate_generation():
        calls.append("supplier_attempt")
        with TSTN_CREDENTIAL_LOCK:
            settings.update({"sourcing": {"tstn": {
                "credential_generation": ROTATED_GENERATION,
            }}})
            store.set(TSTN_CREDENTIALS_CONFIGURED, ROTATED_GENERATION)

    stale = coordinator.sync(
        client=FakeCatalogClient([_item(name="Не опубликовано")], on_page=rotate_generation),
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=GENERATION,
    )
    reopened = TstnCatalogMirror(coordinator.catalog_path, initialize=False)
    assert stale.state == "failure"
    assert stale.error_code == "TSTN_SYNC_STALE_CONFIGURATION"
    assert calls == ["supplier_attempt"]
    assert reopened.revision == first.catalog_revision
    assert reopened.search(article="000101", limit=5)[0].name == "Крепёжный элемент"
    assert SYNTHETIC_TOKEN not in repr(stale)


def test_rotation_waits_until_publication_commit_and_failed_commit_rolls_back(
    tmp_path, monkeypatch,
):
    runtime, settings, store = _runtime(tmp_path, monkeypatch)
    original_sync = _sync(runtime, [_item()])
    assert original_sync.state == "success"
    coordinator = runtime._tstn_runtime.create_catalog_sync_coordinator()
    assert coordinator is not None
    original_check = TstnCatalogMirror._check_integrity_connection
    commit_check_entered = Event()
    rotation_attempted = Event()
    rotated = Event()

    def rotate_after_lock_releases():
        rotation_attempted.set()
        with TSTN_CREDENTIAL_LOCK:
            settings.update({"sourcing": {"tstn": {
                "credential_generation": ROTATED_GENERATION,
            }}})
            store.set(TSTN_CREDENTIALS_CONFIGURED, ROTATED_GENERATION)
        rotated.set()

    def paused_integrity_check(connection):
        commit_check_entered.set()
        rotator.start()
        assert rotation_attempted.wait(5)
        return original_check(connection)

    monkeypatch.setattr(
        TstnCatalogMirror,
        "_check_integrity_connection",
        staticmethod(paused_integrity_check),
    )
    rotator = Thread(target=rotate_after_lock_releases)
    try:
        # The integrity hook runs after publication_guard acquired the lock.
        # Starting the rotator there proves it waits until SQLite commit.
        result = coordinator.sync(
            client=FakeCatalogClient([_item(name="Опубликовано до ротации")]),
            access_token=SecretStr(SYNTHETIC_TOKEN),
            credential_generation=GENERATION,
        )
        assert commit_check_entered.is_set()
    finally:
        if rotator.is_alive():
            rotator.join(timeout=5)

    assert result.state == "success"
    assert rotated.is_set()
    assert TstnCatalogMirror(coordinator.catalog_path, initialize=False).revision != original_sync.catalog_revision

    # A failed integrity check inside the publication transaction is rolled
    # back and cannot replace the last complete snapshot.
    stable_revision = TstnCatalogMirror(coordinator.catalog_path, initialize=False).revision
    monkeypatch.setattr(
        TstnCatalogMirror,
        "_check_integrity_connection",
        staticmethod(lambda _connection: False),
    )
    failed = coordinator.sync(
        client=FakeCatalogClient([_item(name="Не должно сохраниться")]),
        access_token=SecretStr(SYNTHETIC_TOKEN),
        credential_generation=ROTATED_GENERATION,
    )
    monkeypatch.setattr(
        TstnCatalogMirror,
        "_check_integrity_connection",
        staticmethod(original_check),
    )
    restored = TstnCatalogMirror(coordinator.catalog_path, initialize=False)
    assert failed.state == "failure"
    assert failed.error_code == "TSTN_MIRROR_UPDATE_FAILED"
    assert restored.revision == stable_revision
    # The test-only generation was rotated, and the failed publication did
    # not leave the temporary candidate visible.
    assert "Не должно сохраниться" not in str(restored.search(article="000101", limit=5)[0].name)
