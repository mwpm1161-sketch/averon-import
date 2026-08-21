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

    Transport is isolated here so cloud integration does not leak into OCR or
    recognition code.
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
                reason="Cloud AI is disabled by policy",
            )

        if not self.config.yandex_model:
            return AIReviewResult(
                provider=self.name,
                reason="Yandex model is not configured",
            )

        return AIReviewResult(
            provider=self.name,
            reason="Yandex transport placeholder; API client will be added next",
        )
