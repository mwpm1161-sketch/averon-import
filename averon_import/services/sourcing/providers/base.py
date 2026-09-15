from __future__ import annotations

from typing import Protocol

from averon_import.services.sourcing.models import Offer, ProductIntent


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

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        """Return provider-owned commercial offers for an intent."""
        ...

    def stats(self) -> dict:
        ...
