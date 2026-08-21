from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSettings:
    key: str
    label: str
    base_url: str
    model: str
    api_key: str = ""
    auth_schemes: tuple[str, ...] = ("Bearer",)

    @property
    def configured(self) -> bool:
        if self.key == "yandex":
            return bool(self.base_url and self.model and self.api_key)
        return bool(self.base_url and self.model)

    def public(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "configured": self.configured,
            "base_url": self.base_url,
            "model": self.model,
        }


@dataclass(frozen=True)
class AiSettings:
    local: ProviderSettings
    yandex: ProviderSettings
    timeout_seconds: float = 120.0
    batch_size: int = 10
    temperature: float = 0.0
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "AiSettings":
        timeout = _float_env("AVERON_AI_TIMEOUT", 120.0, minimum=5.0, maximum=600.0)
        batch_size = _int_env("AVERON_AI_BATCH_SIZE", 10, minimum=1, maximum=30)
        temperature = _float_env("AVERON_AI_TEMPERATURE", 0.0, minimum=0.0, maximum=1.0)
        max_tokens = _int_env("AVERON_AI_MAX_TOKENS", 4096, minimum=512, maximum=32768)

        local = ProviderSettings(
            key="local",
            label="Локальная модель",
            base_url=os.environ.get(
                "AVERON_LOCAL_AI_BASE_URL", "http://127.0.0.1:11434/v1"
            ).strip().rstrip("/"),
            model=os.environ.get("AVERON_LOCAL_AI_MODEL", "qwen3:8b").strip(),
            api_key=os.environ.get("AVERON_LOCAL_AI_API_KEY", "").strip(),
            auth_schemes=("Bearer",),
        )
        yandex = ProviderSettings(
            key="yandex",
            label="Yandex Cloud AI Studio",
            base_url=os.environ.get(
                "AVERON_YANDEX_AI_BASE_URL", "https://ai.api.cloud.yandex.net/v1"
            ).strip().rstrip("/"),
            model=os.environ.get("AVERON_YANDEX_AI_MODEL", "").strip(),
            api_key=os.environ.get("AVERON_YANDEX_AI_API_KEY", "").strip(),
            # OpenAI-compatible clients use Bearer. Some Yandex API-key setups
            # accept the documented Api-Key scheme instead, so the provider may
            # retry authentication without changing application code.
            auth_schemes=("Bearer", "Api-Key"),
        )
        return cls(
            local=local,
            yandex=yandex,
            timeout_seconds=timeout,
            batch_size=batch_size,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def provider(self, key: str) -> ProviderSettings:
        if key == "local":
            return self.local
        if key == "yandex":
            return self.yandex
        raise KeyError(key)

    def public(self) -> dict:
        return {
            "providers": {
                "off": {
                    "key": "off",
                    "label": "Без ИИ",
                    "configured": True,
                    "base_url": "",
                    "model": "",
                },
                "local": self.local.public(),
                "yandex": self.yandex.public(),
            },
            "batch_size": self.batch_size,
        }


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
