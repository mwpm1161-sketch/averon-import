"""Isolated ВсеИнструменты OpenAPI client foundation.

This package is intentionally not registered in the application runtime.
"""

from .client import (
    VseinstrumentiApiError,
    VseinstrumentiClient,
    VseinstrumentiErrorCategory,
    VseinstrumentiRateLimiter,
)
from .models import (
    VseinstrumentiProduct,
    VseinstrumentiProductSearchResult,
    VseinstrumentiTechnicalSpecification,
    parse_product_search_result,
)

__all__ = [
    "VseinstrumentiApiError",
    "VseinstrumentiClient",
    "VseinstrumentiErrorCategory",
    "VseinstrumentiProduct",
    "VseinstrumentiProductSearchResult",
    "VseinstrumentiRateLimiter",
    "VseinstrumentiTechnicalSpecification",
    "parse_product_search_result",
]
