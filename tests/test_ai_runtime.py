import pytest

from averon_import.ai.config import AIConfig
from averon_import.ai.router import AIRouter
from averon_import.ai.providers.mock_provider import MockAIProvider


@pytest.mark.asyncio
async def test_router_disabled():
    router = AIRouter(AIConfig(enabled=False), {"mock": MockAIProvider()})
    assert router.select_provider() is None


@pytest.mark.asyncio
async def test_router_mock():
    router = AIRouter(AIConfig(enabled=True, provider="mock"), {"mock": MockAIProvider()})
    assert router.select_provider() is not None
