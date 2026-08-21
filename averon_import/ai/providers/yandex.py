from __future__ import annotations

from typing import Any

from averon_import.ai.base import AiProvider
from averon_import.ai.models import AiReviewSuggestion


class YandexAiProvider(AiProvider):
    """Yandex Cloud AI adapter.

    This first implementation intentionally keeps transport isolated. Real API
    calls will be enabled after credentials and cloud policy are configured.
    """

    def __init__(self, *, api_key: str | None = None, folder_id: str | None = None):
        self.api_key = api_key
        self.folder_id = folder_id

    def health(self) -> dict[str, Any]:
        return {
            "provider": "yandex",
            "configured": bool(self.api_key and self.folder_id),
        }

    def review_row(
        self,
        row: dict[str, Any],
        *,
        image_bytes: bytes | None = None,
    ) -> AiReviewSuggestion:
        return AiReviewSuggestion(
            changed=False,
            confidence=0.0,
            reason="Yandex provider configured but API transport is not enabled yet",
            fields={},
        )
