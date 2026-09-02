from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from averon_import.ai.pipeline import AIPipeline, AIPipelineResult
from averon_import.ai.router import AIRouter
from averon_import.ai.service import AiCorrectionService, ProgressCallback
from averon_import.ai.settings import AIPipelineSettings


@dataclass
class SmartAIIntegration:
    """Application-facing adapter for Smart AI Pipeline.

    Keeps the OCR layer independent from the selected AI provider.
    The caller only passes recognition output and receives a processed result.
    Routing behaviour is driven by AIPipelineSettings (environment variables).
    """

    service: AiCorrectionService
    settings: AIPipelineSettings = field(default_factory=AIPipelineSettings)

    def process(
        self,
        result: dict[str, Any],
        provider_key: str,
        progress: ProgressCallback | None = None,
    ) -> AIPipelineResult:
        pipeline = AIPipeline(
            self.service,
            router=AIRouter(min_confidence=self.settings.min_confidence),
            settings=self.settings,
        )
        return pipeline.run(result, provider_key, progress)


__all__ = ["SmartAIIntegration"]
