from dataclasses import dataclass
import os


@dataclass
class AIConfig:
    """Runtime AI configuration.

    Secure defaults: AI and cloud access are disabled.
    """

    enabled: bool = False
    cloud_allowed: bool = False
    provider: str = "none"
    yandex_model: str = "qwen"
    ollama_model: str = "qwen"

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            enabled=os.getenv("AVERON_AI_ENABLED", "false").lower() == "true",
            cloud_allowed=os.getenv("AVERON_AI_CLOUD_ALLOWED", "false").lower() == "true",
            provider=os.getenv("AVERON_AI_PROVIDER", "none"),
            yandex_model=os.getenv("AVERON_YANDEX_MODEL", "qwen"),
            ollama_model=os.getenv("AVERON_OLLAMA_MODEL", "qwen"),
        )
