from __future__ import annotations

from typing import Any

from averon_import.ai.models import AiReviewSuggestion


class YandexAiProvider:
    """Yandex Cloud AI adapter.

    This adapter intentionally stays behind the AiProvider contract.
    Network calls are added after credentials/config are wired.
    """

    def __init__(self, api_key: str | None = None, folder_id: str | None = None):
        self.api_key = api_key
        self.folder_id = folder_id

    def health(self) -> dict[str, Any]:
        return {
            "provider": "yandex",
            "enabled": bool(self.api_key and self.folder_id),
        }

    def review_row(
        self,
        row: dict[str, Any],
        *,
        image_bytes: bytes | None = None,
    ) -> AiReviewSuggestion:
        raise RuntimeError(
            "Yandex AI provider is configured but API transport is not enabled yet."
        )
