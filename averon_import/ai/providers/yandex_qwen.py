from ..schemas import AIRequestContext, AIReviewResult
from .base import AIProvider


class YandexQwenProvider(AIProvider):
    name = "yandex_qwen"

    def __init__(self, endpoint: str, api_key: str, timeout: float = 30.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    async def review(self, context: AIRequestContext) -> AIReviewResult:
        raise NotImplementedError("Yandex Cloud transport will be implemented next")
