"""Bounded, lossless response models for the documented VI product search."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlsplit


MAX_SKU_LENGTH = 180
MAX_NAME_LENGTH = 320
MAX_ARTICLE_LENGTH = 100
MAX_BRAND_LENGTH = 100
MAX_UNIT_LENGTH = 80
MAX_URL_LENGTH = 500
MAX_SPEC_TEXT_LENGTH = 500
MAX_PRODUCTS_PER_PAGE = 40
_DECIMAL_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class VseinstrumentiResponseError(ValueError):
    """Response JSON is outside the bounded, documented VI search shape."""


def _text(value: object, *, field: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise VseinstrumentiResponseError(f"{field} must be a string")
    normalized = value.strip()
    if len(normalized) > maximum or (required and not normalized):
        raise VseinstrumentiResponseError(f"{field} is outside its allowed bounds")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in normalized):
        raise VseinstrumentiResponseError(f"{field} contains control characters")
    return normalized


def _optional_text(value: object, *, field: str, maximum: int) -> str:
    return "" if value is None else _text(value, field=field, maximum=maximum)


def _decimal(value: object, *, field: str) -> Decimal:
    """Accept JSON integer/Decimal or its documented string representation."""

    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise VseinstrumentiResponseError(f"{field} must be a decimal string or number")
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 120 or not _DECIMAL_TEXT.fullmatch(value):
            raise VseinstrumentiResponseError(f"{field} is not a bounded decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise VseinstrumentiResponseError(f"{field} is not a decimal") from None
    if not result.is_finite() or result < 0:
        raise VseinstrumentiResponseError(f"{field} must be finite and non-negative")
    parts = result.as_tuple()
    if len(parts.digits) > 80 or abs(parts.exponent) > 100:
        raise VseinstrumentiResponseError(f"{field} exceeds its numeric bounds")
    return result


@dataclass(frozen=True, slots=True)
class VseinstrumentiTechnicalSpecification:
    name: str = ""
    value: str = ""
    unit: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class VseinstrumentiProduct:
    sku: str
    name: str
    product_code: str
    brand_name: str
    technical_specifications: tuple[VseinstrumentiTechnicalSpecification, ...]
    price: Decimal | None
    base_price: Decimal | None
    unit: str
    stock_at_warehouse: Decimal | None
    pickup_date: str
    courier_date: str
    site_url: str


@dataclass(frozen=True, slots=True)
class VseinstrumentiProductSearchResult:
    search: str
    region_id: str
    products: tuple[VseinstrumentiProduct, ...]


def _optional_price_object(value: object, *, field: str) -> tuple[Decimal | None, Decimal | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise VseinstrumentiResponseError(f"{field} must be an object")
    price = None if value.get("price") is None else _decimal(value["price"], field=f"{field}.price")
    base_price = (
        None if value.get("basePrice") is None
        else _decimal(value["basePrice"], field=f"{field}.basePrice")
    )
    return price, base_price


def _optional_stock(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise VseinstrumentiResponseError("stock must be an object")
    quantity = value.get("atWarehouse")
    return None if quantity is None else _decimal(quantity, field="stock.atWarehouse")


def _specifications(value: object) -> tuple[VseinstrumentiTechnicalSpecification, ...]:
    if value is None or value == {}:
        return ()
    if not isinstance(value, dict):
        raise VseinstrumentiResponseError("technicalSpecifications must be an object")
    allowed = {"name", "value", "unit", "description"}
    if not set(value).issubset(allowed):
        raise VseinstrumentiResponseError("technicalSpecifications has an undocumented shape")
    fields = {
        key: _optional_text(value.get(key, ""), field=f"technicalSpecifications.{key}", maximum=MAX_SPEC_TEXT_LENGTH)
        for key in allowed
    }
    if not any(fields.values()):
        return ()
    return (VseinstrumentiTechnicalSpecification(**fields),)


def _site_url(value: object) -> str:
    if value is None:
        return ""
    url = _text(value, field="siteUrl", maximum=MAX_URL_LENGTH)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise VseinstrumentiResponseError("siteUrl is invalid") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "\\" in url or parsed.fragment or any(char.isspace() for char in url)):
        raise VseinstrumentiResponseError("siteUrl is invalid")
    # Query strings are preserved here: the upstream product-link example uses
    # one. The downstream durable projection has a stricter URL contract.
    return url


def _product(value: object) -> VseinstrumentiProduct:
    if not isinstance(value, dict):
        raise VseinstrumentiResponseError("each product must be an object")
    sku = _text(value.get("sku"), field="sku", maximum=MAX_SKU_LENGTH, required=True)
    name = _text(value.get("name"), field="name", maximum=MAX_NAME_LENGTH, required=True)
    product_code = _optional_text(value.get("productCode", ""), field="productCode", maximum=MAX_ARTICLE_LENGTH)
    brand_name = _optional_text(value.get("brandName", ""), field="brandName", maximum=MAX_BRAND_LENGTH)
    unit = _optional_text(value.get("unit", ""), field="unit", maximum=MAX_UNIT_LENGTH)
    pickup_date = ""
    courier_date = ""
    raw_dates = value.get("deliveryDates")
    if raw_dates is not None:
        if not isinstance(raw_dates, dict):
            raise VseinstrumentiResponseError("deliveryDates must be an object")
        pickup_date = _optional_text(raw_dates.get("pickup", ""), field="deliveryDates.pickup", maximum=80)
        courier_date = _optional_text(raw_dates.get("courier", ""), field="deliveryDates.courier", maximum=80)
    price, base_price = _optional_price_object(value.get("prices"), field="prices")
    return VseinstrumentiProduct(
        sku=sku,
        name=name,
        product_code=product_code,
        brand_name=brand_name,
        technical_specifications=_specifications(value.get("technicalSpecifications")),
        price=price,
        base_price=base_price,
        unit=unit,
        stock_at_warehouse=_optional_stock(value.get("stock")),
        pickup_date=pickup_date,
        courier_date=courier_date,
        site_url=_site_url(value.get("siteUrl")),
    )


def parse_product_search_result(
    payload: object,
    *,
    expected_search: str,
    expected_region_id: str,
    requested_limit: int = MAX_PRODUCTS_PER_PAGE,
) -> VseinstrumentiProductSearchResult:
    """Strictly parse the documented search envelope without inferring facts.

    The PDF calls ``products`` an object but its response example is an array.
    This parser accepts the demonstrated array and an empty object (the only
    unambiguous empty-object representation); non-empty objects are rejected.
    """

    if type(requested_limit) is not int or not 1 <= requested_limit <= MAX_PRODUCTS_PER_PAGE:
        raise VseinstrumentiResponseError("requested product limit is outside its allowed bounds")
    if not isinstance(payload, dict):
        raise VseinstrumentiResponseError("response must be an object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise VseinstrumentiResponseError("response result must be an object")
    search = _text(result.get("search"), field="result.search", maximum=500, required=True)
    region_id = _text(result.get("regionId"), field="result.regionId", maximum=80, required=True)
    if search != expected_search or region_id != expected_region_id:
        raise VseinstrumentiResponseError("response context does not match the request")
    raw_products = result.get("products")
    if isinstance(raw_products, dict):
        if raw_products:
            raise VseinstrumentiResponseError("non-empty products object has an undocumented shape")
        raw_products = []
    if not isinstance(raw_products, list):
        raise VseinstrumentiResponseError("result.products must be an array or empty object")
    if len(raw_products) > requested_limit:
        raise VseinstrumentiResponseError("response contains more products than the requested page limit")

    products: list[VseinstrumentiProduct] = []
    seen: dict[str, tuple[VseinstrumentiProduct, dict[str, Any]]] = {}
    for raw in raw_products:
        product = _product(raw)
        previous = seen.get(product.sku)
        if previous is not None:
            if previous[1] != raw:
                raise VseinstrumentiResponseError("response contains conflicting duplicate SKUs")
            continue
        seen[product.sku] = (product, raw)
        products.append(product)
    return VseinstrumentiProductSearchResult(search=search, region_id=region_id, products=tuple(products))
