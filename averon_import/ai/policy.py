from __future__ import annotations

"""AI invocation policy.

Keeps decisions about when to call models separate from providers.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AIRiskPolicy:
    high_threshold: float = 0.70
    medium_threshold: float = 0.90

    def classify(self, confidence: float) -> str:
        if confidence < self.high_threshold:
            return "high"
        if confidence < self.medium_threshold:
            return "medium"
        return "low"
