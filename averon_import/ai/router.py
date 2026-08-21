from __future__ import annotations

from typing import Any


class HybridAiRouter:
    """Selects an AI provider without coupling application code to a vendor."""

    def __init__(self, local_provider=None, cloud_provider=None):
        self.local_provider = local_provider
        self.cloud_provider = cloud_provider

    def choose(self, row: dict[str, Any]):
        confidence = float(row.get("confidence") or 0)

        if confidence >= 95:
            return None

        if confidence >= 70 and self.local_provider:
            return self.local_provider

        if self.cloud_provider:
            return self.cloud_provider

        return self.local_provider
