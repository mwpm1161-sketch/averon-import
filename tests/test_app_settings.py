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
    YANDEX_API_KEY,
    InsecureFileSecretStore,
    MemorySecretStore,
    create_secret_store,
    resolve_secret,
)

SECRET_VALUE = "yc-test-key-12345"


# ---------- AppSettings: defaults / load / cascade ----------


def test_defaults_match_documented_values():
    settings = AppSettings()
    assert settings.processing_mode == "cloud"
    assert settings.local.base_url == "http://127.0.0.1:11434/v1"
    assert settings.local.model == "qwen3:8b"
    assert settings.yandex.folder_id == ""
    assert settings.yandex.vision_model == "table"
    assert settings.yandex.llm_model == ""
    assert settings.yandex.chunk_pages == 8
    assert settings.yandex.request_timeout_s == 120.0
    assert settings.yandex.operation_timeout_s == 600.0
    assert settings.pipeline.min_confidence == 0.85


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
    service = AppSettingsService(tmp_path)
    assert service.settings.local.model == "from-env"
    assert service.settings.pipeline.min_confidence == 0.7


def test_invalid_json_falls_back_to_defaults_with_warning(tmp_path):
    (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert service.settings.processing_mode == "cloud"
    assert service.warnings and "повреждён" in service.warnings[0]


def test_unknown_keys_ignored_and_file_secrets_scrubbed(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "future_flag": True,
        "yandex": {"api_key": "LEAKED", "folder_id": "ok"},
    }), encoding="utf-8")
    service = AppSettingsService(tmp_path)
    assert not hasattr(service.settings, "future_flag")
    assert service.settings.yandex.folder_id == "ok"
    assert any("проигнорировано" in warning for warning in service.warnings)
    service.save()
    saved = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "LEAKED" not in saved and "api_key" not in saved and "future_flag" not in saved


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


def test_put_partial_section_merge_keeps_other_fields(api, tmp_path):
    app_module, _, service = api
    service.update({"yandex": {"folder_id": "keep-me", "chunk_pages": 16}})
    app_module.put_settings(app_module.SettingsUpdate(
        yandex=app_module.YandexSettingsUpdate(llm_model="qwen3.6-35b"),
    ))
    assert service.settings.yandex.folder_id == "keep-me"
    assert service.settings.yandex.chunk_pages == 16
    assert service.settings.yandex.llm_model == "qwen3.6-35b"


def test_replace_and_delete_api_key_without_reading_it_back(api):
    app_module, store, _ = api
    app_module.put_settings(app_module.SettingsUpdate(api_key="first-key"))
    app_module.put_settings(app_module.SettingsUpdate(api_key="second-key"))
    assert store.get(YANDEX_API_KEY) == "second-key"
    assert app_module.delete_yandex_api_key() == {"deleted": True}
    assert not store.has(YANDEX_API_KEY)
    app_module.delete_yandex_api_key()


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
