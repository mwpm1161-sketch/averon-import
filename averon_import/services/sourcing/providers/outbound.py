from __future__ import annotations

from typing import Protocol


class OutboundAttemptObserver(Protocol):
    """Request-local sink notified immediately before an HTTP transport call."""

    def record_outbound_attempt(self) -> None:
        ...
