from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from averon_import.ai.pipeline import AIPipeline, AIPipelineResult
from averon_import.ai.service import AiCorrectionService


@dataclass
class SmartAIIntegration:
    """Application-facing adapter for Smart AI Pipeline.

    Keeps the OCR layer independent from the selected AI provider.
    The caller only passes recognition output and receives a processed result.
    """

    service: AiCorrectionService

    def process(
        self,
        result: dict[str, Any],
        provider_key: str,
    ) -> AIPipelineResult:
        pipeline = AIPipeline(self.service)
        return pipeline.run(result, provider_key)


__all__ = ["SmartAIIntegration"]
