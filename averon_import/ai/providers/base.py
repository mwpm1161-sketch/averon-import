from __future__ import annotations

from abc import ABC, abstractmethod

from averon_import.ai.schemas import AIReviewResult


class AIProvider(ABC):
    """Common contract for local and cloud AI providers."""

    name: str = "unknown"

    @abstractmethod
    def health(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def review(self, payload: dict) -> AIReviewResult:
        raise NotImplementedError
