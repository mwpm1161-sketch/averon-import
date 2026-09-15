from __future__ import annotations

from typing import Protocol

from averon_import.services.sourcing.models import (
    Offer,
    ProductIntent,
    SourcingProviderCapabilities,
    SourcingProviderRuntimeState,
)


class SourcingProviderError(ValueError):
    """Provider operational failure with a bounded safe public message."""

    def __init__(
        self,
        public_message: str,
        *,
        code: str = "PROVIDER_ERROR",
        category: str = "provider_error",
        status_code: int | None = None,
    ) -> None:
        message = " ".join(str(public_message or "").split())
        lowered = message.casefold()
        if not message or len(message) > 240 or any(
            marker in lowered
            for marker in ("api-key", "authorization", "secret", "token", "password")
        ):
            message = "Поставщик не вернул предложения"
        self.code = str(code or "PROVIDER_ERROR")
        self.category = str(category or "provider_error")
        self.status_code = status_code
        self.public_message = message
        super().__init__(message)


class SourcingProvider(Protocol):
    key: str
    label: str
    capabilities: SourcingProviderCapabilities

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        """Return provider-owned commercial offers for an intent."""
        ...

    def stats(self) -> dict | SourcingProviderRuntimeState:
        ...


def get_provider_capabilities(provider: object) -> SourcingProviderCapabilities:
    """Return a safe capability contract for current and legacy providers."""

    try:
        capabilities = getattr(provider, "capabilities", None)
        if not isinstance(capabilities, SourcingProviderCapabilities):
            return SourcingProviderCapabilities()
        payload = {
            field_name: getattr(capabilities, field_name)
            for field_name in SourcingProviderCapabilities.model_fields
        }
        return SourcingProviderCapabilities.model_validate(payload)
    except Exception:
        return SourcingProviderCapabilities()


def _unavailable_provider_runtime_state() -> SourcingProviderRuntimeState:
    return SourcingProviderRuntimeState(
        configured=True,
        reachable=False,
        item_count=0,
        catalog_version="unavailable",
        latency_ms=None,
        error="Проверка каталога поставщика не выполнена",
    )


def normalize_provider_runtime_state(stats: object) -> SourcingProviderRuntimeState:
    """Rebuild provider runtime data from a strict whitelist, failing closed."""

    try:
        if isinstance(stats, SourcingProviderRuntimeState):
            payload = {
                field_name: getattr(stats, field_name)
                for field_name in SourcingProviderRuntimeState.model_fields
            }
        elif isinstance(stats, dict):
            payload = {
                field_name: stats[field_name]
                for field_name in SourcingProviderRuntimeState.model_fields
                if field_name in stats
            }
            if stats and not payload:
                return _unavailable_provider_runtime_state()
        else:
            return _unavailable_provider_runtime_state()
        return SourcingProviderRuntimeState.model_validate(payload)
    except Exception:
        return _unavailable_provider_runtime_state()
