from __future__ import annotations

from typing import Any

from averon_import.ai.base import AiProvider
from averon_import.ai.models import AiReviewSuggestion


class AiUnavailableError(RuntimeError):
    pass


class AiService:
    """Application-facing AI facade; callers never depend on Ollama directly."""

    def __init__(self, provider: AiProvider | None = None, *, enabled: bool = False):
        self.provider = provider
        self.enabled = enabled

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "available": False, "status": "disabled"}
        if self.provider is None:
            return {"enabled": True, "available": False, "status": "not_configured"}
        try:
            return {"enabled": True, **self.provider.health()}
        except Exception as exc:
            return {
                "enabled": True,
                "available": False,
                "status": "unavailable",
                "error": str(exc),
            }

    def review_row(
        self,
        row: dict[str, Any],
        *,
        image_bytes: bytes | None = None,
    ) -> AiReviewSuggestion:
        if not self.enabled or self.provider is None:
            raise AiUnavailableError("Локальный AI не включён или не настроен")
        return self.provider.review_row(row, image_bytes=image_bytes)
