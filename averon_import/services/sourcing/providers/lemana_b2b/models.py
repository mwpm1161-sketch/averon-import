from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class LemanaSupplierModel(BaseModel):
    """Bounded provider-side representation of an official supplier object."""

    model_config = ConfigDict(extra="ignore")


class LemanaProductRecord(LemanaSupplierModel):
    product_item: str = Field(
        default="", validation_alias=AliasChoices("productItem", "product_item")
    )
    product_available: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("productAvailible", "productAvailable", "product_available"),
    )
    product_name: str = Field(
        default="", validation_alias=AliasChoices("productName", "product_name")
    )
    product_description: str = Field(
        default="", validation_alias=AliasChoices("productDescription", "product_description")
    )
    product_url: str = Field(
        default="", validation_alias=AliasChoices("productUrl", "product_url")
    )
    product_model: str = Field(
        default="", validation_alias=AliasChoices("productModel", "product_model")
    )
    product_brand: str = Field(
        default="", validation_alias=AliasChoices("productBrand", "product_brand")
    )
    product_photo: Any = Field(
        default=None, validation_alias=AliasChoices("productPhoto", "product_photo")
    )
    product_barcode: str = Field(
        default="", validation_alias=AliasChoices("productBarcode", "product_barcode")
    )
    product_params: Any = Field(
        default_factory=dict,
        validation_alias=AliasChoices("productParams", "product_params"),
    )
    product_unit_sale: Any = Field(
        default=None, validation_alias=AliasChoices("productUnitSale", "product_unit_sale")
    )
    categories: Any = Field(default_factory=list)

    @field_validator("product_item", mode="before")
    @classmethod
    def _normalize_product_item(cls, value: Any) -> str:
        if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            raise ValueError("productItem must be a scalar")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("productItem is required")
        return normalized

    @field_validator(
        "product_name",
        "product_description",
        "product_url",
        "product_model",
        "product_brand",
        "product_barcode",
        mode="before",
    )
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            raise ValueError("text field must be scalar")
        return str(value).strip()

    @field_validator("product_available", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any) -> bool | None:
        if value is None or isinstance(value, bool):
            return value
        raise ValueError("availability must be boolean or null")


@dataclass(frozen=True)
class LemanaProductsPage:
    products: tuple[LemanaProductRecord, ...]
    page: int
    per_page: int
    total_count: int | None = None
    malformed_count: int = 0


@dataclass(frozen=True)
class LemanaPriceRecord:
    product_item: str
    price: Any
    currency: str


def _paging_value(payload: dict[str, Any], *names: str) -> int | None:
    paging = payload.get("paging")
    if not isinstance(paging, dict):
        return None
    for name in names:
        value = paging.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def parse_products_payload(payload: Any, *, page: int, per_page: int) -> LemanaProductsPage:
    if not isinstance(payload, dict):
        raise ValueError("products response must be an object")
    raw_products = payload.get("products")
    if raw_products is None:
        raw_products = payload.get("product")
    if not isinstance(raw_products, list):
        raise ValueError("products response must contain a list")
    products: list[LemanaProductRecord] = []
    malformed = 0
    for raw in raw_products:
        if not isinstance(raw, dict):
            malformed += 1
            continue
        try:
            products.append(LemanaProductRecord.model_validate(raw))
        except Exception:
            malformed += 1
    if raw_products and not products:
        raise ValueError("products response contains no valid products")
    return LemanaProductsPage(
        products=tuple(products),
        page=page,
        per_page=per_page,
        total_count=_paging_value(payload, "totalCount", "total_count", "total"),
        malformed_count=malformed,
    )


def _normalize_price(value: Any) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        raise ValueError("salesPrice must be numeric")
    text = str(value).strip()
    if not text:
        raise ValueError("salesPrice must be numeric")
    try:
        from decimal import Decimal

        price = Decimal(text)
    except Exception as exc:
        raise ValueError("salesPrice must be numeric") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("salesPrice must be finite and non-negative")
    return price


def parse_price_payload(payload: Any) -> tuple[LemanaPriceRecord, ...]:
    if not isinstance(payload, dict):
        raise ValueError("price response must be an object")
    raw_prices = payload.get("productPrice")
    if not isinstance(raw_prices, list):
        raise ValueError("price response must contain productPrice list")
    records: list[LemanaPriceRecord] = []
    malformed = 0
    for raw in raw_prices:
        try:
            if not isinstance(raw, dict):
                raise ValueError("price row must be an object")
            product_item = raw.get("productItem")
            if isinstance(product_item, bool) or product_item is None or isinstance(product_item, (dict, list)):
                raise ValueError("productItem is invalid")
            product_item = str(product_item).strip()
            sales_prices = raw.get("salesPrices")
            if not isinstance(sales_prices, list) or not sales_prices:
                raise ValueError("salesPrices is invalid")
            first = sales_prices[0]
            if not isinstance(first, dict):
                raise ValueError("sales price row is invalid")
            currency = first.get("currencyName", "")
            if currency is None or isinstance(currency, (dict, list)):
                raise ValueError("currencyName is invalid")
            records.append(
                LemanaPriceRecord(
                    product_item=product_item,
                    price=_normalize_price(first.get("salesPrice")),
                    currency=str(currency).strip(),
                )
            )
        except Exception:
            malformed += 1
    if raw_prices and not records:
        raise ValueError("price response contains no valid prices")
    return tuple(records)
