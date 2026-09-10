"""Application settings layer.

Deterministic cascade: ENV overrides > settings.json > defaults.
Secrets are never persisted here — they belong to the SecretStore
(see averon_import.services.secrets).

This stage only stores configuration; no runtime behaviour switches on it
yet (ProcessingCoordinator arrives in a later stage).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from averon_import.core.constants import PROCESSING_MODES

_FORBIDDEN_FILE_KEYS = {"api_key", "api-key", "secret", "secrets", "password", "token"}


class LocalAiSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3:8b"


class YandexCloudSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    folder_id: str = ""
    vision_model: str = "table"
    llm_model: str = ""
    vision_base_url: str = "https://ocr.api.cloud.yandex.net"
    llm_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    language_codes: list[str] = ["ru", "en"]
    chunk_pages: int = Field(default=8, ge=1, le=50)
    request_timeout_s: float = Field(default=120.0, gt=0, le=1800)
    operation_timeout_s: float = Field(default=600.0, gt=0, le=7200)


class PipelineTuningSettings(BaseModel):
    """Stored tuning values only; pipeline logic stays in the ai package."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    rules_enabled: bool = True
    validation_enabled: bool = True
    batch_size: int = Field(default=10, ge=1, le=30)
    min_confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class SourcingSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str = "local_catalog"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    processing_mode: Literal["local", "cloud", "hybrid"] = "cloud"  # type: ignore[valid-type]
    local: LocalAiSettings = Field(default_factory=LocalAiSettings)
    yandex: YandexCloudSettings = Field(default_factory=YandexCloudSettings)
    pipeline: PipelineTuningSettings = Field(default_factory=PipelineTuningSettings)
    sourcing: SourcingSettings = Field(default_factory=SourcingSettings)

    def public(self) -> dict:
        return {
            "processing_mode": self.processing_mode,
            "local": self.local.model_dump(),
            "yandex": self.yandex.model_dump(),
            "pipeline": self.pipeline.model_dump(),
            "sourcing": self.sourcing.model_dump(),
        }


def _scrub_forbidden_keys(node, warnings: list[str]) -> object:
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            if str(key).lower() in _FORBIDDEN_FILE_KEYS:
                warnings.append(
                    f"Поле «{key}» в settings.json проигнорировано: секреты не хранятся в файле настроек."
                )
                continue
            cleaned[key] = _scrub_forbidden_keys(value, warnings)
        return cleaned
    if isinstance(node, list):
        return [_scrub_forbidden_keys(item, warnings) for item in node]
    return node


class AppSettingsService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "settings.json"
        self.warnings: list[str] = []
        self.settings = self._load()
        self.apply_env_overrides()

    def _load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.warnings.append(
                f"settings.json повреждён ({exc}); используются значения по умолчанию."
            )
            return AppSettings()
        if not isinstance(raw, dict):
            self.warnings.append(
                "settings.json имеет неверную структуру; используются значения по умолчанию."
            )
            return AppSettings()
        raw = _scrub_forbidden_keys(raw, self.warnings)
        try:
            return AppSettings.model_validate(raw)
        except ValidationError as exc:
            detail = str(exc.errors()[0].get("msg", "")) if exc.errors() else ""
            self.warnings.append(
                f"settings.json содержит недопустимые значения ({detail}); "
                "используются значения по умолчанию."
            )
            return AppSettings()

    def apply_env_overrides(self) -> None:
        settings = self.settings
        mode = os.environ.get("AVERON_PROCESSING_MODE", "").strip().lower()
        if mode:
            if mode in PROCESSING_MODES:
                settings.processing_mode = mode  # type: ignore[assignment]
            else:
                self.warnings.append(
                    f"AVERON_PROCESSING_MODE=«{mode}» не распознан; допустимо: {', '.join(PROCESSING_MODES)}."
                )
        if value := os.environ.get("AVERON_LOCAL_AI_BASE_URL", "").strip():
            settings.local.base_url = value.rstrip("/")
        if value := os.environ.get("AVERON_LOCAL_AI_MODEL", "").strip():
            settings.local.model = value
        if value := os.environ.get("AVERON_YANDEX_AI_BASE_URL", "").strip():
            settings.yandex.llm_base_url = value.rstrip("/")
        if value := os.environ.get("AVERON_YANDEX_AI_MODEL", "").strip():
            settings.yandex.llm_model = value
        if value := os.environ.get("AVERON_YANDEX_LANGUAGE_CODES", "").strip():
            codes = [part.strip() for part in value.split(",") if part.strip()]
            if codes:
                settings.yandex.language_codes = codes
            else:
                self.warnings.append(
                    "AVERON_YANDEX_LANGUAGE_CODES пуст; используются ru, en."
                )
        if (value := _env_float("AVERON_AI_MIN_CONFIDENCE")) is not None:
            settings.pipeline.min_confidence = value
        if (value := _env_int("AVERON_AI_BATCH_SIZE")) is not None:
            settings.pipeline.batch_size = value
        if value := os.environ.get("AVERON_SOURCING_PROVIDER", "").strip():
            settings.sourcing.provider = value

    def update(self, patch: dict) -> AppSettings:
        current = json.loads(self.settings.model_dump_json())
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key] = {**current[key], **value}
            else:
                current[key] = value
        candidate = AppSettings.model_validate(current)
        self.settings = candidate
        self.apply_env_overrides()
        self.save()
        return self.settings

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("settings.json.tmp")
        payload = json.dumps(json.loads(self.settings.model_dump_json()), ensure_ascii=False, indent=2)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def warnings_snapshot(self) -> list[str]:
        return list(self.warnings)

    def public(self) -> dict:
        return {**self.settings.public(), "warnings": self.warnings_snapshot()}


def _env_float(name: str) -> float | None:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return None


def _env_int(name: str) -> int | None:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return None
