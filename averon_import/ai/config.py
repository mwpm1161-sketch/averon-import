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
    yandex_api_key: str | None = None
    yandex_endpoint: str = ""
    request_timeout: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            enabled=os.getenv("AVERON_AI_ENABLED", "false").lower() == "true",
            cloud_allowed=os.getenv("AVERON_AI_CLOUD_ALLOWED", "false").lower() == "true",
            provider=os.getenv("AVERON_AI_PROVIDER", "none"),
            yandex_model=os.getenv("AVERON_YANDEX_MODEL", "qwen"),
            ollama_model=os.getenv("AVERON_OLLAMA_MODEL", "qwen"),
            yandex_api_key=os.getenv("AVERON_YANDEX_API_KEY"),
            yandex_endpoint=os.getenv("AVERON_YANDEX_ENDPOINT", ""),
            request_timeout=float(os.getenv("AVERON_AI_TIMEOUT", "30")),
            max_retries=int(os.getenv("AVERON_AI_RETRIES", "3")),
        )
