"""Offline-testable client for the documented TSTN API 1.0.0."""

from .client import (
    TSTN_API_BASE_URL,
    TSTN_MAX_HTTP_ATTEMPTS,
    TSTN_MAX_RESPONSE_BYTES,
    TstnApiClient,
    TstnApiError,
    TstnErrorCategory,
    TstnHttpResponse,
)
from .models import (
    TstnAuthorizationRequest,
    TstnAuthorizationResponse,
    TstnAvailableItem,
    TstnAvailableItemsResponse,
    TstnCategoriesResponse,
    TstnCategoryDetail,
    TstnCity,
    TstnItemDetail,
    TstnItemListResponse,
    TstnItemCategory,
    TstnItemPrice,
    TstnPricesResponse,
)

__all__ = [
    "TSTN_API_BASE_URL",
    "TSTN_MAX_HTTP_ATTEMPTS",
    "TSTN_MAX_RESPONSE_BYTES",
    "TstnApiClient",
    "TstnApiError",
    "TstnErrorCategory",
    "TstnHttpResponse",
    "TstnAuthorizationRequest",
    "TstnAuthorizationResponse",
    "TstnAvailableItem",
    "TstnAvailableItemsResponse",
    "TstnCategoriesResponse",
    "TstnCategoryDetail",
    "TstnCity",
    "TstnItemDetail",
    "TstnItemListResponse",
    "TstnItemCategory",
    "TstnItemPrice",
    "TstnPricesResponse",
]
