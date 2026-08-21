from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class AIConfig:
    """Runtime AI policy.

    Cloud usage is opt-in. Averon must remain fully functional without any
    external AI provider configured.
    """

    enabled: bool = False
    mode: str = "off"
    allow_cloud: bool = False
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = ""
    yandex_model: str = ""
    yandex_folder_id: str = ""
    yandex_api_key: str = ""

    @classmethod
    def from_environment(cls) -> "AIConfig":
        return cls(
            enabled=os.getenv("AVERON_AI_ENABLED", "false").lower() == "true",
            mode=os.getenv("AVERON_AI_MODE", "off"),
            allow_cloud=os.getenv("ALLOW_CLOUD_AI", "false").lower() == "true",
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", ""),
            yandex_model=os.getenv("YANDEX_MODEL", ""),
            yandex_folder_id=os.getenv("YANDEX_FOLDER_ID", ""),
            yandex_api_key=os.getenv("YANDEX_API_KEY", ""),
        )
