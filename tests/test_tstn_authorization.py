from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import traceback
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from averon_import.services.secrets import (
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
    TSTN_X_SP_SECRET,
    MemorySecretStore,
)
from averon_import.services.sourcing.providers.tstn import (
    SecretStoreTstnAuthorizer,
    TstnApiClient,
    TstnApiError,
    TstnAuthorizationResponse,
    TstnErrorCategory,
    TstnHttpResponse,
)


SERVICE = "tn3-offline-service-synthetic"
CLIENT_KEY = "tn3-offline-client-key-synthetic"
X_SP_SECRET = "tn3-offline-xsp-synthetic"
ACCESS_TOKEN = "tn3-offline-access-token-synthetic"
GENERATION = "1" * 32


class TrackingMemorySecretStore(MemorySecretStore):
    def __init__(self):
        super().__init__()
        self.read_keys: list[str] = []

    def get(self, key: str) -> str | None:
        self.read_keys.append(key)
        return super().get(key)


def configured_store(generation: str = GENERATION) -> TrackingMemorySecretStore:
    store = TrackingMemorySecretStore()
    store.set(TSTN_SERVICE_IDENTIFIER, SERVICE)
    store.set(TSTN_CLIENT_KEY, CLIENT_KEY)
    store.set(TSTN_X_SP_SECRET, X_SP_SECRET)
    store.set(TSTN_CREDENTIALS_CONFIGURED, generation)
    return store


def fake_tstn_client(calls: list, *, entered: Event | None = None, release: Event | None = None):
    def transport(request, timeout):
        calls.append((request, timeout))
        if entered is not None:
            entered.set()
        if release is not None and not release.wait(timeout=5):
            raise TimeoutError("synthetic test transport wait expired")
        return TstnHttpResponse(
            status_code=200,
            body=(
                '{"access_token":"'
                + ACCESS_TOKEN
                + '","access_date":"not-an-expiry"}'
            ).encode("utf-8"),
        )

    return TstnApiClient(transport=transport, max_attempts=1)


def authorizer(store, client, generation=GENERATION, current=None, *, enabled=True):
    config = current or (lambda: (True, generation))
    return SecretStoreTstnAuthorizer(
        store,
        client,
        enabled=enabled,
        credential_generation=generation,
        current_configuration=config,
    )


def test_creation_is_passive_and_explicit_authorization_uses_one_documented_post():
    store = configured_store()
    calls = []
    client = fake_tstn_client(calls)
    session = authorizer(store, client)

    assert store.read_keys == []
    assert calls == []
    token = session.authorize()

    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == ACCESS_TOKEN
    assert ACCESS_TOKEN not in str(token)
    assert SERVICE not in repr(session) and CLIENT_KEY not in repr(session)
    assert client.request_attempt_count == 1
    assert len(calls) == 1
    request, timeout = calls[0]
    query = parse_qs(urlsplit(request.full_url).query)
    assert query == {"service": [SERVICE], "key": [CLIENT_KEY]}
    assert "X-SP-Secret" not in request.headers
    assert store.read_keys == [
        TSTN_CREDENTIALS_CONFIGURED,
        TSTN_SERVICE_IDENTIFIER,
        TSTN_CLIENT_KEY,
        TSTN_CREDENTIALS_CONFIGURED,
    ]
    assert TSTN_X_SP_SECRET not in store.read_keys
    assert ACCESS_TOKEN not in store._values.values()
    assert timeout == client.timeout_seconds

    with pytest.raises(TstnApiError) as second_attempt:
        session.authorize()
    assert second_attempt.value.category == TstnErrorCategory.REQUEST_LIMIT
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["stale_generation", "disabled", "missing_marker", "missing_key"])
def test_invalid_or_stale_configuration_fails_before_supplier_http(failure):
    store = configured_store()
    current_generation = GENERATION
    if failure == "missing_marker":
        store.delete(TSTN_CREDENTIALS_CONFIGURED)
    elif failure == "missing_key":
        store.delete(TSTN_CLIENT_KEY)
    if failure == "stale_generation":
        current_generation = "2" * 32
    calls = []
    client = fake_tstn_client(calls)
    session = authorizer(
        store,
        client,
        current=lambda: (failure != "disabled", current_generation),
    )

    with pytest.raises(TstnApiError) as caught:
        session.authorize()

    assert caught.value.category == TstnErrorCategory.MISCONFIGURED
    assert calls == []
    assert client.request_attempt_count == 0
    assert ACCESS_TOKEN not in str(caught.value)
    if failure == "stale_generation":
        assert store.read_keys == []


def test_authorizer_sanitizes_errors_from_an_injected_client():
    store = configured_store()

    class LeakyFakeClient:
        def authorize(self, *, service, key):
            raise RuntimeError(
                f"synthetic upstream failure {service.get_secret_value()} {key.get_secret_value()}"
            )

    session = authorizer(store, LeakyFakeClient())
    with pytest.raises(TstnApiError) as caught:
        session.authorize()
    rendered_traceback = traceback.format_exc()

    assert caught.value.category == TstnErrorCategory.TRANSPORT
    for value in (SERVICE, CLIENT_KEY):
        assert value not in str(caught.value)
        assert value not in rendered_traceback


def test_authorization_and_rotation_are_serialized_by_the_same_lock(tmp_path, monkeypatch):
    from averon_import import main as app_module
    from averon_import.services.app_settings import AppSettingsService

    service = AppSettingsService(tmp_path)
    store = configured_store()
    monkeypatch.setattr(app_module, "app_settings_service", service)
    monkeypatch.setattr(app_module, "secret_store", store)
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    entered = Event()
    release = Event()
    rotation_started = Event()
    calls = []
    client = fake_tstn_client(calls, entered=entered, release=release)
    session = authorizer(
        store,
        client,
        current=lambda: (
            service.settings.sourcing.tstn.enabled,
            service.settings.sourcing.tstn.credential_generation,
        ),
        enabled=True,
    )
    service.update({"sourcing": {"tstn": {"enabled": True, "credential_generation": GENERATION}}})

    def rotate():
        rotation_started.set()
        return app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier="tn3-rotated-service-synthetic",
            tstn_client_key="tn3-rotated-key-synthetic",
        ))

    with ThreadPoolExecutor(max_workers=2) as pool:
        auth_future = pool.submit(session.authorize)
        assert entered.wait(timeout=5)
        rotate_future = pool.submit(rotate)
        assert rotation_started.wait(timeout=5)
        assert not rotate_future.done()
        release.set()
        assert auth_future.result(timeout=5).get_secret_value() == ACCESS_TOKEN
        rotate_future.result(timeout=5)

    assert len(calls) == 1
    assert store.get(TSTN_SERVICE_IDENTIFIER) == "tn3-rotated-service-synthetic"
    assert service.settings.sourcing.tstn.credential_generation != GENERATION


def test_old_authorization_handle_cannot_read_rotated_credentials(tmp_path, monkeypatch):
    from averon_import import main as app_module
    from averon_import.services.app_settings import AppSettingsService

    service = AppSettingsService(tmp_path)
    service.update({
        "sourcing": {"tstn": {"enabled": True, "credential_generation": GENERATION}},
    })
    store = configured_store()
    monkeypatch.setattr(app_module, "app_settings_service", service)
    monkeypatch.setattr(app_module, "secret_store", store)
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    calls = []
    stale_handle = authorizer(
        store,
        fake_tstn_client(calls),
        current=lambda: (
            service.settings.sourcing.tstn.enabled,
            service.settings.sourcing.tstn.credential_generation,
        ),
    )

    app_module.put_settings(app_module.SettingsUpdate(
        tstn_service_identifier="tn3-new-service-synthetic",
        tstn_client_key="tn3-new-key-synthetic",
    ))
    store.read_keys.clear()

    with pytest.raises(TstnApiError) as caught:
        stale_handle.authorize()

    assert caught.value.category == TstnErrorCategory.MISCONFIGURED
    assert calls == []
    assert TSTN_SERVICE_IDENTIFIER not in store.read_keys
    assert TSTN_CLIENT_KEY not in store.read_keys
