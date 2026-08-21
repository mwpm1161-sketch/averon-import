from .config import AIConfig
from .providers.base import AIProvider


class AIRouter:
    def __init__(self, config: AIConfig, providers: dict[str, AIProvider]):
        self.config = config
        self.providers = providers

    def select_provider(self) -> AIProvider | None:
        if not self.config.enabled:
            return None

        if self.config.provider in self.providers:
            return self.providers[self.config.provider]

        return None
