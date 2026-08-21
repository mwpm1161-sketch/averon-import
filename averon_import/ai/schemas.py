from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AIReviewResult:
    """Safe AI suggestion container.

    Suggestions never mutate specification data automatically. The operator
    remains the final approval point.
    """

    changed_fields: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    provider: str = "none"
    accepted: bool = False
