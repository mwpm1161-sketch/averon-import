from __future__ import annotations

"""Yandex Cloud AI provider boundary.

This module intentionally contains no business logic. It adapts a future
Yandex Cloud client to Averon's AIProvider contract.
"""

from dataclasses import dataclass

from averon_import.ai.config import AIConfig
from averon_import.ai.providers.base import AIProvider
from averon_import.ai.schemas import AIReviewResult


@dataclass(slots=True)
class YandexAIProvider(AIProvider):
    """Cloud AI adapter.

    The first implementation is a safe boundary: it validates configuration
    and keeps cloud calls behind one provider. Real transport can be swapped in
    without changing recognition or review code.
    """

    config: AIConfig
    name: str = "yandex"

    def health(self) -> dict:
        return {
            "provider": self.name,
            "configured": bool(self.config.yandex_model),
            "cloud_allowed": self.config.allow_cloud,
        }

    def review(self, payload: dict) -> AIReviewResult:
        if not self.config.allow_cloud:
            return AIReviewResult(
                provider=self.name,
                status="disabled",
                message="Cloud AI is disabled by policy",
            )

        if not self.config.yandex_model:
            return AIReviewResult(
                provider=self.name,
                status="not_configured",
                message="Yandex model is not configured",
            )

        return AIReviewResult(
            provider=self.name,
            status="not_implemented",
            message="Yandex transport will be added behind this boundary",
        )
