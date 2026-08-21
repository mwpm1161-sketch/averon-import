from dataclasses import dataclass, field
from typing import Any


@dataclass
class AIReviewResult:
    """Non-destructive AI suggestion result.

    AI never mutates specification data directly. The user decides whether
    the suggested changes are accepted.
    """

    changed_fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    provider: str = ""
    accepted: bool = False


@dataclass
class AIRequestContext:
    """Context passed to providers without exposing document internals."""

    row_id: str
    fields: dict[str, Any]
