from dataclasses import dataclass


@dataclass
class AIRouteDecision:
    action: str
    reason: str


class AIRouter:
    """Decides whether OCR output needs AI processing.

    Conservative by design: good OCR is not sent to the model.
    """

    def __init__(self, min_confidence: float = 0.95):
        self.min_confidence = min_confidence

    def decide(self, confidence: float | None, has_rule_candidate: bool = False) -> AIRouteDecision:
        if confidence is not None and confidence >= self.min_confidence:
            return AIRouteDecision("skip", "high OCR confidence")
        if has_rule_candidate:
            return AIRouteDecision("rule", "deterministic OCR correction available")
        return AIRouteDecision("ai", "requires semantic correction")
