"""Runtime construction for the sourcing-only Yandex AI Studio client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

from averon_import.ai.config import AiSettings, ProviderSettings
from averon_import.ai.provider import OpenAICompatibleProvider
from averon_import.ai.service import AiCorrectionService
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.catalog_repository import CatalogRepository
from averon_import.services.sourcing.providers.base import SourcingProvider
from averon_import.services.sourcing.providers.demo_store_http import DemoStoreHttpProvider
from averon_import.services.sourcing.providers.execution import (
    OutcomeNativeProviderAdapter,
    ProviderRunner,
)
from averon_import.services.sourcing.providers.execution_adapters import (
    EtmIproExecutionAdapter,
    LemanaB2BExecutionAdapter,
    LocalCatalogExecutionAdapter,
)
from averon_import.services.sourcing.providers.vseinstrumenti.adapter import (
    VseinstrumentiExecutionAdapter,
)
from averon_import.services.sourcing.providers.vseinstrumenti.client import (
    is_valid_region_id,
)
from averon_import.services.sourcing.providers.vseinstrumenti.runtime_client import (
    SecretBackedVseinstrumentiClient,
)
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider
from averon_import.services.sourcing.providers.lemana_b2b import LemanaB2BProvider
from averon_import.services.sourcing.providers.etm_ipro import EtmIproProvider
from averon_import.services.sourcing.providers.one_c_history import OneCHistoryProvider
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.service import SourcingService
from averon_import.services.one_c_history.repository import OneCHistoryRepository
from averon_import.services.secrets import (
    TSTN_CREDENTIALS_CONFIGURED,
    VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
    YANDEX_AI_API_KEY,
    resolve_secret,
)
from averon_import.services.sourcing.providers.tstn.adapter import (
    TstnCommercialExecutionAdapter,
)
from averon_import.services.sourcing.providers.tstn.authorization import (
    TSTN_CREDENTIAL_LOCK,
)
from averon_import.services.sourcing.providers.tstn.mirror import TstnCatalogMirror


VI_CREDENTIAL_LOCK = RLock()


@dataclass(frozen=True)
class TstnReadiness:
    """Safe local readiness categories; never implies remote API reachability."""

    credential_state: str
    catalog_state: str
    configured: bool
    catalog_ready: bool
    live_verified: bool = False


@dataclass(frozen=True)
class TstnRuntimePreparation:
    """Private TN4B composition, intentionally outside all provider registries."""

    adapter: TstnCommercialExecutionAdapter | None = field(repr=False, compare=False)
    _settings_service: Any = field(repr=False, compare=False)
    _secret_store: Any = field(repr=False, compare=False)
    _mirror: TstnCatalogMirror | None = field(repr=False, compare=False)
    _prepared_enabled: bool = field(repr=False)
    _prepared_generation: str = field(repr=False)
    _catalog_path: Path | None = field(default=None, repr=False, compare=False)
    _storage_error: bool = field(default=False, repr=False)
    _preparation_error: bool = field(default=False, repr=False)

    def __repr__(self) -> str:
        return "TstnRuntimePreparation(<private>)"

    @property
    def readiness(self) -> TstnReadiness:
        """Read only settings and the non-secret marker; never scans the catalog."""

        try:
            # Credential updates take this same lock before changing either
            # SecretStore markers or the server-owned generation.
            with TSTN_CREDENTIAL_LOCK:
                try:
                    current = self._settings_service.settings.sourcing.tstn
                    enabled = current.enabled is True
                    generation = current.credential_generation
                except Exception:
                    return TstnReadiness(
                        credential_state="configuration_error",
                        catalog_state="not_checked",
                        configured=False,
                        catalog_ready=False,
                    )
                if not enabled:
                    credential_state = "disabled"
                elif self._preparation_error:
                    credential_state = "runtime_preparation_error"
                elif not self._prepared_enabled or generation != self._prepared_generation:
                    credential_state = "runtime_generation_stale"
                else:
                    try:
                        marker = self._secret_store.get(TSTN_CREDENTIALS_CONFIGURED)
                    except Exception:
                        credential_state = "credential_store_error"
                    else:
                        if not marker:
                            credential_state = "authorization_pair_missing"
                        elif marker != generation:
                            credential_state = "credentials_marker_stale"
                        elif self.adapter is None:
                            credential_state = "runtime_preparation_error"
                        else:
                            credential_state = "configured"
        except Exception:
            return TstnReadiness(
                credential_state="configuration_error",
                catalog_state="not_checked",
                configured=False,
                catalog_ready=False,
            )

        if not enabled:
            return TstnReadiness(
                credential_state=credential_state,
                catalog_state="not_checked",
                configured=False,
                catalog_ready=False,
            )

        catalog_state = self._catalog_readiness(enabled=enabled)
        return TstnReadiness(
            credential_state=credential_state,
            catalog_state=catalog_state,
            configured=credential_state == "configured",
            catalog_ready=catalog_state == "ready",
        )

    def _catalog_readiness(self, *, enabled: bool) -> str:
        if not enabled:
            return "not_checked"
        if self._storage_error:
            return "storage_error"
        if self._mirror is None:
            return "catalog_missing"
        try:
            revision = self._mirror.revision
            stats = self._mirror.stats()
            if not isinstance(stats, dict) or stats.get("catalog_revision") != revision:
                return "storage_error"
        except Exception:
            return "storage_error"
        if stats.get("integrity_ok") is False:
            return "integrity_error"
        if revision == "unknown":
            return "catalog_unsynchronized"
        if stats.get("search_index_ready") is True:
            return "ready"
        return "integrity_unconfirmed"


def _prepare_tstn_runtime(
    data_root: Path,
    settings_service: Any,
    secret_store: Any,
) -> TstnRuntimePreparation:
    """Prepare TSTN privately, without probing credentials or contacting TSTN."""

    try:
        with TSTN_CREDENTIAL_LOCK:
            configured = settings_service.settings.sourcing.tstn
            enabled = configured.enabled is True
            generation = configured.credential_generation
    except Exception:
        return TstnRuntimePreparation(
            adapter=None,
            _settings_service=settings_service,
            _secret_store=secret_store,
            _mirror=None,
            _prepared_enabled=False,
            _prepared_generation="",
            _preparation_error=True,
        )

    if not enabled:
        return TstnRuntimePreparation(
            adapter=None,
            _settings_service=settings_service,
            _secret_store=secret_store,
            _mirror=None,
            _prepared_enabled=False,
            _prepared_generation=generation,
        )

    catalog_path = data_root / "sourcing" / "providers" / "tstn" / "catalog.sqlite3"
    mirror: TstnCatalogMirror | None = None
    storage_error = False
    try:
        # TstnCatalogMirror creates its parent/schema; instantiate only when a
        # persisted mirror already exists so runtime prep cannot create one.
        catalog_path.stat()
    except FileNotFoundError:
        pass
    except Exception:
        storage_error = True
    else:
        try:
            mirror = TstnCatalogMirror(catalog_path, initialize=False)
        except Exception:
            storage_error = True

    def current_configuration() -> tuple[bool, str]:
        current = settings_service.settings.sourcing.tstn
        return current.enabled, current.credential_generation

    adapter: TstnCommercialExecutionAdapter | None = None
    preparation_error = False
    try:
        adapter = TstnCommercialExecutionAdapter(
            mirror,
            secret_store,
            enabled=enabled,
            credential_generation=generation,
            request_timeout_s=configured.request_timeout_s,
            current_configuration=current_configuration,
        )
    except Exception:
        preparation_error = True

    return TstnRuntimePreparation(
        adapter=adapter,
        _settings_service=settings_service,
        _secret_store=secret_store,
        _mirror=mirror,
        _prepared_enabled=enabled,
        _prepared_generation=generation,
        _catalog_path=catalog_path,
        _storage_error=storage_error,
        _preparation_error=preparation_error,
    )


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
    execution_providers: Mapping[str, OutcomeNativeProviderAdapter]
    provider_runner: ProviderRunner
    one_c_history_provider: OneCHistoryProvider | None = None
    _tstn_runtime: TstnRuntimePreparation | None = field(default=None, repr=False, compare=False)

    @property
    def execution_provider_keys(self) -> tuple[str, ...]:
        """Canonical internal capability list; reading it never probes providers."""

        return tuple(sorted(self.execution_providers))


def create_sourcing_runtime(
    data_dir: Path,
    settings_service: Any,
    secret_store: Any,
    *,
    one_c_history_repository: OneCHistoryRepository | None = None,
) -> SourcingRuntime:
    """Build the current sourcing providers and service from owned dependencies."""

    data_root = Path(data_dir)
    tstn_runtime = _prepare_tstn_runtime(data_root, settings_service, secret_store)
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
    # Keep the outcome-native domain explicit. Do not derive this registry
    # from `providers`: legacy-only providers must never become selectable by
    # merely being added to the legacy runtime map.
    execution_providers: dict[str, OutcomeNativeProviderAdapter] = {
        etm_provider.key: EtmIproExecutionAdapter(etm_provider),
        lemana_provider.key: LemanaB2BExecutionAdapter(lemana_provider),
        local_provider.key: LocalCatalogExecutionAdapter(local_provider),
    }
    vi_settings = settings_service.settings.sourcing.vseinstrumenti
    try:
        vi_is_configured = (
            vi_settings.enabled
            and is_valid_region_id(vi_settings.region_id)
            and secret_store.get(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
            == vi_settings.credential_generation
        )
        if vi_is_configured:
            vi_client = SecretBackedVseinstrumentiClient(
                secret_store,
                environment=vi_settings.environment,
                timeout_seconds=vi_settings.request_timeout_s,
                credential_generation=vi_settings.credential_generation,
                current_generation=lambda: settings_service.settings.sourcing.vseinstrumenti.credential_generation,
                credential_lock=VI_CREDENTIAL_LOCK,
            )
            execution_providers[VseinstrumentiExecutionAdapter.key] = VseinstrumentiExecutionAdapter(
                vi_client,
                region_id=vi_settings.region_id,
                credential_generation=vi_settings.credential_generation,
            )
    except Exception:
        # Invalid or unavailable local credentials fail closed for this
        # optional execution-only provider without disabling other sourcing.
        pass
    provider_runner = ProviderRunner(execution_providers)
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
        provider_runner=provider_runner,
        one_c_history_provider=(
            OneCHistoryProvider(one_c_history_repository)
            if one_c_history_repository is not None
            else None
        ),
    )
    return SourcingRuntime(
        repository=repository,
        providers=providers,
        service=sourcing_service,
        execution_providers=MappingProxyType(execution_providers),
        provider_runner=provider_runner,
        one_c_history_provider=sourcing_service.one_c_history_provider,
        _tstn_runtime=tstn_runtime,
    )
