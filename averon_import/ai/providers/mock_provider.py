from ..schemas import AIReviewResult


class MockAIProvider:
    name = "mock"

    async def review(self, context):
        return AIReviewResult(
            changed_fields={},
            confidence=1.0,
            reason="Mock provider",
            provider=self.name,
            accepted=False,
        )
