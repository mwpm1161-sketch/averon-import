"""Averon AI orchestration layer.

Providers are intentionally isolated from OCR and business logic so local
and cloud models can be switched without rewriting the application.
"""

from .config import AIConfig
from .schemas import AIReviewResult

__all__ = ["AIConfig", "AIReviewResult"]
