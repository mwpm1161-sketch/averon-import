from __future__ import annotations

from typing import Protocol

from averon_import.services.sourcing.models import Offer, ProductIntent


class SourcingProvider(Protocol):
    key: str
    label: str

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        """Return provider-owned commercial offers for an intent."""
        ...

    def stats(self) -> dict:
        ...
