from .config import AIConfig
from .providers.base import AIProvider


class AIRouter:
    def __init__(self, config: AIConfig, providers: dict[str, AIProvider]):
        self.config = config
        self.providers = providers

    def select_provider(self) -> AIProvider | None:
        if not self.config.enabled:
            return None

        return self.providers.get(self.config.provider)

    def require_provider(self) -> AIProvider:
        provider = self.select_provider()
        if provider is None:
            raise RuntimeError("AI provider is not available")
        return provider
