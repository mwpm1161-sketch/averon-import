from .router import AIRouter
from .schemas import AIRequestContext, AIReviewResult


class AIReviewService:
    def __init__(self, router: AIRouter):
        self.router = router

    async def review(self, context: AIRequestContext) -> AIReviewResult:
        provider = self.router.select_provider()
        if provider is None:
            return AIReviewResult(
                changed_fields={},
                confidence=0.0,
                reason="AI disabled",
                provider="none",
                accepted=False,
            )

        return await provider.review(context)
