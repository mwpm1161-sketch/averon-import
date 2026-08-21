from __future__ import annotations

import os


class AIPipelineSettings:
    """Runtime configuration for Smart AI Pipeline.

    Values are intentionally environment driven so deployment profiles can
    switch behaviour without changing code.
    """

    def __init__(self) -> None:
        self.enabled = self._bool_env("AVERON_AI_PIPELINE_ENABLED", True)
        self.rules_enabled = self._bool_env("AVERON_AI_RULES_ENABLED", True)
        self.validation_enabled = self._bool_env("AVERON_AI_VALIDATION_ENABLED", True)
        self.batch_size = self._int_env("AVERON_AI_BATCH_SIZE", 20)
        self.min_confidence = self._float_env("AVERON_AI_MIN_CONFIDENCE", 0.85)

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    @staticmethod
    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default
