from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from averon_import.services.app_settings import (
    PROCESSING_MODES,
    AppSettings,
    AppSettingsService,
)
from averon_import.services.secrets import (
    ETM_IPRO_LOGIN,
    ETM_IPRO_PASSWORD,
    LEMANA_B2B_CLIENT_SECRET,
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
    TSTN_X_SP_SECRET,
    TSTN_X_SP_SECRET_CONFIGURED,
    VSEINSTRUMENTI_BEARER_TOKEN,
    VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
    YANDEX_AI_API_KEY,
    YANDEX_API_KEY,
    InsecureFileSecretStore,
    MemorySecretStore,
    SecretStoreError,
    create_secret_store,
    resolve_secret,
)

SECRET_VALUE = "yc-test-key-12345"


class FaultInjectingMemorySecretStore(MemorySecretStore):
    """Memory store that can fail once before or after one operation."""

    def __init__(self):
        super().__init__()
        self._next_failure: tuple[str, str, bool, int] | None = None
        self.read_keys: list[str] = []

    def fail_once_at(
        self, method: str, key: str, *, after: bool = False, skip: int = 0
    ) -> None:
        self._next_failure = (method, key, after, skip)

    def _consume_failure(self, method: str, key: str) -> bool | None:
        if self._next_failure is None:
            return None
        expected_method, expected_key, after, skip = self._next_failure
        if (method, key) != (expected_method, expected_key):
            return None
        if skip:
            self._next_failure = (expected_method, expected_key, after, skip - 1)
            return None
        self._next_failure = None
        if not after:
            raise SecretStoreError("injected SecretStore failure")
        return True

    def set(self, key: str, value: str) -> None:
        fail_after = self._consume_failure("set", key)
        super().set(key, value)
        if fail_after:
            raise SecretStoreError("injected SecretStore failure")

    def get(self, key: str) -> str | None:
        self.read_keys.append(key)
        fail_after = self._consume_failure("get", key)
        value = super().get(key)
        if fail_after:
            raise SecretStoreError("injected SecretStore failure")
        return value

    def delete(self, key: str) -> None:
        fail_after = self._consume_failure("delete", key)
        super().delete(key)
        if fail_after:
            raise SecretStoreError("injected SecretStore failure")


# ---------- AppSettings: defaults / load / cascade ----------


def test_defaults_match_documented_values():
    settings = AppSettings()
    assert settings.processing_mode == "cloud"
    assert settings.local.base_url == "http://127.0.0.1:11434/v1"
    assert settings.local.model == "qwen3:8b"
    assert settings.yandex.folder_id == ""
    assert settings.yandex.vision_model == "table"
    assert settings.yandex.llm_model == ""
    assert settings.sourcing.provider == "local_catalog"
    assert settings.sourcing.demo_store_base_url == "http://127.0.0.1:8877"
    assert settings.yandex.chunk_pages == 8
    assert settings.yandex.request_timeout_s == 120.0
    assert settings.yandex.operation_timeout_s == 600.0
    assert settings.pipeline.min_confidence == 0.85
    assert settings.sourcing.tstn.enabled is False
    assert settings.sourcing.tstn.request_timeout_s == 10.0
    assert "credential_generation" not in json.dumps(settings.public(), ensure_ascii=False)


def test_load_from_settings_json(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "processing_mode": "cloud",
        "local": {"model": "llama3:8b"},
        "yandex": {"folder_id": "b1gfolder", "llm_model": "qwen3.6-35b", "chunk_pages": 12},
    }), encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert service.settings.processing_mode == "cloud"
    assert service.settings.local.model == "llama3:8b"
    assert service.settings.local.base_url == "http://127.0.0.1:11434/v1"
    assert service.settings.yandex.folder_id == "b1gfolder"
    assert service.settings.yandex.chunk_pages == 12
    assert service.warnings == []


def test_env_overrides_settings_file(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "local": {"model": "from-file"},
        "pipeline": {"min_confidence": 0.9},
    }), encoding="utf-8")
    monkeypatch.setenv("AVERON_LOCAL_AI_MODEL", "from-env")
    monkeypatch.setenv("AVERON_AI_MIN_CONFIDENCE", "0.7")
    monkeypatch.setenv("AVERON_DEMO_STORE_BASE_URL", "https://demo.example///")
    service = AppSettingsService(tmp_path)
    assert service.settings.local.model == "from-env"
    assert service.settings.pipeline.min_confidence == 0.7
    assert service.settings.sourcing.demo_store_base_url == "https://demo.example"


@pytest.mark.parametrize("value", ["not-a-uuid", "1234", "{0c5b2444-70a0-4932-980c-b4dc0d3f02b5}"])
def test_invalid_vi_region_env_override_preserves_settings_json(monkeypatch, tmp_path, value):
    region = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
    (tmp_path / "settings.json").write_text(json.dumps({
        "sourcing": {"vseinstrumenti": {"region_id": region, "request_timeout_s": 8.5}},
    }), encoding="utf-8")
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_REGION_ID", value)
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S", raising=False)

    service = AppSettingsService(tmp_path)

    assert service.settings.sourcing.vseinstrumenti.region_id == region
    assert any("AVERON_VSEINSTRUMENTI_REGION_ID" in warning for warning in service.warnings)
    assert all(value not in warning for warning in service.warnings)


@pytest.mark.parametrize("value", ["-1", "30.01", "nan", "NaN", "inf", "Infinity", "-inf", "1e9999", "not-a-number"])
def test_invalid_vi_timeout_env_override_preserves_settings_json(monkeypatch, tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({
        "sourcing": {"vseinstrumenti": {"region_id": "", "request_timeout_s": 8.5}},
    }), encoding="utf-8")
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_REGION_ID", raising=False)
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S", value)

    service = AppSettingsService(tmp_path)

    assert service.settings.sourcing.vseinstrumenti.request_timeout_s == 8.5
    assert any("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S" in warning for warning in service.warnings)
    assert all(value not in warning for warning in service.warnings)


def test_valid_vi_env_overrides_are_model_validated_and_applied(monkeypatch, tmp_path):
    region = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_REGION_ID", region.upper())
    monkeypatch.setenv("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S", "12.5")

    service = AppSettingsService(tmp_path)

    assert service.settings.sourcing.vseinstrumenti.region_id == region
    assert service.settings.sourcing.vseinstrumenti.request_timeout_s == 12.5
    assert not any("AVERON_VSEINSTRUMENTI_REGION_ID" in warning for warning in service.warnings)
    assert not any("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S" in warning for warning in service.warnings)


def test_demo_store_url_is_validated_and_persisted(tmp_path):
    service = AppSettingsService(tmp_path)
    service.update({"sourcing": {"demo_store_base_url": "http://store.example///"}})
    assert service.settings.sourcing.demo_store_base_url == "http://store.example"
    assert AppSettingsService(tmp_path).settings.sourcing.demo_store_base_url == "http://store.example"
    with pytest.raises(ValidationError):
        service.update({"sourcing": {"demo_store_base_url": "ftp://store.example"}})


def test_nested_sourcing_settings_update_preserves_untouched_lemana_fields(tmp_path):
    service = AppSettingsService(tmp_path)
    service.update({
        "sourcing": {
            "lemana_b2b": {
                "enabled": True,
                "environment": "prod",
                "client_id": "abc",
                "region_id": 34,
                "request_timeout_s": 10,
            }
        }
    })
    service.update({"sourcing": {"lemana_b2b": {"request_timeout_s": 20}}})

    assert service.settings.sourcing.lemana_b2b.model_dump() == {
        "enabled": True,
        "environment": "prod",
        "client_id": "abc",
        "region_id": 34,
        "request_timeout_s": 20.0,
    }


def test_etm_ipro_settings_are_non_secret_and_warehouse_codes_are_normalized(tmp_path, monkeypatch):
    monkeypatch.setenv("AVERON_ETM_IPRO_ENABLED", "true")
    monkeypatch.setenv("AVERON_ETM_IPRO_ENVIRONMENT", "prod")
    monkeypatch.setenv("AVERON_ETM_IPRO_WAREHOUSE_CODES", "15, 22,15")
    monkeypatch.setenv("AVERON_ETM_IPRO_MAX_LIVE_CANDIDATES", "7")
    service = AppSettingsService(tmp_path)

    assert service.settings.sourcing.etm_ipro.enabled is True
    assert service.settings.sourcing.etm_ipro.environment == "prod"
    assert service.settings.sourcing.etm_ipro.warehouse_codes == ["15", "22"]
    assert service.settings.sourcing.etm_ipro.max_live_candidates == 7
    assert "password" not in (tmp_path / "settings.json").read_text(encoding="utf-8") if (tmp_path / "settings.json").exists() else True


def test_vseinstrumenti_settings_validate_local_configuration_and_hide_generation(tmp_path):
    service = AppSettingsService(tmp_path)
    service.update({
        "sourcing": {
            "vseinstrumenti": {
                "enabled": True,
                "environment": "test",
                "region_id": "0C5B2444-70A0-4932-980C-B4DC0D3F02B5",
                "request_timeout_s": 5,
            }
        }
    })

    assert service.settings.sourcing.vseinstrumenti.region_id == "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"
    assert service.settings.sourcing.vseinstrumenti.environment == "test"
    assert "credential_generation" not in json.dumps(service.public(), ensure_ascii=False)
    assert service.settings.sourcing.vseinstrumenti.credential_generation in service.path.read_text(encoding="utf-8")

    with pytest.raises(ValidationError):
        service.update({"sourcing": {"vseinstrumenti": {"region_id": "not-a-fias-uuid"}}})


def test_tstn_settings_are_bounded_and_hide_server_generation(tmp_path):
    service = AppSettingsService(tmp_path)
    service.update({"sourcing": {"tstn": {"enabled": True, "request_timeout_s": 12.5}}})

    assert service.settings.sourcing.tstn.enabled is True
    assert service.settings.sourcing.tstn.request_timeout_s == 12.5
    assert "credential_generation" not in json.dumps(service.public(), ensure_ascii=False)
    assert service.settings.sourcing.tstn.credential_generation in service.path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        service.update({"sourcing": {"tstn": {"request_timeout_s": 30.1}}})


def test_tstn_secret_like_fields_in_settings_json_are_scrubbed(tmp_path):
    fake_service = "tn3-file-service-synthetic"
    fake_key = "tn3-file-key-synthetic"
    fake_xsp = "tn3-file-xsp-synthetic"
    (tmp_path / "settings.json").write_text(json.dumps({
        "sourcing": {"tstn": {
            "enabled": True,
            "service": fake_service,
            "key": fake_key,
            "x_sp_secret": fake_xsp,
        }},
    }), encoding="utf-8")

    service = AppSettingsService(tmp_path)

    assert service.settings.sourcing.tstn.enabled is True
    assert service.warnings
    service.save()
    saved = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert fake_service not in saved and fake_key not in saved and fake_xsp not in saved


def test_invalid_json_falls_back_to_defaults_with_warning(tmp_path):
    (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert service.settings.processing_mode == "cloud"
    assert service.warnings and "повреждён" in service.warnings[0]


def test_unknown_keys_ignored_and_file_secrets_scrubbed(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "future_flag": True,
        "yandex": {"api_key": "LEAKED", "folder_id": "ok"},
        "sourcing": {"vseinstrumenti": {"bearer_token": "LEAKED", "region_id": ""}},
    }), encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert not hasattr(service.settings, "future_flag")
    assert service.settings.yandex.folder_id == "ok"
    assert any("проигнорировано" in warning for warning in service.warnings)
    service.save()
    saved = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "LEAKED" not in saved and "api_key" not in saved and "bearer_token" not in saved and "future_flag" not in saved


def test_invalid_processing_mode_in_file_falls_back(tmp_path):
    (tmp_path / "settings.json").write_text('{"processing_mode": "teleport"}', encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert service.settings.processing_mode == "cloud"
    assert service.warnings


# ---------- atomic save / update validation ----------


def test_update_is_atomic_and_valid_json(tmp_path):
    service = AppSettingsService(tmp_path)
    service.update({"yandex": {"folder_id": "folder-42"}})
    assert not (tmp_path / "settings.json.tmp").exists()
    reloaded = AppSettingsService(tmp_path)
    assert reloaded.settings.yandex.folder_id == "folder-42"
    assert reloaded.settings.yandex.chunk_pages == 8


def test_update_rejects_invalid_processing_mode(tmp_path):
    service = AppSettingsService(tmp_path)
    with pytest.raises(ValidationError):
        service.update({"processing_mode": "banana"})
    assert service.settings.processing_mode == "cloud"


def test_settings_api_preserves_explicit_boolean_false(api):
    app_module, _, service = api
    assert service.settings.pipeline.enabled is True
    app_module.put_settings(app_module.SettingsUpdate(
        pipeline=app_module.PipelineSettingsUpdate(enabled=False),
    ))
    assert service.settings.pipeline.enabled is False
    persisted = json.loads(service.path.read_text(encoding="utf-8"))
    assert persisted["pipeline"]["enabled"] is False


def test_processing_modes_constant():
    assert PROCESSING_MODES == ("local", "cloud", "hybrid")


# ---------- SecretStore implementations ----------


def test_memory_store_has_set_delete():
    store = MemorySecretStore()
    assert not store.has(YANDEX_API_KEY)
    store.set(YANDEX_API_KEY, SECRET_VALUE)
    assert store.has(YANDEX_API_KEY)
    assert store.get(YANDEX_API_KEY) == SECRET_VALUE
    store.delete(YANDEX_API_KEY)
    assert not store.has(YANDEX_API_KEY)
    store.delete(YANDEX_API_KEY)


def test_memory_store_keeps_vision_and_ai_credentials_independent():
    store = MemorySecretStore()
    store.set(YANDEX_API_KEY, "vision-key")
    store.set(YANDEX_AI_API_KEY, "ai-key")
    assert store.get(YANDEX_API_KEY) == "vision-key"
    assert store.get(YANDEX_AI_API_KEY) == "ai-key"
    store.delete(YANDEX_AI_API_KEY)
    assert store.get(YANDEX_API_KEY) == "vision-key"
    assert store.get(YANDEX_AI_API_KEY) is None


def test_insecure_file_store_roundtrip_and_atomicity(tmp_path):
    store = InsecureFileSecretStore(tmp_path)
    store.set(YANDEX_API_KEY, SECRET_VALUE)
    assert store.get(YANDEX_API_KEY) == SECRET_VALUE
    assert not (tmp_path / "secrets.insecure.tmp").exists()
    store.set(YANDEX_API_KEY, "second")
    assert store.get(YANDEX_API_KEY) == "second"
    store.delete(YANDEX_API_KEY)
    assert store.get(YANDEX_API_KEY) is None
    assert store.is_insecure


def test_factory_routing_without_touching_os_store():
    assert isinstance(create_secret_store(kind="memory"), MemorySecretStore)
    if os.name == "nt":
        store = create_secret_store(kind="wincred")
        assert store.backend_name == "windows-credential-manager"
        assert not store.is_insecure


def test_env_key_has_priority_over_store(monkeypatch, tmp_path):
    store = MemorySecretStore()
    store.set(YANDEX_API_KEY, "from-store")
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    assert resolve_secret(None, store, YANDEX_API_KEY) == "from-store"
    monkeypatch.setenv("AVERON_YANDEX_AI_API_KEY", "from-env")
    assert resolve_secret(os.environ.get("AVERON_YANDEX_AI_API_KEY"), store, YANDEX_API_KEY) == "from-env"


def test_resolve_returns_none_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    empty = MemorySecretStore()
    assert resolve_secret(None, empty, YANDEX_API_KEY) is None
    empty.set(YANDEX_API_KEY, "   ")
    assert resolve_secret(None, empty, YANDEX_API_KEY) is None


# ---------- Settings API (main.py endpoints, isolated stores) ----------


@pytest.fixture()
def api(monkeypatch, tmp_path):
    from averon_import import main as app_module

    store = MemorySecretStore()
    service = AppSettingsService(tmp_path)
    monkeypatch.setattr(app_module, "secret_store", store)
    monkeypatch.setattr(app_module, "app_settings_service", service)
    monkeypatch.delenv("AVERON_YANDEX_AI_API_KEY", raising=False)
    return app_module, store, service


def _assert_secret_absent(payload, secret_value):
    assert secret_value not in json.dumps(payload, ensure_ascii=False)


def test_get_settings_exposes_only_boolean_for_key(api):
    app_module, store, _ = api
    store.set(YANDEX_API_KEY, SECRET_VALUE)
    payload = app_module.get_settings()
    assert payload["yandex"]["api_key_configured"] is True
    assert payload["secret_backend"] == "memory"
    _assert_secret_absent(payload, SECRET_VALUE)
    assert "api_key" not in payload["yandex"]


def test_get_settings_distinguishes_vision_and_ai_key_status(api):
    app_module, store, _ = api
    store.set(YANDEX_API_KEY, "vision-secret")
    payload = app_module.get_settings()
    assert payload["yandex"]["api_key_configured"] is True
    assert payload["yandex"]["vision_api_key_configured"] is True
    assert payload["yandex"]["ai_api_key_configured"] is False
    store.set(YANDEX_AI_API_KEY, "ai-secret")
    payload = app_module.get_settings()
    assert payload["yandex"]["vision_api_key_configured"] is True
    assert payload["yandex"]["ai_api_key_configured"] is True
    _assert_secret_absent(payload, "vision-secret")
    _assert_secret_absent(payload, "ai-secret")


def test_api_config_never_contains_secret(api):
    app_module, _, _ = api
    config_payload = app_module.config()
    _assert_secret_absent(config_payload, SECRET_VALUE)
    assert config_payload["settings"]["processing_mode"] in PROCESSING_MODES


def test_put_saves_key_to_store_not_settings_json(api, tmp_path):
    app_module, store, service = api
    response = app_module.put_settings(app_module.SettingsUpdate(
        processing_mode="cloud",
        yandex=app_module.YandexSettingsUpdate(folder_id="f-1"),
        api_key=f"  {SECRET_VALUE}  ",
    ))
    assert store.get(YANDEX_API_KEY) == SECRET_VALUE.strip()
    assert response["yandex"]["api_key_configured"] is True
    raw_file = (service.path).read_text(encoding="utf-8") if service.path.exists() else ""
    assert SECRET_VALUE not in raw_file and "api_key" not in raw_file
    assert service.settings.processing_mode == "cloud"


def test_put_saves_dedicated_ai_key_without_touching_vision_key(api, tmp_path):
    app_module, store, service = api
    response = app_module.put_settings(app_module.SettingsUpdate(
        yandex=app_module.YandexSettingsUpdate(
            folder_id="f-ai",
            llm_model="gpt://f-ai/qwen3.6-35b-a3b/latest",
        ),
        ai_api_key="  dedicated-ai-key  ",
    ))
    assert store.get(YANDEX_API_KEY) is None
    assert store.get(YANDEX_AI_API_KEY) == "dedicated-ai-key"
    assert response["yandex"]["api_key_configured"] is False
    assert response["yandex"]["ai_api_key_configured"] is True
    raw_file = service.path.read_text(encoding="utf-8")
    assert "dedicated-ai-key" not in raw_file


def test_put_and_delete_vision_and_ai_keys_are_independent(api):
    app_module, store, _ = api
    app_module.put_settings(app_module.SettingsUpdate(
        api_key="vision-key",
        ai_api_key="ai-key",
    ))
    assert store.get(YANDEX_API_KEY) == "vision-key"
    assert store.get(YANDEX_AI_API_KEY) == "ai-key"
    app_module.put_settings(app_module.SettingsUpdate(delete_yandex_api_key=True))
    assert store.get(YANDEX_API_KEY) is None
    assert store.get(YANDEX_AI_API_KEY) == "ai-key"
    app_module.put_settings(app_module.SettingsUpdate(delete_yandex_ai_api_key=True))
    assert store.get(YANDEX_API_KEY) is None
    assert store.get(YANDEX_AI_API_KEY) is None


def test_put_partial_section_merge_keeps_other_fields(api, tmp_path):
    app_module, _, service = api
    service.update({"yandex": {"folder_id": "keep-me", "chunk_pages": 16}})
    app_module.put_settings(app_module.SettingsUpdate(
        yandex=app_module.YandexSettingsUpdate(llm_model="qwen3.6-35b"),
    ))
    assert service.settings.yandex.folder_id == "keep-me"
    assert service.settings.yandex.chunk_pages == 16
    assert service.settings.yandex.llm_model == "qwen3.6-35b"


def test_put_selects_demo_store_without_changing_vision_or_ai_credentials(api):
    app_module, _, service = api
    payload = app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            provider="demo_store_http",
            demo_store_base_url="http://demo.example///",
        ),
    ))
    assert service.settings.sourcing.provider == "demo_store_http"
    assert service.settings.sourcing.demo_store_base_url == "http://demo.example"
    assert app_module.sourcing_service.default_provider == "demo_store_http"
    assert app_module.sourcing_service.provider().key == "demo_store_http"
    assert payload["sourcing"]["demo_store_base_url"] == "http://demo.example"


def test_settings_api_nested_lemana_update_preserves_other_fields(api):
    app_module, _, service = api
    app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            lemana_b2b=app_module.LemanaB2BSettingsUpdate(
                enabled=True,
                environment="prod",
                client_id="abc",
                region_id=34,
                request_timeout_s=10,
            )
        )
    ))
    app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            lemana_b2b=app_module.LemanaB2BSettingsUpdate(request_timeout_s=20),
        )
    ))

    assert service.settings.sourcing.lemana_b2b.enabled is True
    assert service.settings.sourcing.lemana_b2b.environment == "prod"
    assert service.settings.sourcing.lemana_b2b.client_id == "abc"
    assert service.settings.sourcing.lemana_b2b.region_id == 34
    assert service.settings.sourcing.lemana_b2b.request_timeout_s == 20


def test_replace_and_delete_api_key_without_reading_it_back(api):
    app_module, store, _ = api
    app_module.put_settings(app_module.SettingsUpdate(api_key="first-key"))
    app_module.put_settings(app_module.SettingsUpdate(api_key="second-key"))
    assert store.get(YANDEX_API_KEY) == "second-key"
    assert app_module.delete_yandex_api_key() == {"deleted": True}
    assert not store.has(YANDEX_API_KEY)
    app_module.delete_yandex_api_key()


def test_lemana_client_secret_is_write_only_and_not_persisted(api):
    app_module, store, service = api
    secret = "lemana-client-secret-private"

    payload = app_module.put_settings(
        app_module.SettingsUpdate(lemana_client_secret=secret)
    )

    assert store.get(LEMANA_B2B_CLIENT_SECRET) == secret
    assert payload["sourcing"]["lemana_b2b"]["client_secret_configured"] is True
    encoded_payload = json.dumps(payload, ensure_ascii=False)
    assert secret not in encoded_payload
    assert "client_secret" not in (service.path.read_text(encoding="utf-8") if service.path.exists() else "")
    assert app_module.delete_lemana_client_secret() == {"deleted": True}
    assert store.get(LEMANA_B2B_CLIENT_SECRET) is None


def test_lemana_secret_delete_rebuilds_runtime_and_public_state(api, monkeypatch):
    app_module, store, _ = api
    monkeypatch.delenv("AVERON_LEMANA_B2B_CLIENT_SECRET", raising=False)
    app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            lemana_b2b=app_module.LemanaB2BSettingsUpdate(
                enabled=True,
                environment="test",
                client_id="client-1",
                region_id=34,
            )
        ),
        lemana_client_secret="private-secret",
    ))
    before = app_module.sourcing_runtime.providers["lemana_b2b"]
    assert before.configured is True

    assert app_module.delete_lemana_client_secret() == {"deleted": True}
    after = app_module.sourcing_runtime.providers["lemana_b2b"]
    payload = app_module.get_settings()

    assert store.get(LEMANA_B2B_CLIENT_SECRET) is None
    assert after.configured is False
    assert payload["sourcing"]["lemana_b2b"]["client_secret_configured"] is False


def test_etm_credentials_are_write_only_and_runtime_rebuilds(api, monkeypatch):
    app_module, store, service = api
    monkeypatch.delenv("AVERON_ETM_IPRO_LOGIN", raising=False)
    monkeypatch.delenv("AVERON_ETM_IPRO_PASSWORD", raising=False)
    payload = app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            provider="etm_ipro",
            etm_ipro=app_module.EtmIproSettingsUpdate(
                enabled=True, environment="test", warehouse_codes=["WH-1"]
            ),
        ),
        etm_login="etm-login-private",
        etm_password="etm-password-private",
    ))

    assert store.get(ETM_IPRO_LOGIN) == "etm-login-private"
    assert store.get(ETM_IPRO_PASSWORD) == "etm-password-private"
    assert payload["sourcing"]["etm_ipro"]["login_configured"] is True
    assert payload["sourcing"]["etm_ipro"]["password_configured"] is True
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "etm-login-private" not in encoded
    assert "etm-password-private" not in encoded
    saved = service.path.read_text(encoding="utf-8")
    assert "etm_password" not in saved
    assert app_module.delete_etm_ipro_login() == {"deleted": True}
    assert app_module.delete_etm_ipro_password() == {"deleted": True}
    assert not store.has(ETM_IPRO_LOGIN)
    assert not store.has(ETM_IPRO_PASSWORD)


def test_tstn_credentials_rotate_generation_and_remain_write_only(api, monkeypatch, tmp_path):
    app_module, store, service = api
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    first_service = "tn3-service-first-synthetic"
    first_key = "tn3-key-first-synthetic"
    first_xsp = "tn3-xsp-first-synthetic"
    first_payload = app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            tstn=app_module.TstnSettingsUpdate(enabled=True, request_timeout_s=4.5),
        ),
        tstn_service_identifier=first_service,
        tstn_client_key=first_key,
        tstn_x_sp_secret=first_xsp,
    ))
    first_generation = service.settings.sourcing.tstn.credential_generation

    assert store.get(TSTN_SERVICE_IDENTIFIER) == first_service
    assert store.get(TSTN_CLIENT_KEY) == first_key
    assert store.get(TSTN_X_SP_SECRET) == first_xsp
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == first_generation
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) == first_generation
    assert first_payload["sourcing"]["tstn"]["credentials_configured"] is True
    assert first_payload["sourcing"]["tstn"]["x_sp_secret_configured"] is True
    assert "credential_generation" not in json.dumps(first_payload, ensure_ascii=False)
    for secret in (first_service, first_key, first_xsp):
        assert secret not in json.dumps(first_payload, ensure_ascii=False)
        assert secret not in service.path.read_text(encoding="utf-8")

    replacement_service = "tn3-service-replacement-synthetic"
    replacement_key = "tn3-key-replacement-synthetic"
    second_payload = app_module.put_settings(app_module.SettingsUpdate(
        tstn_service_identifier=replacement_service,
        tstn_client_key=replacement_key,
    ))
    second_generation = service.settings.sourcing.tstn.credential_generation
    assert second_generation != first_generation
    assert store.get(TSTN_SERVICE_IDENTIFIER) == replacement_service
    assert store.get(TSTN_CLIENT_KEY) == replacement_key
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == second_generation
    assert store.get(TSTN_X_SP_SECRET) == first_xsp
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) == second_generation
    assert "credential_generation" not in json.dumps(second_payload, ensure_ascii=False)

    second_xsp = "tn3-xsp-replacement-synthetic"
    third_payload = app_module.put_settings(app_module.SettingsUpdate(
        tstn_x_sp_secret=second_xsp,
    ))
    third_generation = service.settings.sourcing.tstn.credential_generation
    assert third_generation != second_generation
    assert store.get(TSTN_X_SP_SECRET) == second_xsp
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == third_generation
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) == third_generation
    assert second_xsp not in json.dumps(third_payload, ensure_ascii=False)

    assert app_module.delete_tstn_x_sp_secret() == {"deleted": True}
    fourth_generation = service.settings.sourcing.tstn.credential_generation
    assert fourth_generation != third_generation
    assert store.get(TSTN_X_SP_SECRET) is None
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) is None
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) == fourth_generation

    assert app_module.delete_tstn_authorization_credentials() == {"deleted": True}
    fifth_generation = service.settings.sourcing.tstn.credential_generation
    assert fifth_generation != fourth_generation
    assert store.get(TSTN_SERVICE_IDENTIFIER) is None
    assert store.get(TSTN_CLIENT_KEY) is None
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) is None


def test_tstn_partial_or_conflicting_credential_updates_are_rejected_without_echo(api, tmp_path):
    from fastapi import HTTPException

    app_module, store, service = api
    private_service = "tn3-invalid-service-must-not-echo"
    generation = service.settings.sourcing.tstn.credential_generation
    with pytest.raises(HTTPException) as partial:
        app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier=private_service,
        ))
    assert partial.value.status_code == 400
    assert private_service not in str(partial.value.detail)
    assert service.settings.sourcing.tstn.credential_generation == generation
    assert not store.has(TSTN_SERVICE_IDENTIFIER)

    with pytest.raises(HTTPException) as invalid_type:
        app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier=private_service,
            tstn_client_key=123,
        ))
    assert invalid_type.value.status_code == 400
    assert private_service not in str(invalid_type.value.detail)
    assert service.settings.sourcing.tstn.credential_generation == generation

    with pytest.raises(HTTPException) as conflicting:
        app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier=private_service,
            tstn_client_key="tn3-invalid-key-synthetic",
            delete_tstn_authorization_credentials=True,
        ))
    assert conflicting.value.status_code == 400
    assert private_service not in str(conflicting.value.detail)
    assert not service.path.exists()


def test_tstn_passive_status_and_runtime_rebuild_do_not_read_credentials_or_make_http(
    api, monkeypatch, tmp_path,
):
    from averon_import.services.sourcing.providers.tstn import client as tstn_client_module

    app_module, _, service = api
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    generation = service.settings.sourcing.tstn.credential_generation
    service.update({
        "sourcing": {
            "tstn": {"enabled": True, "credential_generation": generation},
        },
    })

    class ReadTrackingStore(MemorySecretStore):
        def __init__(self):
            super().__init__()
            self.read_keys: list[str] = []

        def get(self, key):
            self.read_keys.append(key)
            return super().get(key)

    store = ReadTrackingStore()
    store.set(TSTN_SERVICE_IDENTIFIER, "tn3-status-service-private")
    store.set(TSTN_CLIENT_KEY, "tn3-status-key-private")
    store.set(TSTN_X_SP_SECRET, "tn3-status-xsp-private")
    store.set(TSTN_CREDENTIALS_CONFIGURED, generation)
    store.set(TSTN_X_SP_SECRET_CONFIGURED, generation)
    monkeypatch.setattr(app_module, "secret_store", store)
    outbound = []

    def forbidden_transport(*args, **kwargs):
        outbound.append((args, kwargs))
        raise AssertionError("passive TSTN operations must not make HTTP")

    monkeypatch.setattr(tstn_client_module, "_default_transport", forbidden_transport)

    app_module._rebuild_sourcing_runtime()
    config = app_module.config()
    settings = app_module.get_settings()
    providers = app_module.sourcing_providers()
    capabilities = app_module.get_multi_provider_sourcing_capabilities(None)

    assert settings["sourcing"]["tstn"]["credentials_configured"] is True
    assert settings["sourcing"]["tstn"]["x_sp_secret_configured"] is True
    assert "credential_generation" not in json.dumps(config, ensure_ascii=False)
    assert "tstn" not in str(capabilities).casefold()
    assert all(provider["key"] != "tstn" for provider in providers["providers"])
    assert TSTN_SERVICE_IDENTIFIER not in store.read_keys
    assert TSTN_CLIENT_KEY not in store.read_keys
    assert TSTN_X_SP_SECRET not in store.read_keys
    assert outbound == []


@pytest.mark.parametrize(
    ("operation", "method", "key", "after", "skip"),
    [
        ("replace", "delete", TSTN_CREDENTIALS_CONFIGURED, False, 0),
        ("replace", "delete", TSTN_X_SP_SECRET_CONFIGURED, True, 0),
        ("replace", "set", TSTN_SERVICE_IDENTIFIER, False, 0),
        ("replace", "set", TSTN_SERVICE_IDENTIFIER, True, 0),
        ("replace", "set", TSTN_CLIENT_KEY, False, 0),
        ("replace", "set", TSTN_CLIENT_KEY, True, 0),
        ("replace", "get", TSTN_SERVICE_IDENTIFIER, False, 0),
        ("replace", "get", TSTN_CLIENT_KEY, True, 0),
        ("replace", "set", TSTN_CREDENTIALS_CONFIGURED, False, 0),
        ("replace", "set", TSTN_CREDENTIALS_CONFIGURED, True, 0),
        ("replace", "get", TSTN_CREDENTIALS_CONFIGURED, False, 1),
        ("replace", "set", TSTN_X_SP_SECRET_CONFIGURED, True, 0),
        ("replace", "get", TSTN_X_SP_SECRET_CONFIGURED, False, 1),
        ("delete_pair", "delete", TSTN_SERVICE_IDENTIFIER, True, 0),
        ("delete_pair", "delete", TSTN_CLIENT_KEY, False, 0),
        ("delete_xsp", "delete", TSTN_X_SP_SECRET, False, 0),
        ("delete_xsp", "delete", TSTN_X_SP_SECRET, True, 0),
    ],
)
def test_tstn_secret_store_faults_fail_closed_and_never_report_false_success(
    api, monkeypatch, tmp_path, operation, method, key, after, skip,
):
    from fastapi import HTTPException

    app_module, _, service = api
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    store = FaultInjectingMemorySecretStore()
    monkeypatch.setattr(app_module, "secret_store", store)
    app_module.put_settings(app_module.SettingsUpdate(
        tstn_service_identifier="tn3-old-service-private",
        tstn_client_key="tn3-old-key-private",
        tstn_x_sp_secret="tn3-old-xsp-private",
    ))
    prior_generation = service.settings.sourcing.tstn.credential_generation
    store.fail_once_at(method, key, after=after, skip=skip)

    if operation == "replace":
        request = lambda: app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier="tn3-new-service-private",
            tstn_client_key="tn3-new-key-private",
        ))
    elif operation == "delete_pair":
        request = app_module.delete_tstn_authorization_credentials
    else:
        request = app_module.delete_tstn_x_sp_secret

    with pytest.raises(HTTPException) as failure:
        request()

    current_generation = service.settings.sourcing.tstn.credential_generation
    assert failure.value.status_code == 503
    for value in (
        "tn3-old-service-private",
        "tn3-old-key-private",
        "tn3-old-xsp-private",
        "tn3-new-service-private",
        "tn3-new-key-private",
    ):
        assert value not in str(failure.value.detail)
    assert current_generation != prior_generation
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) != current_generation
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) != current_generation
    restarted = AppSettingsService(service.data_dir)
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) != restarted.settings.sourcing.tstn.credential_generation
    assert store.get(TSTN_X_SP_SECRET_CONFIGURED) != restarted.settings.sourcing.tstn.credential_generation
    if service.path.exists():
        saved = service.path.read_text(encoding="utf-8")
        assert "tn3-old-service-private" not in saved
        assert "tn3-old-key-private" not in saved
        assert "tn3-old-xsp-private" not in saved


@pytest.mark.parametrize("after", [False, True])
def test_tstn_settings_save_failure_invalidates_secret_markers(api, monkeypatch, tmp_path, after):
    from fastapi import HTTPException

    app_module, store, service = api
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "runtime")
    original_save = service.save
    injected = False

    def fail_once():
        nonlocal injected
        if not injected:
            injected = True
            if after:
                original_save()
            raise OSError("synthetic settings persistence failure")
        original_save()

    monkeypatch.setattr(service, "save", fail_once)
    with pytest.raises(HTTPException) as failure:
        app_module.put_settings(app_module.SettingsUpdate(
            tstn_service_identifier="tn3-save-service-private",
            tstn_client_key="tn3-save-key-private",
        ))

    generation = service.settings.sourcing.tstn.credential_generation
    assert failure.value.status_code == 503
    assert "tn3-save-service-private" not in str(failure.value.detail)
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) != generation
    restarted = AppSettingsService(service.data_dir)
    assert store.get(TSTN_CREDENTIALS_CONFIGURED) != restarted.settings.sourcing.tstn.credential_generation
    assert store.get(TSTN_SERVICE_IDENTIFIER) is None
    assert store.get(TSTN_CLIENT_KEY) is None


def test_vseinstrumenti_bearer_token_is_write_only_rotates_generation_and_rebuilds(api, monkeypatch):
    app_module, store, service = api
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_ENABLED", raising=False)
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_ENVIRONMENT", raising=False)
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_REGION_ID", raising=False)
    monkeypatch.delenv("AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S", raising=False)
    token_one = "vi.secret-token-one"
    token_two = "vi.secret-token-two"
    secret_reads = []
    original_get = store.get

    def tracked_get(key):
        if key == VSEINSTRUMENTI_BEARER_TOKEN:
            secret_reads.append(key)
        return original_get(key)

    monkeypatch.setattr(store, "get", tracked_get)

    first_payload = app_module.put_settings(app_module.SettingsUpdate(
        sourcing=app_module.SourcingSettingsUpdate(
            vseinstrumenti=app_module.VseinstrumentiSettingsUpdate(
                enabled=True,
                environment="test",
                region_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
                request_timeout_s=5,
            ),
        ),
        vseinstrumenti_bearer_token=token_one,
    ))
    first_generation = service.settings.sourcing.vseinstrumenti.credential_generation
    first_revision = app_module.sourcing_runtime.execution_providers["vseinstrumenti"]._affinity().config_revision

    assert secret_reads == []
    assert original_get(VSEINSTRUMENTI_BEARER_TOKEN) == token_one
    assert store.has(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
    assert original_get(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED) == first_generation
    assert first_payload["sourcing"]["vseinstrumenti"]["bearer_token_configured"] is True
    assert token_one not in json.dumps(first_payload, ensure_ascii=False)
    assert "credential_generation" not in json.dumps(first_payload, ensure_ascii=False)
    saved = service.path.read_text(encoding="utf-8")
    assert token_one not in saved and "bearer_token" not in saved
    assert "vseinstrumenti" not in app_module.sourcing_runtime.providers

    second_payload = app_module.put_settings(app_module.SettingsUpdate(
        vseinstrumenti_bearer_token=token_two,
    ))
    second_generation = service.settings.sourcing.vseinstrumenti.credential_generation
    second_revision = app_module.sourcing_runtime.execution_providers["vseinstrumenti"]._affinity().config_revision

    assert secret_reads == []
    assert original_get(VSEINSTRUMENTI_BEARER_TOKEN) == token_two
    assert store.has(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
    assert original_get(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED) == second_generation
    assert second_payload["sourcing"]["vseinstrumenti"]["bearer_token_configured"] is True
    assert second_generation != first_generation
    assert second_revision != first_revision

    app_module.put_settings(app_module.SettingsUpdate(delete_vseinstrumenti_bearer_token=True))
    third_generation = service.settings.sourcing.vseinstrumenti.credential_generation
    assert third_generation != second_generation
    assert not store.has(VSEINSTRUMENTI_BEARER_TOKEN)
    assert not store.has(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
    assert "vseinstrumenti" not in app_module.sourcing_runtime.execution_providers
    assert secret_reads == []

    assert app_module.delete_vseinstrumenti_bearer_token() == {"deleted": True}
    assert secret_reads == []
    assert not store.has(VSEINSTRUMENTI_BEARER_TOKEN)
    assert "vseinstrumenti" not in app_module.sourcing_runtime.execution_providers


@pytest.mark.parametrize(
    ("operation", "method", "key", "after"),
    [
        ("save", "delete", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("save", "set", VSEINSTRUMENTI_BEARER_TOKEN, False),
        ("save", "set", VSEINSTRUMENTI_BEARER_TOKEN, True),
        ("save", "set", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("save", "set", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, True),
        ("save", "get", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("replace", "delete", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("replace", "set", VSEINSTRUMENTI_BEARER_TOKEN, False),
        ("replace", "set", VSEINSTRUMENTI_BEARER_TOKEN, True),
        ("replace", "set", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("replace", "set", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, True),
        ("replace", "get", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("delete", "delete", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
        ("delete", "delete", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, True),
        ("delete", "delete", VSEINSTRUMENTI_BEARER_TOKEN, False),
        ("delete", "delete", VSEINSTRUMENTI_BEARER_TOKEN, True),
        ("delete", "get", VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, False),
    ],
)
def test_vi_credential_store_failures_fail_closed_and_survive_restart(
    api, monkeypatch, tmp_path, operation, method, key, after,
):
    from fastapi import HTTPException

    from averon_import.services.sourcing.runtime import create_sourcing_runtime
    from averon_import.services.sourcing.providers.vseinstrumenti import client as vi_client_module

    app_module, _, service = api
    transport_calls = []

    def forbidden_transport(*args, **kwargs):
        transport_calls.append((args, kwargs))
        raise AssertionError("credential failure tests must not contact VI")

    monkeypatch.setattr(vi_client_module, "_default_transport", forbidden_transport)
    for name in (
        "AVERON_VSEINSTRUMENTI_ENABLED",
        "AVERON_VSEINSTRUMENTI_ENVIRONMENT",
        "AVERON_VSEINSTRUMENTI_REGION_ID",
        "AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S",
        "AVERON_VSEINSTRUMENTI_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    store = FaultInjectingMemorySecretStore()
    monkeypatch.setattr(app_module, "secret_store", store)

    settings_patch = app_module.SourcingSettingsUpdate(
        vseinstrumenti=app_module.VseinstrumentiSettingsUpdate(
            enabled=True,
            environment="test",
            region_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            request_timeout_s=5,
        ),
    )
    old_adapter = None
    if operation != "save":
        app_module.put_settings(app_module.SettingsUpdate(
            sourcing=settings_patch,
            vseinstrumenti_bearer_token="vi.old-private-token",
        ))
        old_adapter = app_module.sourcing_runtime.execution_providers["vseinstrumenti"]

    prior_generation = service.settings.sourcing.vseinstrumenti.credential_generation
    store.fail_once_at(method, key, after=after)
    if operation == "save":
        request = lambda: app_module.put_settings(app_module.SettingsUpdate(
            sourcing=settings_patch,
            vseinstrumenti_bearer_token="vi.new-private-token",
        ))
    elif operation == "replace":
        request = lambda: app_module.put_settings(app_module.SettingsUpdate(
            vseinstrumenti_bearer_token="vi.replacement-private-token",
        ))
    else:
        request = app_module.delete_vseinstrumenti_bearer_token

    with pytest.raises(HTTPException) as failure:
        request()

    current_generation = service.settings.sourcing.vseinstrumenti.credential_generation
    assert failure.value.status_code == 503
    assert "private-token" not in str(failure.value.detail)
    assert current_generation != prior_generation
    assert store.get(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED) != current_generation
    assert app_module.get_settings()["sourcing"]["vseinstrumenti"]["bearer_token_configured"] is False
    assert "vseinstrumenti" not in app_module.sourcing_runtime.execution_providers

    if old_adapter is not None:
        from averon_import.services.sourcing.providers.vseinstrumenti.client import (
            VseinstrumentiApiError,
            VseinstrumentiErrorCategory,
        )

        with pytest.raises(VseinstrumentiApiError) as stale_runtime:
            old_adapter.client.search_products(
                "test", region_id="0c5b2444-70a0-4932-980c-b4dc0d3f02b5", limit=1,
            )
        assert stale_runtime.value.category == VseinstrumentiErrorCategory.MISCONFIGURED

    restarted_settings = AppSettingsService(service.data_dir)
    restarted = create_sourcing_runtime(tmp_path / "restarted-runtime", restarted_settings, store)
    assert "vseinstrumenti" not in restarted.execution_providers
    assert transport_calls == []
    assert VSEINSTRUMENTI_BEARER_TOKEN not in store.read_keys
    settings_json = service.path.read_text(encoding="utf-8")
    assert "private-token" not in settings_json
    assert "bearer_token" not in settings_json


def test_vseinstrumenti_invalid_token_and_invalid_configuration_fail_closed(api):
    from fastapi import HTTPException

    app_module, store, service = api
    with pytest.raises(HTTPException) as invalid_token:
        app_module.put_settings(app_module.SettingsUpdate(vseinstrumenti_bearer_token="bad token"))
    assert invalid_token.value.status_code == 400
    assert "bad token" not in str(invalid_token.value.detail)
    assert not store.has(VSEINSTRUMENTI_BEARER_TOKEN)

    with pytest.raises(HTTPException) as invalid_region:
        app_module.put_settings(app_module.SettingsUpdate(
            sourcing=app_module.SourcingSettingsUpdate(
                vseinstrumenti=app_module.VseinstrumentiSettingsUpdate(
                    enabled=True,
                    region_id="not-a-fias-uuid",
                ),
            ),
            vseinstrumenti_bearer_token="vi.valid-token",
        ))
    assert invalid_region.value.status_code == 400
    assert not store.has(VSEINSTRUMENTI_BEARER_TOKEN)
    assert "credential_generation" not in service.path.read_text(encoding="utf-8") if service.path.exists() else True


def test_vseinstrumenti_is_publicly_available_only_with_current_local_configuration(api, monkeypatch):
    app_module, store, _ = api
    for name in (
        "AVERON_VSEINSTRUMENTI_ENABLED",
        "AVERON_VSEINSTRUMENTI_ENVIRONMENT",
        "AVERON_VSEINSTRUMENTI_REGION_ID",
        "AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    store.set(VSEINSTRUMENTI_BEARER_TOKEN, "vi.public-test-token")
    app_module.app_settings_service.update({
        "sourcing": {
            "vseinstrumenti": {
                "enabled": True,
                "environment": "test",
                "region_id": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            },
        },
    })
    store.set(
        VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
        app_module.app_settings_service.settings.sourcing.vseinstrumenti.credential_generation,
    )
    app_module._rebuild_sourcing_runtime()

    capabilities = app_module.get_multi_provider_sourcing_capabilities(None)

    assert "vseinstrumenti" in app_module.sourcing_runtime.execution_providers
    assert [item["key"] for item in capabilities["providers"]] == ["vseinstrumenti"]
    assert capabilities["enabled"] is False  # One configured provider cannot start a comparison.
    assert "vi.public-test-token" not in str(capabilities)
    assert "credential_generation" not in str(capabilities)


def test_config_status_capabilities_and_runtime_rebuild_are_supplier_network_free(
    api,
    tmp_path,
    monkeypatch,
):
    from averon_import.services.sourcing.providers.etm_ipro.client import EtmIproClient
    from averon_import.services.sourcing.providers.lemana_b2b.client import LemanaB2BClient
    from averon_import.services.sourcing.providers.vseinstrumenti import client as vi_client_module

    app_module, store, service = api
    for name in (
        "AVERON_LEMANA_B2B_CLIENT_SECRET",
        "AVERON_ETM_IPRO_LOGIN",
        "AVERON_ETM_IPRO_PASSWORD",
        "AVERON_VSEINSTRUMENTI_ENABLED",
        "AVERON_VSEINSTRUMENTI_ENVIRONMENT",
        "AVERON_VSEINSTRUMENTI_REGION_ID",
        "AVERON_VSEINSTRUMENTI_REQUEST_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)

    service.update({
        "sourcing": {
            "vseinstrumenti": {
                "enabled": True,
                "environment": "test",
                "region_id": "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
            },
        },
    })
    store.set(
        VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
        service.settings.sourcing.vseinstrumenti.credential_generation,
    )
    token_reads = []
    original_get = store.get

    def tracked_get(key):
        if key == VSEINSTRUMENTI_BEARER_TOKEN:
            token_reads.append(key)
        return original_get(key)

    monkeypatch.setattr(store, "get", tracked_get)
    calls = []

    def forbidden_transport(*_args, **_kwargs):
        calls.append("supplier-http")
        raise AssertionError("passive config/status/capability reads must not call suppliers")

    monkeypatch.setattr(
        "averon_import.services.sourcing.providers.demo_store_http._default_transport",
        forbidden_transport,
    )
    monkeypatch.setattr(EtmIproClient, "check_access", forbidden_transport)
    monkeypatch.setattr(LemanaB2BClient, "check_access", forbidden_transport)
    monkeypatch.setattr(vi_client_module, "_default_transport", forbidden_transport)

    for name in (
        "demo_store_provider", "sourcing_provider", "sourcing_repository",
        "sourcing_runtime", "sourcing_service",
    ):
        monkeypatch.setattr(app_module, name, getattr(app_module, name))
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "app-data")

    app_module._rebuild_sourcing_runtime()
    config_payload = app_module.config()
    providers_payload = app_module.sourcing_providers()
    capabilities = app_module.get_multi_provider_sourcing_capabilities(None)
    demo_status = next(
        item for item in providers_payload["providers"]
        if item["key"] == "demo_store_http"
    )

    assert "vseinstrumenti" in app_module.sourcing_runtime.execution_providers
    assert capabilities["providers"] == [
        {"key": "vseinstrumenti", "label": "ВсеИнструменты.ru"},
    ]
    assert demo_status["reachable"] is None
    assert config_payload["sourcing"]["provider"]["key"] == "local_catalog"
    assert calls == []
    assert token_reads == []


def test_put_rejects_invalid_payload_values():
    from averon_import.main import SettingsUpdate

    with pytest.raises(ValidationError):
        SettingsUpdate.model_validate({"processing_mode": "banana"})
    parsed = SettingsUpdate.model_validate({"yandex": {"api_key": "ignored-unknown-field", "folder_id": "ok"}})
    assert not hasattr(parsed.yandex, "api_key")
    assert parsed.yandex.folder_id == "ok"


def test_put_out_of_range_value_rejected_by_storage_model(api):
    from fastapi import HTTPException

    app_module, _, service = api
    payload = app_module.SettingsUpdate(
        pipeline=app_module.PipelineSettingsUpdate(batch_size=999),
    )
    with pytest.raises(HTTPException) as exc_info:
        app_module.put_settings(payload)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail and "batch_size" in exc_info.value.detail
    assert service.settings.pipeline.batch_size == 10


def test_put_validation_translates_to_http_error(api, monkeypatch):
    from fastapi import HTTPException

    app_module, _, _ = api

    def raise_bad(patch):
        raise HTTPException(400, "Недопустимые настройки")

    monkeypatch.setattr(app_module.app_settings_service, "update", raise_bad)
    with pytest.raises(HTTPException) as exc_info:
        app_module.put_settings(app_module.SettingsUpdate(local=app_module.LocalSettingsUpdate(model="x")))
    assert exc_info.value.status_code == 400
