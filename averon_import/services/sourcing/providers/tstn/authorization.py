"""Request-local TSTN authorization backed by the application SecretStore."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any, Protocol

from pydantic import SecretStr

from averon_import.services.secrets import (
    TSTN_CLIENT_KEY,
    TSTN_CREDENTIALS_CONFIGURED,
    TSTN_SERVICE_IDENTIFIER,
)

from .client import TstnApiError, TstnApiClient, TstnErrorCategory
from .models import MAX_TSTN_CREDENTIAL_LENGTH, TstnAuthorizationResponse


TSTN_CREDENTIAL_LOCK = RLock()


class TstnAuthorizationClient(Protocol):
    """Typed authorization method provided by the TN1 API client."""

    def authorize(
        self,
        *,
        service: str | SecretStr,
        key: str | SecretStr,
    ) -> TstnAuthorizationResponse: ...


def _is_credential(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_TSTN_CREDENTIAL_LENGTH
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


class SecretStoreTstnAuthorizer:
    """One-shot authorizer; callers create one instance for one explicit job.

    Construction is passive. Credentials are read only by ``authorize()``,
    which serializes against credential rotation and performs at most one
    documented ``POST /user/`` through the injected, bounded TN1 client.
    The access token is returned as ``SecretStr`` and is never retained here.
    """

    def __init__(
        self,
        secret_store: Any,
        client: TstnAuthorizationClient,
        *,
        enabled: bool,
        credential_generation: str,
        current_configuration: Callable[[], tuple[bool, str]],
        credential_lock: Any = TSTN_CREDENTIAL_LOCK,
    ) -> None:
        self._secret_store = secret_store
        self._client = client
        self._enabled = bool(enabled)
        self._credential_generation = credential_generation
        self._current_configuration = current_configuration
        self._credential_lock = credential_lock
        self._attempted = False

    def __repr__(self) -> str:
        return "SecretStoreTstnAuthorizer(credentials=<deferred>, access_token=<not-cached>)"

    def authorize(self) -> SecretStr:
        with self._credential_lock:
            if self._attempted:
                raise TstnApiError(TstnErrorCategory.REQUEST_LIMIT)
            self._attempted = True
            try:
                current_enabled, current_generation = self._current_configuration()
                if (
                    not self._enabled
                    or not current_enabled
                    or current_generation != self._credential_generation
                    or self._secret_store.get(TSTN_CREDENTIALS_CONFIGURED)
                    != self._credential_generation
                ):
                    raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
                service = self._secret_store.get(TSTN_SERVICE_IDENTIFIER)
                key = self._secret_store.get(TSTN_CLIENT_KEY)
            except TstnApiError:
                raise
            except Exception:
                raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None

            if not _is_credential(service) or not _is_credential(key):
                raise TstnApiError(TstnErrorCategory.MISCONFIGURED)

            try:
                response = self._client.authorize(
                    service=SecretStr(service),
                    key=SecretStr(key),
                )
            except TstnApiError:
                raise
            except Exception:
                # Injected transports must not be able to expose query values.
                raise TstnApiError(TstnErrorCategory.TRANSPORT) from None

            try:
                current_enabled, current_generation = self._current_configuration()
                marker = self._secret_store.get(TSTN_CREDENTIALS_CONFIGURED)
            except Exception:
                raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None
            if (
                not isinstance(response, TstnAuthorizationResponse)
                or not current_enabled
                or current_generation != self._credential_generation
                or marker != self._credential_generation
            ):
                raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
            return response.access_token


def create_tstn_authorization_client(
    timeout_seconds: float,
    *,
    max_attempts: int = 64,
    outbound_attempt_observer: Callable[[], None] | None = None,
) -> TstnApiClient:
    """Build one bounded TN1 client, optionally sharing an attempt budget."""

    return TstnApiClient(
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        outbound_attempt_observer=outbound_attempt_observer,
    )
