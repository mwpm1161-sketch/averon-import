"""Runtime construction for the sourcing-only Yandex AI Studio client."""

from __future__ import annotations

import os
from typing import Any

from averon_import.ai.config import AiSettings, ProviderSettings
from averon_import.ai.provider import OpenAICompatibleProvider
from averon_import.ai.service import AiCorrectionService
from averon_import.services.secrets import YANDEX_AI_API_KEY, resolve_secret


def _v1_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if base and not base.casefold().endswith("/v1"):
        base += "/v1"
    return base


def create_sourcing_ai_transport(settings_service: Any, secret_store: Any) -> AiCorrectionService:
    """Build a Yandex AI transport from the application's owned settings.

    OCR keeps its existing AI service.  This separate transport makes the
    sourcing ownership explicit and resolves non-secret settings and secrets
    through their respective application layers.
    """

    env_model = os.environ.get("AVERON_YANDEX_AI_MODEL", "").strip()
    env_base_url = os.environ.get("AVERON_YANDEX_AI_BASE_URL", "").strip()
    configured = settings_service.settings.yandex
    base = AiSettings.from_env()
    provider_settings = ProviderSettings(
        key="yandex",
        label="Yandex Cloud AI Studio",
        base_url=_v1_base_url(env_base_url or configured.llm_base_url),
        model=env_model or str(configured.llm_model or "").strip(),
        api_key=resolve_secret(
            os.environ.get("AVERON_YANDEX_AI_API_KEY"),
            secret_store,
            YANDEX_AI_API_KEY,
        ) or "",
        # API-key credentials use Api-Key; IAM-style credentials can use
        # Bearer.  The provider retries only on 401/403.
        auth_schemes=("Api-Key", "Bearer"),
    )
    runtime_settings = AiSettings(
        local=base.local,
        yandex=provider_settings,
        timeout_seconds=base.timeout_seconds,
        batch_size=base.batch_size,
        temperature=base.temperature,
        max_tokens=base.max_tokens,
    )
    provider = OpenAICompatibleProvider(
        provider_settings,
        timeout_seconds=runtime_settings.timeout_seconds,
        temperature=runtime_settings.temperature,
        max_tokens=runtime_settings.max_tokens,
    )
    return AiCorrectionService(settings=runtime_settings, providers={"yandex": provider})
