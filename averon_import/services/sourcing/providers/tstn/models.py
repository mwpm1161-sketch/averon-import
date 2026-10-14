"""Typed, bounded models for the documented TSTN Swagger 2.0 contract.

The API specification intentionally leaves many response properties optional.
This module preserves absent fields as ``None`` and does not infer commercial
meaning for prices, units, availability, or dates.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    StrictStr,
    field_validator,
)


MAX_TSTN_TEXT_LENGTH = 4096
MAX_TSTN_CREDENTIAL_LENGTH = 8192
MAX_TSTN_COLLECTION_ITEMS = 10_000
MAX_TSTN_PRICE_TEXT_LENGTH = 120
MAX_TSTN_PAGE_LIMIT = 100
MAX_TSTN_OFFSET = 1_000_000

TstnText = Annotated[StrictStr, Field(max_length=MAX_TSTN_TEXT_LENGTH)]
TstnCount = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
TstnPageLimit = Annotated[StrictInt, Field(ge=1, le=MAX_TSTN_PAGE_LIMIT)]
TstnOffset = Annotated[StrictInt, Field(ge=0, le=MAX_TSTN_OFFSET)]

_DECIMAL_TEXT_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TstnModel(BaseModel):
    """Frozen model that tolerates undocumented response extensions."""

    model_config = ConfigDict(extra="ignore", frozen=True)


def _secret(value: object) -> SecretStr:
    if isinstance(value, SecretStr):
        raw = value.get_secret_value()
    elif isinstance(value, str):
        raw = value
    else:
        raise ValueError("credential must be text")
    if (not raw or len(raw) > MAX_TSTN_CREDENTIAL_LENGTH
            or raw != raw.strip() or _CONTROL_RE.search(raw)):
        raise ValueError("credential is outside its allowed bounds")
    return SecretStr(raw)


class TstnRequestModel(TstnModel):
    """Request objects reject every field absent from Swagger."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TstnAuthorizationRequest(TstnRequestModel):
    """POST /user/ query values; SecretStr keeps repr and serialization safe."""

    service: SecretStr
    key: SecretStr

    @field_validator("service", "key", mode="before")
    @classmethod
    def _validate_secret(cls, value: object) -> SecretStr:
        return _secret(value)


class TstnAccessRequest(TstnRequestModel):
    """Required access_token query value for every documented GET operation."""

    access_token: SecretStr

    @field_validator("access_token", mode="before")
    @classmethod
    def _validate_secret(cls, value: object) -> SecretStr:
        return _secret(value)


class TstnSectionDetailRequest(TstnAccessRequest):
    section_id: StrictInt


class TstnItemsRequest(TstnAccessRequest):
    category: StrictInt | None = None
    limit: TstnPageLimit | None = None
    offset: TstnOffset | None = None


class TstnItemDetailRequest(TstnAccessRequest):
    # The path parameter is a string in Swagger because it can be an ID or EKN.
    item_id: StrictStr | StrictInt

    @field_validator("item_id")
    @classmethod
    def _validate_item_id(cls, value: str | int) -> str | int:
        if isinstance(value, str) and (not value or len(value) > 1024 or _CONTROL_RE.search(value)):
            raise ValueError("item id is outside its allowed bounds")
        return value


class TstnCatalogFilterRequest(TstnAccessRequest):
    category: StrictInt | None = None
    id: TstnText | None = None
    article: TstnText | None = None
    city: TstnText | None = None


class TstnAuthorizationResponse(TstnModel):
    access_token: SecretStr
    access_date: TstnText | None = None

    @field_validator("access_token", mode="before")
    @classmethod
    def _validate_access_token(cls, value: object) -> SecretStr:
        return _secret(value)


class TstnCity(TstnModel):
    name: TstnText | None = None
    code: TstnText | None = None


class TstnItemCategory(TstnModel):
    id: StrictInt
    name: TstnText
    parent: StrictInt | None = None


class TstnCategoryDetail(TstnModel):
    id: StrictInt | None = None
    name: TstnText | None = None
    parent: StrictInt | None = None
    level: StrictInt | None = None
    element_count: StrictInt | None = None
    categories: tuple[TstnCategoryDetail, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


class TstnCategoriesResponse(TstnModel):
    count: TstnCount | None = None
    results: tuple[TstnCategoryDetail, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


class TstnItemDetail(TstnModel):
    id: StrictInt | None = None
    name: TstnText | None = None
    categories: tuple[TstnItemCategory, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )
    date_create: TstnText | None = None
    date_modify: TstnText | None = None
    article: TstnText | None = None
    barcode: TstnText | None = None
    brand: TstnText | None = None
    subbrand: TstnText | None = None
    country_supplier: TstnText | None = None
    base_unit: TstnText | None = None
    # Swagger declares an array without an item schema. Preserve JSON values
    # without inventing a nested domain model.
    units_list: tuple[Any, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


class TstnItemListResponse(TstnModel):
    count: TstnCount | None = None
    count_all: TstnCount | None = None
    page_num: TstnCount | None = None
    results: tuple[TstnItemDetail, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


class TstnAvailableItem(TstnModel):
    article: TstnText | None = None
    # Keep the supplier status as text; unknown values are not availability.
    status: TstnText | None = None


class TstnAvailableItemsResponse(TstnModel):
    city: TstnText | None = None
    count: TstnCount | None = None
    results: tuple[TstnAvailableItem, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


def _decimal_price(value: object) -> Decimal:
    """Accept exact JSON integers/decimals and the documented string example."""

    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise ValueError("price must be a decimal number")
    if isinstance(value, str):
        text = value.strip()
        if len(text) > MAX_TSTN_PRICE_TEXT_LENGTH or not _DECIMAL_TEXT_RE.fullmatch(text):
            raise ValueError("price is not a bounded decimal")
        value = text
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("price must be a decimal number") from None
    if not result.is_finite() or result < 0:
        raise ValueError("price must be finite and non-negative")
    parts = result.as_tuple()
    if len(parts.digits) > 80 or abs(parts.exponent) > 100:
        raise ValueError("price exceeds its numeric bounds")
    return result


class TstnItemPrice(TstnModel):
    article: TstnText | None = None
    # Swagger says integer but its own example is the string "6655.47".
    # Decimal parsing preserves that documented fractional value exactly.
    price: Decimal | None = None

    @field_validator("price", mode="before")
    @classmethod
    def _validate_price(cls, value: object) -> Decimal | None:
        return None if value is None else _decimal_price(value)


class TstnPricesResponse(TstnModel):
    city: TstnText | None = None
    count: TstnCount | None = None
    results: tuple[TstnItemPrice, ...] | None = Field(
        default=None,
        max_length=MAX_TSTN_COLLECTION_ITEMS,
    )


TstnCategoryDetail.model_rebuild()
