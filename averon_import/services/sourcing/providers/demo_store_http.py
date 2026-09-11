"""HTTP adapter for the separate Averon Demo Store.

The adapter deliberately stops at provider-owned catalog facts.  It does not
trust the store's ranking or decision fields; the regular Averon matcher keeps
ownership of all sourcing decisions.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlencode

from averon_import.services.app_settings import normalize_http_base_url
from averon_import.services.sourcing.models import Offer, ProductIntent


class DemoStoreProviderError(ValueError):
    """Sanitized, client-safe error raised by the Demo Store adapter."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
_OpenTransport = Callable[[urllib.request.Request, float], Any]
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 3.0
_HEALTH_TIMEOUT_SECONDS = 2.0
_MAX_LIMIT = 100

_ATTRIBUTE_ALIASES: dict[str, tuple[str, str]] = {
    "power_kw": ("power", "kw"),
    "power_w": ("power", "w"),
    "voltage_v": ("voltage", "identity"),
    "current_a": ("current", "identity"),
    "diameter_mm": ("diameter", "identity"),
    "dn": ("diameter", "identity"),
    "pressure_pn": ("pressure", "identity"),
    "pn": ("pressure", "identity"),
    "cable_section_mm2": ("cable_section", "identity"),
    "core_count": ("cores", "identity"),
    "ip": ("protection_class", "identity"),
}
_CANONICAL_ATTRIBUTES = {
    "power",
    "voltage",
    "current",
    "diameter",
    "pressure",
    "cable_section",
    "cores",
    "protection_class",
    "material",
    "mounting_type",
    "features",
    "model",
}


def _default_transport(request: urllib.request.Request, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _retrieval_payload(intent: ProductIntent) -> dict[str, Any]:
    """Prepare search-only identifiers without changing the source intent."""
    body = intent.model_dump(mode="json")
    model = body.get("model")
    if isinstance(model, str):
        primary_model = next((line.strip() for line in model.splitlines() if line.strip()), "")
        if primary_model and primary_model != model:
            body["model"] = primary_model
            queries = body.get("search_queries")
            if isinstance(queries, list):
                body["search_queries"] = list(dict.fromkeys([primary_model, *queries]))[:4]
    return body


class DemoStoreHttpProvider:
    key = "demo_store_http"
    label = "Averon Demo Store"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8877",
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        health_timeout_seconds: float = _HEALTH_TIMEOUT_SECONDS,
        transport: _OpenTransport | None = None,
    ) -> None:
        self.base_url = normalize_http_base_url(base_url)
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 10.0))
        self.health_timeout_seconds = max(0.1, min(float(health_timeout_seconds), 3.0))
        self._transport = transport or _default_transport
        self.last_request_diagnostics: dict[str, Any] = {}

    def search(self, intent: ProductIntent, *, limit: int = 20) -> list[Offer]:
        bounded_limit = _bounded_limit(limit)
        body = _retrieval_payload(intent)
        data = self._request_json(
            "POST",
            f"/api/v1/search?{urlencode({'limit': bounded_limit})}",
            body=body,
            timeout=self.timeout_seconds,
            operation="search",
        )
        if not isinstance(data, dict):
            raise DemoStoreProviderError(
                "Averon Demo Store: ответ поиска должен быть объектом",
                category="invalid_response",
            )
        items = data.get("items")
        if not isinstance(items, list):
            raise DemoStoreProviderError(
                "Averon Demo Store: ответ поиска не содержит список товаров",
                category="invalid_response",
            )
        catalog_version = str(data.get("catalog_version") or "unknown")
        offers: list[Offer] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise DemoStoreProviderError(
                    f"Averon Demo Store: товар {index + 1} имеет неверный формат",
                    category="invalid_response",
                )
            try:
                offers.append(self._offer_from_item(item, catalog_version=catalog_version))
            except DemoStoreProviderError:
                raise
            except Exception as exc:
                raise DemoStoreProviderError(
                    f"Averon Demo Store: товар {index + 1} не прошёл проверку",
                    category="invalid_response",
                ) from exc
        self.last_request_diagnostics.update({"item_count": len(offers)})
        return offers

    def stats(self) -> dict[str, Any]:
        try:
            data = self._request_json(
                "GET",
                "/api/v1/catalog/stats",
                timeout=self.health_timeout_seconds,
                operation="stats",
            )
        except DemoStoreProviderError as exc:
            return {
                "configured": True,
                "reachable": False,
                "item_count": 0,
                "catalog_version": "unavailable",
                "error": str(exc),
            }
        if not isinstance(data, dict):
            return self._unavailable_stats("Averon Demo Store: некорректный ответ stats")
        raw_count = data.get("items", data.get("item_count", 0))
        if isinstance(raw_count, list):
            item_count = len(raw_count)
        elif isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            item_count = raw_count
        else:
            return self._unavailable_stats("Averon Demo Store: stats содержит неверное число товаров")
        catalog_version = data.get("catalog_version", data.get("version", "unknown"))
        if catalog_version is None or isinstance(catalog_version, (dict, list)):
            return self._unavailable_stats("Averon Demo Store: stats содержит неверную версию каталога")
        return {
            "configured": True,
            "reachable": True,
            "item_count": item_count,
            "catalog_version": str(catalog_version),
            "latency_ms": self.last_request_diagnostics.get("latency_ms"),
        }

    def _offer_from_item(self, item: dict[str, Any], *, catalog_version: str) -> Offer:
        item_id = _required_text(item.get("id"), "id")
        title = _required_text(item.get("title"), "title")
        raw_attributes = item.get("attributes", {})
        if raw_attributes is None:
            raw_attributes = {}
        if not isinstance(raw_attributes, dict):
            raise DemoStoreProviderError(
                "Averon Demo Store: attributes товара имеют неверный формат",
                category="invalid_response",
            )
        attributes = _normalize_attributes(raw_attributes)
        remote_model = item.get("model")
        if remote_model not in (None, ""):
            existing_model = attributes.get("model")
            if existing_model not in (None, "") and not _same_value(existing_model, remote_model):
                raise DemoStoreProviderError(
                    "Averon Demo Store: конфликт model в товаре",
                    category="invalid_response",
                )
            if existing_model in (None, ""):
                attributes["model"] = remote_model

        provenance: dict[str, Any] = {
            "source": "averon_demo_store",
            "source_item_id": item_id,
            "catalog_version": catalog_version,
            "remote_attributes": raw_attributes,
        }
        if item.get("api_url") not in (None, ""):
            provenance["api_url"] = item["api_url"]
        if item.get("retrieval_score") is not None:
            provenance["retrieval_score"] = item["retrieval_score"]
        if item.get("provenance") is not None:
            provenance["remote_provenance"] = item["provenance"]

        try:
            return Offer(
                offer_id=item_id,
                provider=self.key,
                source_item_id=item_id,
                title=title,
                article=_optional_text(item.get("article")),
                manufacturer=_optional_text(item.get("manufacturer")),
                brand=_optional_text(item.get("brand")),
                price=item.get("price"),
                currency=_optional_text(item.get("currency")),
                price_unit=_optional_text(item.get("price_unit")) or "шт.",
                availability=_optional_bool(item.get("availability")),
                availability_text=_optional_text(item.get("availability_text")),
                url=_optional_text(item.get("url")),
                attributes=attributes,
                data_provenance=provenance,
            )
        except Exception as exc:
            raise DemoStoreProviderError(
                "Averon Demo Store: товар содержит недопустимые коммерческие данные",
                category="invalid_response",
            ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float,
        operation: str,
    ) -> Any:
        url = f"{self.base_url}{path}"
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        started = time.perf_counter()
        try:
            response = self._transport(request, timeout)
            try:
                status = getattr(response, "status", None) or getattr(response, "code", None)
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise DemoStoreProviderError(
                        f"Averon Demo Store: HTTP {status if status is not None else 'unknown'}",
                        category="http_error",
                        status_code=status if isinstance(status, int) else None,
                    )
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise DemoStoreProviderError(
                    "Averon Demo Store: ответ слишком большой",
                    category="invalid_response",
                )
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DemoStoreProviderError(
                    "Averon Demo Store: ответ содержит некорректный JSON",
                    category="invalid_response",
                ) from exc
            self.last_request_diagnostics = {
                "operation": operation,
                "http_status": status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            return result
        except urllib.error.HTTPError as exc:
            self.last_request_diagnostics = {
                "operation": operation,
                "http_status": int(exc.code),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            raise DemoStoreProviderError(
                f"Averon Demo Store: HTTP {int(exc.code)}",
                category="http_error",
                status_code=int(exc.code),
            ) from exc
        except urllib.error.URLError as exc:
            self.last_request_diagnostics = {
                "operation": operation,
                "http_status": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            reason = getattr(exc, "reason", None)
            category = "timeout" if isinstance(reason, TimeoutError) else "connection_error"
            message = "превышено время ожидания" if category == "timeout" else "ошибка соединения"
            raise DemoStoreProviderError(
                f"Averon Demo Store: {message}",
                category=category,
            ) from exc
        except (TimeoutError, OSError) as exc:
            self.last_request_diagnostics = {
                "operation": operation,
                "http_status": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            raise DemoStoreProviderError(
                "Averon Demo Store: превышено время ожидания" if isinstance(exc, TimeoutError)
                else "Averon Demo Store: ошибка соединения",
                category="timeout" if isinstance(exc, TimeoutError) else "connection_error",
            ) from exc

    @staticmethod
    def _unavailable_stats(error: str) -> dict[str, Any]:
        return {
            "configured": True,
            "reachable": False,
            "item_count": 0,
            "catalog_version": "unavailable",
            "error": error,
        }


def _bounded_limit(value: int) -> int:
    try:
        return max(1, min(int(value), _MAX_LIMIT))
    except (TypeError, ValueError):
        return 1


def _required_text(value: Any, field_name: str) -> str:
    if isinstance(value, (dict, list)):
        raise DemoStoreProviderError(
            f"Averon Demo Store: обязательное поле {field_name} имеет неверный формат",
            category="invalid_response",
        )
    text = str(value or "").strip()
    if not text:
        raise DemoStoreProviderError(
            f"Averon Demo Store: товар не содержит обязательное поле {field_name}",
            category="invalid_response",
        )
    return text


def _optional_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        raise DemoStoreProviderError(
            "Averon Demo Store: товар содержит поле неверного формата",
            category="invalid_response",
        )
    return "" if value is None else str(value).strip()


def _optional_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise DemoStoreProviderError(
        "Averon Demo Store: availability имеет неверный формат",
        category="invalid_response",
    )


def _normalize_attribute_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")


def _normalize_attributes(raw: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = _normalize_attribute_key(raw_key)
        if not key:
            continue
        if key in _ATTRIBUTE_ALIASES:
            target, unit = _ATTRIBUTE_ALIASES[key]
            value = _convert_numeric_alias(raw_value, unit, target)
        else:
            target = key if key in _CANONICAL_ATTRIBUTES else str(raw_key)
            value = raw_value
        if target in normalized and not _same_value(normalized[target], value):
            raise DemoStoreProviderError(
                f"Averon Demo Store: конфликт атрибутов {target}",
                category="invalid_response",
            )
        normalized[target] = value
    return normalized


def _convert_numeric_alias(value: Any, unit: str, target: str) -> Any:
    if target == "protection_class":
        return value
    number = _numeric_value(value)
    if number is None:
        raise DemoStoreProviderError(
            f"Averon Demo Store: атрибут {target} не является числом",
            category="invalid_response",
        )
    if unit == "w":
        return number / 1000
    return number


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    result = float(match.group(0).replace(",", "."))
    return result if math.isfinite(result) else None


def _same_value(left: Any, right: Any) -> bool:
    left_number = _numeric_value(left)
    right_number = _numeric_value(right)
    if left_number is not None and right_number is not None:
        return math.isclose(left_number, right_number, rel_tol=1e-9, abs_tol=1e-9)
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()
