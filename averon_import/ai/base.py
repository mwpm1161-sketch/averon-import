from __future__ import annotations

from typing import Any, Protocol

from averon_import.ai.models import AiReviewSuggestion


class AiProvider(Protocol):
    def health(self) -> dict[str, Any]:
        ...

    def review_row(
        self,
        row: dict[str, Any],
        *,
        image_bytes: bytes | None = None,
    ) -> AiReviewSuggestion:
        ...
