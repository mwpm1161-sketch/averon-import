from __future__ import annotations

"""Provider selection logic for Averon AI workflows.

The router keeps business services independent from concrete AI vendors.
"""

from dataclasses import dataclass

from averon_import.ai.providers.base import AIProvider


@dataclass(slots=True)
class AIRouter:
    """Selects an AI provider according to configured policy."""

    local_provider: AIProvider | None = None
    cloud_provider: AIProvider | None = None

    def choose(self, risk: str) -> AIProvider | None:
        """Return provider for a recognized risk level.

        Risk values are intentionally simple at this layer. The logic that
        calculates risk belongs to document/specification analysis.
        """
        if risk == "high":
            return self.cloud_provider or self.local_provider

        if risk == "medium":
            return self.local_provider or self.cloud_provider

        return None
