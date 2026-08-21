from abc import ABC, abstractmethod

from ..schemas import AIRequestContext, AIReviewResult


class AIProvider(ABC):
    """Common interface for local and cloud AI providers."""

    name: str = "unknown"

    @abstractmethod
    async def review(self, context: AIRequestContext) -> AIReviewResult:
        """Return suggestions without changing source data."""
        raise NotImplementedError
