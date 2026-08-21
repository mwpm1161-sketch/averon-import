import asyncio

import httpx

from ..schemas import AIRequestContext, AIReviewResult
from .base import AIProvider


class YandexQwenProvider(AIProvider):
    name = "yandex_qwen"

    def __init__(self, endpoint: str, api_key: str, timeout: float = 30.0, retries: int = 3):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries

    async def review(self, context: AIRequestContext) -> AIReviewResult:
        if not self.endpoint:
            raise RuntimeError("Yandex Qwen endpoint is not configured")

        payload = {
            "input": context.model_dump() if hasattr(context, "model_dump") else context.__dict__,
        }

        error = None
        for attempt in range(self.retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        self.endpoint,
                        headers={"Authorization": f"Api-Key {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    return self._parse_response(response.json())
            except Exception as exc:
                error = exc
                if attempt < self.retries - 1:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError("Yandex Qwen request failed") from error

    def _parse_response(self, data: dict) -> AIReviewResult:
        return AIReviewResult(
            changed_fields=data.get("changed_fields", {}),
            confidence=float(data.get("confidence", 0)),
            reason=data.get("reason", ""),
            provider=self.name,
            accepted=False,
        )
