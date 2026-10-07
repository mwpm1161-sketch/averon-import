"""Inactive outcome-native adapters for the existing sourcing providers.

These adapters are explicit opt-ins for ProviderRunner and are not composed
into the production sourcing runtime. HTTP accounting is supplied request-
locally and performed by each provider client immediately before transport.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
)
from averon_import.services.sourcing.providers.etm_ipro.provider import EtmIproProvider
from averon_import.services.sourcing.providers.execution import ProviderRequestCounter
from averon_import.services.sourcing.providers.lemana_b2b.provider import LemanaB2BProvider
from averon_import.services.sourcing.providers.local_catalog import LocalCatalogProvider


_LOCAL_ADAPTER_REVISION = "local_catalog_execution_v1"
_LEMANA_ADAPTER_REVISION = "lemana_b2b_execution_v1"
_ETM_ADAPTER_REVISION = "etm_ipro_execution_v1"


def _opaque_revision(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identity(
    *,
    provider_key: str,
    intent: ProductIntent,
    limit: int,
    execution_scope_id: str,
    affinity: ProviderAffinity,
) -> ProviderSearchRequestIdentity:
    fingerprint = _opaque_revision(
        {
            "provider_key": provider_key,
            "intent_fingerprint": intent.fingerprint,
            "limit": limit,
            "affinity": affinity.model_dump(mode="json"),
        }
    )
    return ProviderSearchRequestIdentity(
        execution_scope_id=execution_scope_id,
        provider_key=provider_key,
        request_fingerprint=fingerprint,
        affinity=affinity,
        limit=limit,
    )


def failure_category_for_sourcing_error(error: SourcingProviderError) -> ProviderFailureCategory:
    """Map existing typed provider categories to bounded execution diagnostics.

    Authentication, throttling, timeout, transport, and invalid-response
    categories retain their specific meaning. Local configuration/input
    failures are MISCONFIGURED; upstream and local-storage availability
    failures are UNAVAILABLE. Unknown categories remain UNKNOWN. The public
    message and exception text are never copied into the outcome.
    """

    category = str(error.category or "").strip().casefold()
    mapping = {
        "auth": ProviderFailureCategory.AUTHENTICATION,
        "authentication": ProviderFailureCategory.AUTHENTICATION,
        "rate_limit": ProviderFailureCategory.RATE_LIMITED,
        "rate_limited": ProviderFailureCategory.RATE_LIMITED,
        "timeout": ProviderFailureCategory.TIMEOUT,
        "network": ProviderFailureCategory.TRANSPORT,
        "transport": ProviderFailureCategory.TRANSPORT,
        "invalid_response": ProviderFailureCategory.INVALID_RESPONSE,
        "not_configured": ProviderFailureCategory.MISCONFIGURED,
        "invalid_request": ProviderFailureCategory.MISCONFIGURED,
        "health_error": ProviderFailureCategory.MISCONFIGURED,
        "upstream_error": ProviderFailureCategory.UNAVAILABLE,
        "storage_error": ProviderFailureCategory.UNAVAILABLE,
    }
    return mapping.get(category, ProviderFailureCategory.UNKNOWN)


def _outcome(
    *,
    provider_key: str,
    offers: list[Offer],
    request_counter: ProviderRequestCounter,
    affinity: ProviderAffinity,
    catalog_version: str,
) -> ProviderSearchOutcome:
    return ProviderSearchOutcome(
        provider_key=provider_key,
        state=ProviderSearchState.SUCCESS if offers else ProviderSearchState.EMPTY,
        offers=tuple(offers),
        request_count=request_counter.request_count,
        affinity=affinity,
        catalog_version=catalog_version,
    )


def _failure_outcome(
    *,
    provider_key: str,
    request_counter: ProviderRequestCounter,
    affinity: ProviderAffinity,
    catalog_version: str,
    category: ProviderFailureCategory,
) -> ProviderSearchOutcome:
    return ProviderSearchOutcome(
        provider_key=provider_key,
        state=ProviderSearchState.FAILURE,
        request_count=request_counter.request_count,
        failure_category=category,
        affinity=affinity,
        catalog_version=catalog_version,
    )


class LocalCatalogExecutionAdapter:
    """Outcome wrapper over local catalogue search; never probes provider stats."""

    key = LocalCatalogProvider.key

    def __init__(self, provider: LocalCatalogProvider) -> None:
        self.provider = provider

    def _affinity(self) -> ProviderAffinity:
        return ProviderAffinity(
            config_revision=str(self.provider.repository.catalog_version),
            adapter_revision=_LOCAL_ADAPTER_REVISION,
        )

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        affinity = self._affinity()
        return _identity(
            provider_key=self.key,
            intent=intent,
            limit=limit,
            execution_scope_id=execution_scope_id,
            affinity=affinity,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        affinity = self._affinity()
        try:
            offers = self.provider.search(intent, limit=limit)
        except SourcingProviderError as exc:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=affinity.config_revision,
                category=failure_category_for_sourcing_error(exc),
            )
        except Exception:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=affinity.config_revision,
                category=ProviderFailureCategory.UNKNOWN,
            )
        return _outcome(
            provider_key=self.key,
            offers=offers,
            request_counter=request_counter,
            affinity=affinity,
            catalog_version=affinity.config_revision,
        )


class LemanaB2BExecutionAdapter:
    """Inactive outcome wrapper over Lemana mirror and live price enrichment."""

    key = LemanaB2BProvider.key

    def __init__(self, provider: LemanaB2BProvider) -> None:
        self.provider = provider

    def _affinity(self) -> ProviderAffinity:
        settings = self.provider.settings
        mirror_revision = str(self.provider.mirror.revision)
        return ProviderAffinity(
            environment=str(settings.environment),
            region_id="" if settings.region_id is None else str(settings.region_id),
            config_revision=_opaque_revision(
                {
                    "mirror_revision": mirror_revision,
                    "enabled": bool(settings.enabled),
                    "client_configured": bool(self.provider.client.configured),
                    "request_timeout_s": settings.request_timeout_s,
                }
            ),
            adapter_revision=_LEMANA_ADAPTER_REVISION,
        )

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        affinity = self._affinity()
        return _identity(
            provider_key=self.key,
            intent=intent,
            limit=limit,
            execution_scope_id=execution_scope_id,
            affinity=affinity,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        affinity = self._affinity()
        try:
            offers = self.provider.search_with_observer(
                intent,
                limit=limit,
                outbound_attempt_observer=request_counter,
            )
        except SourcingProviderError as exc:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=str(self.provider.mirror.revision),
                category=failure_category_for_sourcing_error(exc),
            )
        except Exception:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=str(self.provider.mirror.revision),
                category=ProviderFailureCategory.UNKNOWN,
            )
        return _outcome(
            provider_key=self.key,
            offers=offers,
            request_counter=request_counter,
            affinity=affinity,
            catalog_version=str(self.provider.mirror.revision),
        )


class EtmIproExecutionAdapter:
    """Inactive outcome wrapper over the ETM mirror-first execution path."""

    key = EtmIproProvider.key

    def __init__(self, provider: EtmIproProvider) -> None:
        self.provider = provider

    def _affinity(self) -> ProviderAffinity:
        settings = self.provider.settings
        mirror_revision = str(self.provider.mirror.revision)
        config_revision = _opaque_revision(
            {
                "mirror_revision": mirror_revision,
                "search_index_revision": str(self.provider.mirror.search_index_revision),
                "warehouse_codes": list(settings.warehouse_codes),
                "max_live_candidates": settings.max_live_candidates,
                "base_url_override": settings.base_url_override,
                "enabled": bool(settings.enabled),
                "client_configured": bool(self.provider.client.configured),
                "request_timeout_s": settings.request_timeout_s,
            }
        )
        return ProviderAffinity(
            environment=str(settings.environment),
            config_revision=config_revision,
            adapter_revision=_ETM_ADAPTER_REVISION,
        )

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        affinity = self._affinity()
        return _identity(
            provider_key=self.key,
            intent=intent,
            limit=limit,
            execution_scope_id=execution_scope_id,
            affinity=affinity,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        affinity = self._affinity()
        try:
            offers = self.provider.search_with_observer(
                intent,
                limit=limit,
                outbound_attempt_observer=request_counter,
            )
        except SourcingProviderError as exc:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=str(self.provider.mirror.revision),
                category=failure_category_for_sourcing_error(exc),
            )
        except Exception:
            return _failure_outcome(
                provider_key=self.key,
                request_counter=request_counter,
                affinity=affinity,
                catalog_version=str(self.provider.mirror.revision),
                category=ProviderFailureCategory.UNKNOWN,
            )
        return _outcome(
            provider_key=self.key,
            offers=offers,
            request_counter=request_counter,
            affinity=affinity,
            catalog_version=str(self.provider.mirror.revision),
        )
