"""Runtime construction for the sourcing-only Yandex AI Studio client."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from averon_import.ai.config import AiSettings, ProviderSettings
from averon_import.ai.provider import OpenAICompatibleProvider
from averon_import.ai.service import AiCorrectionService
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.providers.base import SourcingProvider
from averon_import.services.sourcing.providers.demo_store_http import DemoStoreHttpProvider
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.providers.lemana_b2b import LemanaB2BProvider
from averon_import.services.sourcing.providers.etm_ipro import EtmIproProvider
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.service import SourcingService
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


@dataclass(frozen=True)
class SourcingRuntime:
    """Explicit composition root for the sourcing runtime dependencies."""

    repository: CatalogRepository
    providers: dict[str, SourcingProvider]
    service: SourcingService


def create_sourcing_runtime(
    data_dir: Path,
    settings_service: Any,
    secret_store: Any,
) -> SourcingRuntime:
    """Build the current sourcing providers and service from owned dependencies."""

    data_root = Path(data_dir)
    repository = CatalogRepository(data_root / "sourcing" / "catalog.sqlite3")
    local_provider = LocalCatalogProvider(repository)
    demo_store_provider = DemoStoreHttpProvider(
        settings_service.settings.sourcing.demo_store_base_url,
    )
    lemana_provider = LemanaB2BProvider(
        settings_service.settings.sourcing.lemana_b2b,
        secret_store,
        data_root,
    )
    etm_provider = EtmIproProvider(
        settings_service.settings.sourcing.etm_ipro,
        secret_store,
        data_root,
    )
    providers: dict[str, SourcingProvider] = {
        local_provider.key: local_provider,
        demo_store_provider.key: demo_store_provider,
        lemana_provider.key: lemana_provider,
        etm_provider.key: etm_provider,
    }
    configured_provider = str(settings_service.settings.sourcing.provider or "")
    default_provider = (
        configured_provider
        if configured_provider in providers
        else local_provider.key
    )
    sourcing_service = SourcingService(
        providers,
        default_provider=default_provider,
        ai=SourcingAIService(create_sourcing_ai_transport(settings_service, secret_store)),
        cache=SourcingCache(data_root / "sourcing" / "cache.json"),
    )
    return SourcingRuntime(
        repository=repository,
        providers=providers,
        service=sourcing_service,
    )
