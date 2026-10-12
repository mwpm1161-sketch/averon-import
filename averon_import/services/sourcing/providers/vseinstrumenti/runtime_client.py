"""SecretStore-backed VI client that resolves credentials only on execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from averon_import.services.secrets import (
    VSEINSTRUMENTI_BEARER_TOKEN,
    VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED,
)

from .client import (
    VseinstrumentiApiError,
    VseinstrumentiClient,
    VseinstrumentiErrorCategory,
    VseinstrumentiRateLimiter,
    is_valid_bearer_token,
    shared_rate_limiter,
)
from .models import VseinstrumentiProductSearchResult


class SecretBackedVseinstrumentiClient:
    """Resolve a token for an explicit search, never during runtime composition."""

    def __init__(
        self,
        secret_store: Any,
        *,
        environment: str,
        timeout_seconds: float,
        credential_generation: str,
        current_generation: Callable[[], str],
        credential_lock: Any,
        rate_limiter: VseinstrumentiRateLimiter | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._credential_generation = credential_generation
        self._current_generation = current_generation
        self._credential_lock = credential_lock
        self.environment = environment
        self.timeout_seconds = float(timeout_seconds)
        self.rate_limiter = rate_limiter or shared_rate_limiter()

    def __repr__(self) -> str:
        return (
            "SecretBackedVseinstrumentiClient("
            f"environment={self.environment!r}, timeout_seconds={self.timeout_seconds!r}, "
            "bearer_token=<deferred>)"
        )

    def search_products(
        self,
        search: str,
        *,
        region_id: str,
        limit: int,
        sort: str = "asc",
        order_by: str = "price",
        outbound_attempt_observer: Any = None,
    ) -> VseinstrumentiProductSearchResult:
        # Admin token replacement persists the new generation before storing
        # the new credential. The same lock makes the generation check and
        # SecretStore read one coherent snapshot for an in-flight search.
        with self._credential_lock:
            try:
                if self._current_generation() != self._credential_generation:
                    raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED)
                if self._secret_store.get(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED) != self._credential_generation:
                    raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED)
                token = self._secret_store.get(VSEINSTRUMENTI_BEARER_TOKEN)
            except VseinstrumentiApiError:
                raise
            except Exception:
                raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None

        if not is_valid_bearer_token(token):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        client = VseinstrumentiClient(
            token,
            environment=self.environment,
            timeout_seconds=self.timeout_seconds,
            rate_limiter=self.rate_limiter,
        )
        return client.search_products(
            search,
            region_id=region_id,
            limit=limit,
            sort=sort,
            order_by=order_by,
            outbound_attempt_observer=outbound_attempt_observer,
        )
