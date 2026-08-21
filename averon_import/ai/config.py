from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AiConfig:
    mode: str = "off"
    allow_cloud: bool = False
    yandex_api_key: str | None = None
    yandex_folder_id: str | None = None

    @classmethod
    def from_env(cls) -> "AiConfig":
        return cls(
            mode=os.getenv("AVERON_AI_MODE", "off"),
            allow_cloud=os.getenv("ALLOW_CLOUD_AI", "false").lower() == "true",
            yandex_api_key=os.getenv("YANDEX_API_KEY"),
            yandex_folder_id=os.getenv("YANDEX_FOLDER_ID"),
        )
