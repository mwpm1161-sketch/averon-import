"""Inactive outcome-native adapter for one VI OpenAPI product search."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.product_understanding import normalize_attribute_key
from averon_import.services.sourcing.providers.contracts import (
    ProviderAffinity,
    ProviderFailureCategory,
    ProviderSearchOutcome,
    ProviderSearchRequestIdentity,
    ProviderSearchState,
)
from averon_import.services.sourcing.providers.execution import ProviderRequestCounter

from .client import (
    VSEINSTRUMENTI_MAX_PAGE_SIZE,
    VseinstrumentiApiError,
    VseinstrumentiClient,
    VseinstrumentiErrorCategory,
)
from .models import VseinstrumentiProduct, VseinstrumentiTechnicalSpecification


_ADAPTER_REVISION = "vseinstrumenti_openapi_execution_v1"
_CLIENT_REVISION = "vseinstrumenti_openapi_client_v1"
_MAX_QUERY_LENGTH = 500
_NUMERIC_TEXT_RE = re.compile(r"^\+?\d+(?:[.,]\d+)?$")
_DIMENSIONS_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)"
    r"(?:\s*[xх×]\s*(\d+(?:[.,]\d+)?))?\s*$",
    re.IGNORECASE,
)
_DURABLE_SECRET_TEXT = re.compile(
    r"(?:authorization\s*[:=]|\bbearer\s+|(?:password|passwd|client_secret|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|token)\s*[:=])",
    re.IGNORECASE,
)
_RUSSIAN_ATTRIBUTE_ALIASES = {
    "мощность": "power",
    "номинальная мощность": "power",
    "напряжение": "voltage",
    "рабочее напряжение": "voltage",
    "ток": "current",
    "диаметр": "diameter",
    "условный проход": "diameter",
    "ду": "diameter",
    "давление": "pressure",
    "ру": "pressure",
    "сечение кабеля": "cable_section",
    "сечение кабеля мм2": "cable_section",
    "количество жил": "cores",
    "число жил": "cores",
    "степень защиты": "protection_class",
    "класс защиты": "protection_class",
    "габариты": "dimensions",
    "размеры": "dimensions",
    "материал": "material",
    "тип монтажа": "mounting_type",
    "особенности": "features",
}
_FAILURE_CATEGORIES = {
    VseinstrumentiErrorCategory.AUTHENTICATION: ProviderFailureCategory.AUTHENTICATION,
    VseinstrumentiErrorCategory.INVALID_REQUEST: ProviderFailureCategory.MISCONFIGURED,
    VseinstrumentiErrorCategory.RATE_LIMITED: ProviderFailureCategory.RATE_LIMITED,
    VseinstrumentiErrorCategory.TIMEOUT: ProviderFailureCategory.TIMEOUT,
    VseinstrumentiErrorCategory.TRANSPORT: ProviderFailureCategory.TRANSPORT,
    VseinstrumentiErrorCategory.INVALID_RESPONSE: ProviderFailureCategory.INVALID_RESPONSE,
    VseinstrumentiErrorCategory.UNAVAILABLE: ProviderFailureCategory.UNAVAILABLE,
    VseinstrumentiErrorCategory.MISCONFIGURED: ProviderFailureCategory.MISCONFIGURED,
}


def _opaque_revision(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _query_plan(intent: ProductIntent) -> tuple[str, str]:
    """Choose one query: article, first prepared query, model, name, source text."""

    candidates = (
        ("article", intent.article),
        ("search_queries", intent.search_queries[0] if intent.search_queries else ""),
        ("model", intent.model),
        ("normalized_name", intent.normalized_name),
        ("source_text", intent.source_text),
    )
    for source, value in candidates:
        query = value.strip() if isinstance(value, str) else ""
        if query:
            return source, query
    return "none", ""


def _attribute_key(name: str) -> str | None:
    folded = re.sub(r"[^a-z0-9а-яё]+", " ", name.casefold().replace("ё", "е")).strip()
    return _RUSSIAN_ATTRIBUTE_ALIASES.get(folded) or normalize_attribute_key(name)


def _unit_key(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold().replace("ё", "е")).replace("²", "2")


def _decimal_spec(value: str) -> Decimal | None:
    normalized = value.strip()
    if len(normalized) > 100 or not _NUMERIC_TEXT_RE.fullmatch(normalized):
        return None
    try:
        result = Decimal(normalized.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _specification_value(spec: VseinstrumentiTechnicalSpecification, key: str) -> Any | None:
    value = spec.value.strip()
    if not value:
        return None
    unit = _unit_key(spec.unit)
    if key == "power":
        number = _decimal_spec(value)
        if number is None:
            return None
        if unit in {"w", "вт"}:
            return number / Decimal(1000)
        return number if unit in {"kw", "квт"} else None
    expected_units = {
        "voltage": {"v", "в"},
        "current": {"a", "а"},
        "diameter": {"mm", "мм"},
        "cable_section": {"mm2", "мм2"},
    }
    if key in expected_units:
        number = _decimal_spec(value)
        return number if number is not None and unit in expected_units[key] else None
    if key == "pressure":
        number = _decimal_spec(value)
        if number is None:
            return None
        name = re.sub(r"[^a-z0-9а-яё]+", " ", spec.name.casefold().replace("ё", "е")).strip()
        return number if unit in {"pn", "ру"} or (not unit and name in {"pn", "ру"}) else None
    if key == "cores":
        number = _decimal_spec(value)
        return number if number is not None and unit in {"", "count", "шт", "жил", "жила", "жилы"} else None
    if key == "dimensions":
        if unit not in {"mm", "мм"}:
            return None
        match = _DIMENSIONS_RE.fullmatch(value)
        if match is None:
            return None
        return "x".join(part.replace(",", ".") for part in match.groups() if part is not None)
    if key == "protection_class":
        return re.sub(r"\s+", "", value).upper()
    if key in {"material", "mounting_type", "features"}:
        return value
    return None


def _offer_attributes(product: VseinstrumentiProduct) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for spec in product.technical_specifications:
        key = _attribute_key(spec.name)
        if key is None:
            continue
        value = _specification_value(spec, key)
        if value is None:
            continue
        if key in attributes and attributes[key] != value:
            raise ValueError("conflicting normalized VI characteristics")
        attributes[key] = value
    if product.stock_at_warehouse is not None:
        attributes["supplier_stock_at_warehouse"] = product.stock_at_warehouse
    if product.pickup_date:
        attributes["supplier_pickup_date"] = product.pickup_date
    if product.courier_date:
        attributes["supplier_courier_date"] = product.courier_date
    return attributes


def _availability_text(product: VseinstrumentiProduct) -> str:
    facts: list[str] = []
    if product.stock_at_warehouse is not None:
        facts.append(f"Остаток по данным ВИ: {product.stock_at_warehouse}")
    if product.pickup_date:
        facts.append(f"Самовывоз: {product.pickup_date}")
    if product.courier_date:
        facts.append(f"Доставка: {product.courier_date}")
    summary = "; ".join(facts)
    return summary if len(summary) <= 120 else ""


def _safe_product_url(value: str) -> str:
    """Retain only a URL already accepted by the durable reader; never rewrite."""

    if not value:
        return ""
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in value
        or "redirect" in f"{parts.hostname}{parts.path}".casefold()
        or any(char.isspace() for char in value)
        or _DURABLE_SECRET_TEXT.search(unquote(value))
    ):
        return ""
    return value


class VseinstrumentiExecutionAdapter:
    """One-query, one-page VI adapter; construct and register explicitly."""

    key = "vseinstrumenti"

    def __init__(
        self,
        client: VseinstrumentiClient,
        *,
        region_id: str,
    ) -> None:
        if not isinstance(region_id, str) or len(region_id) > 80:
            raise ValueError("VI region id is outside its allowed bounds")
        self.client = client
        self.region_id = region_id

    def _affinity(self) -> ProviderAffinity:
        # Fingerprint only explicit non-secret client/query settings. The
        # bearer token and any derivative of it are intentionally excluded.
        config_revision = _opaque_revision({
            "client_revision": _CLIENT_REVISION,
            "environment": self.client.environment,
            "timeout_seconds": self.client.timeout_seconds,
            "max_requests": self.client.rate_limiter.max_requests,
            "rate_window_seconds": self.client.rate_limiter.window_seconds,
            "max_page_size": VSEINSTRUMENTI_MAX_PAGE_SIZE,
        })
        return ProviderAffinity(
            environment=self.client.environment,
            region_id=self.region_id,
            config_revision=config_revision,
            adapter_revision=_ADAPTER_REVISION,
        )

    def request_identity(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        execution_scope_id: str,
    ) -> ProviderSearchRequestIdentity:
        affinity = self._affinity()
        query_source, query = _query_plan(intent)
        effective_limit = min(limit, VSEINSTRUMENTI_MAX_PAGE_SIZE) if type(limit) is int and limit > 0 else limit
        fingerprint = _opaque_revision({
            "provider_key": self.key,
            "query_source": query_source,
            "search": query,
            "requested_limit": limit,
            "page_limit": effective_limit,
            "offset": 0,
            "sort": "asc",
            "order_by": "price",
            "affinity": affinity.model_dump(mode="json"),
        })
        return ProviderSearchRequestIdentity(
            execution_scope_id=execution_scope_id,
            provider_key=self.key,
            request_fingerprint=fingerprint,
            affinity=affinity,
            limit=limit,
        )

    def execute_search(
        self,
        intent: ProductIntent,
        *,
        limit: int,
        request_counter: ProviderRequestCounter,
    ) -> ProviderSearchOutcome:
        affinity = self._affinity()
        _query_source, query = _query_plan(intent)
        if (
            not query
            or len(query) > _MAX_QUERY_LENGTH
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in query)
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.MISCONFIGURED,
            )
        try:
            result = self.client.search_products(
                query,
                region_id=self.region_id,
                limit=min(limit, VSEINSTRUMENTI_MAX_PAGE_SIZE),
                outbound_attempt_observer=request_counter,
            )
        except VseinstrumentiApiError as exc:
            return self._failure(
                request_counter,
                affinity,
                _FAILURE_CATEGORIES.get(exc.category, ProviderFailureCategory.UNKNOWN),
            )
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.UNKNOWN,
            )

        try:
            offers = [self._offer(product, affinity) for product in result.products]
        except Exception:
            return self._failure(
                request_counter,
                affinity,
                ProviderFailureCategory.INVALID_RESPONSE,
            )
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=ProviderSearchState.SUCCESS if offers else ProviderSearchState.EMPTY,
            offers=tuple(offers),
            request_count=request_counter.request_count,
            affinity=affinity,
        )

    def _failure(
        self,
        request_counter: ProviderRequestCounter,
        affinity: ProviderAffinity,
        category: ProviderFailureCategory,
    ) -> ProviderSearchOutcome:
        return ProviderSearchOutcome(
            provider_key=self.key,
            state=ProviderSearchState.FAILURE,
            request_count=request_counter.request_count,
            failure_category=category,
            affinity=affinity,
        )

    def _offer(self, product: VseinstrumentiProduct, affinity: ProviderAffinity) -> Offer:
        return Offer(
            offer_id=product.sku,
            provider=self.key,
            source_item_id=product.sku,
            title=product.name,
            article=product.product_code,
            manufacturer="",
            brand=product.brand_name,
            price=product.price,
            currency="",
            price_unit=product.unit or "",
            availability=None,
            availability_text=_availability_text(product),
            url=_safe_product_url(product.site_url),
            attributes=_offer_attributes(product),
            data_provenance={
                "source": self.key,
                "source_item_id": product.sku,
                "price_field": "prices.price" if product.price is not None else "",
                "environment": affinity.environment,
                "region_id": affinity.region_id,
                "config_revision": affinity.config_revision,
            },
        )
