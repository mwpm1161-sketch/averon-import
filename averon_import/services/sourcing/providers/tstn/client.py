"""Bounded, offline-testable client for the eight TSTN API 1.0.0 operations.

The Swagger contract sends credentials in query parameters. This client never
logs, stores, or attaches a complete URL or upstream body to an exception.
It performs one HTTP attempt per method call and does not follow redirects.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import json
import math
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlencode, urlsplit

from pydantic import BaseModel, SecretStr

from .models import (
    MAX_TSTN_CREDENTIAL_LENGTH,
    TstnAccessRequest,
    TstnAuthorizationRequest,
    TstnAuthorizationResponse,
    TstnAvailableItemsResponse,
    TstnCatalogFilterRequest,
    TstnCategoriesResponse,
    TstnCategoryDetail,
    TstnCity,
    TstnItemDetail,
    TstnItemDetailRequest,
    TstnItemListResponse,
    TstnItemsRequest,
    TstnPricesResponse,
    TstnSectionDetailRequest,
)


TSTN_API_BASE_URL = "https://tstn.ru/api/v1"
TSTN_ALLOWED_HOST = "tstn.ru"
TSTN_DEFAULT_TIMEOUT_SECONDS = 10.0
TSTN_MAX_TIMEOUT_SECONDS = 30.0
TSTN_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TSTN_MAX_HTTP_ATTEMPTS = 64
TSTN_MAX_URL_LENGTH = 16_384


class TstnErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    REDIRECT_BLOCKED = "redirect_blocked"
    UNEXPECTED_STATUS = "unexpected_status"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"
    REQUEST_LIMIT = "request_limit"
    MISCONFIGURED = "misconfigured"


_SAFE_MESSAGES = {
    TstnErrorCategory.AUTHENTICATION: "TSTN authentication failed.",
    TstnErrorCategory.FORBIDDEN: "TSTN denied access to the requested operation.",
    TstnErrorCategory.NOT_FOUND: "TSTN did not find the requested resource.",
    TstnErrorCategory.RATE_LIMITED: "TSTN rate limit was reached.",
    TstnErrorCategory.UNAVAILABLE: "TSTN is temporarily unavailable.",
    TstnErrorCategory.INVALID_REQUEST: "TSTN rejected the request.",
    TstnErrorCategory.REDIRECT_BLOCKED: "TSTN returned a redirect; it was not followed.",
    TstnErrorCategory.UNEXPECTED_STATUS: "TSTN returned an unexpected HTTP status.",
    TstnErrorCategory.TIMEOUT: "The TSTN request timed out.",
    TstnErrorCategory.TRANSPORT: "The TSTN request could not be completed.",
    TstnErrorCategory.INVALID_RESPONSE: "TSTN returned an invalid response.",
    TstnErrorCategory.RESPONSE_TOO_LARGE: "The TSTN response exceeded the configured size limit.",
    TstnErrorCategory.REQUEST_LIMIT: "The TSTN client request-attempt limit was reached.",
    TstnErrorCategory.MISCONFIGURED: "The TSTN client configuration or request is invalid.",
}


class TstnApiError(RuntimeError):
    """Sanitized error that carries no URL, query, response body, or secret."""

    def __init__(
        self,
        category: TstnErrorCategory,
        *,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.status_code = status_code
        message = _SAFE_MESSAGES[category]
        if status_code is not None:
            message = f"{message} (HTTP {status_code})"
        super().__init__(message)

    def __repr__(self) -> str:
        return f"TstnApiError(category={self.category.value!r}, status_code={self.status_code!r})"


@dataclass(frozen=True, slots=True)
class TstnHttpResponse:
    """Small transport result; production reads at most limit plus one byte."""

    status_code: int
    body: bytes


Transport = Callable[[urllib.request.Request, float], TstnHttpResponse]
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
_DOCUMENTED_OPERATIONS = frozenset({
    ("authorize", "POST", "/user/"),
    ("list_sections", "GET", "/catalog/sections/"),
    ("get_section", "GET", "/catalog/sections/{id}/"),
    ("list_items", "GET", "/catalog/items/"),
    ("get_item", "GET", "/catalog/items/{id}/"),
    ("get_available", "GET", "/catalog/available/"),
    ("get_prices", "GET", "/catalog/prices/"),
    ("list_cities", "GET", "/city/"),
})


def _default_transport(
    request: urllib.request.Request,
    timeout: float,
) -> TstnHttpResponse:
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            body = response.read(TSTN_MAX_RESPONSE_BYTES + 1)
            return TstnHttpResponse(status_code=int(response.getcode()), body=body)
    except urllib.error.HTTPError as exc:
        # Do not read an error body: a server or proxy could reflect query data.
        status = int(exc.code)
        exc.close()
        return TstnHttpResponse(status_code=status, body=b"")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _load_json(body: bytes) -> Any:
    return json.loads(
        body.decode("utf-8"),
        parse_float=Decimal,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def _credential_value(value: object) -> str | None:
    if isinstance(value, SecretStr):
        text = value.get_secret_value()
    elif isinstance(value, str):
        text = value
    else:
        return None
    if (not text or len(text) > MAX_TSTN_CREDENTIAL_LENGTH
            or text != text.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        return None
    return text


def _validate_url(url: str, *, expected_path: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == TSTN_ALLOWED_HOST
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and parts.path == expected_path
        and bool(parts.query)
        and not parts.fragment
        and len(url) <= TSTN_MAX_URL_LENGTH
    )


def _status_category(status: int) -> TstnErrorCategory:
    if status == 401:
        return TstnErrorCategory.AUTHENTICATION
    if status == 403:
        return TstnErrorCategory.FORBIDDEN
    if status == 404:
        return TstnErrorCategory.NOT_FOUND
    if status == 429:
        return TstnErrorCategory.RATE_LIMITED
    if 300 <= status < 400:
        return TstnErrorCategory.REDIRECT_BLOCKED
    if 500 <= status < 600:
        return TstnErrorCategory.UNAVAILABLE
    if 400 <= status < 500:
        return TstnErrorCategory.INVALID_REQUEST
    return TstnErrorCategory.UNEXPECTED_STATUS


class TstnApiClient:
    """Client for exactly the eight operations listed in TSTN Swagger 2.0."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = TSTN_DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = TSTN_MAX_RESPONSE_BYTES,
        max_attempts: int = TSTN_MAX_HTTP_ATTEMPTS,
    ) -> None:
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None
        if (isinstance(timeout_seconds, bool) or not math.isfinite(timeout)
                or not 0.1 <= timeout <= TSTN_MAX_TIMEOUT_SECONDS):
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None
        if (type(max_response_bytes) is not int
                or not 1 <= max_response_bytes <= TSTN_MAX_RESPONSE_BYTES
                or type(max_attempts) is not int
                or not 1 <= max_attempts <= TSTN_MAX_HTTP_ATTEMPTS
                or transport is not None and not callable(transport)):
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None
        self.timeout_seconds = timeout
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max_attempts
        self._transport = _default_transport if transport is None else transport
        self._attempt_count = 0
        self._attempt_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            "TstnApiClient("
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_response_bytes={self.max_response_bytes!r}, "
            f"max_attempts={self.max_attempts!r})"
        )

    @property
    def request_attempt_count(self) -> int:
        with self._attempt_lock:
            return self._attempt_count

    def authorize(
        self,
        *,
        service: str | SecretStr,
        key: str | SecretStr,
    ) -> TstnAuthorizationResponse:
        request_data = self._validate_request(
            TstnAuthorizationRequest,
            {"service": service, "key": key},
        )
        service_value = _credential_value(request_data.service)
        key_value = _credential_value(request_data.key)
        if service_value is None or key_value is None:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        response = self._request(
            "authorize",
            "/user/",
            "POST",
            (("service", service_value), ("key", key_value)),
            TstnAuthorizationResponse,
        )
        return response

    def list_sections(
        self,
        *,
        access_token: str | SecretStr,
    ) -> TstnCategoriesResponse:
        token = self._access_token(access_token)
        return self._request(
            "list_sections", "/catalog/sections/", "GET",
            (("access_token", token),), TstnCategoriesResponse,
        )

    def get_section(
        self,
        section_id: int,
        *,
        access_token: str | SecretStr,
    ) -> TstnCategoryDetail:
        request_data = self._validate_request(
            TstnSectionDetailRequest,
            {"section_id": section_id, "access_token": access_token},
        )
        return self._request(
            "get_section",
            f"/catalog/sections/{request_data.section_id}/",
            "GET",
            (("access_token", _credential_value(request_data.access_token) or ""),),
            TstnCategoryDetail,
        )

    def list_items(
        self,
        *,
        access_token: str | SecretStr,
        category: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> TstnItemListResponse:
        request_data = self._validate_request(
            TstnItemsRequest,
            {"access_token": access_token, "category": category, "limit": limit, "offset": offset},
        )
        token = _credential_value(request_data.access_token) or ""
        params = [("access_token", token)]
        params.extend((name, value) for name, value in (
            ("category", request_data.category),
            ("limit", request_data.limit),
            ("offset", request_data.offset),
        ) if value is not None)
        return self._request("list_items", "/catalog/items/", "GET", params, TstnItemListResponse)

    def get_item(
        self,
        item_id: str | int,
        *,
        access_token: str | SecretStr,
    ) -> TstnItemDetail:
        request_data = self._validate_request(
            TstnItemDetailRequest,
            {"item_id": item_id, "access_token": access_token},
        )
        token = _credential_value(request_data.access_token) or ""
        item_id_text = str(request_data.item_id)
        if not item_id_text or item_id_text in {".", ".."}:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        encoded_id = quote(item_id_text, safe="")
        return self._request(
            "get_item", f"/catalog/items/{encoded_id}/", "GET",
            (("access_token", token),), TstnItemDetail,
        )

    def get_available(
        self,
        *,
        access_token: str | SecretStr,
        category: int | None = None,
        id: str | None = None,
        article: str | None = None,
        city: str | None = None,
    ) -> TstnAvailableItemsResponse:
        request_data = self._validate_request(
            TstnCatalogFilterRequest,
            {"access_token": access_token, "category": category, "id": id, "article": article, "city": city},
        )
        params = self._catalog_filter_params(request_data)
        return self._request("get_available", "/catalog/available/", "GET", params, TstnAvailableItemsResponse)

    def get_prices(
        self,
        *,
        access_token: str | SecretStr,
        category: int | None = None,
        id: str | None = None,
        article: str | None = None,
        city: str | None = None,
    ) -> TstnPricesResponse:
        request_data = self._validate_request(
            TstnCatalogFilterRequest,
            {"access_token": access_token, "category": category, "id": id, "article": article, "city": city},
        )
        params = self._catalog_filter_params(request_data)
        return self._request("get_prices", "/catalog/prices/", "GET", params, TstnPricesResponse)

    def list_cities(
        self,
        *,
        access_token: str | SecretStr,
    ) -> tuple[TstnCity, ...]:
        token = self._access_token(access_token)
        raw = self._request("list_cities", "/city/", "GET", (("access_token", token),), tuple)
        if len(raw) > 10_000:
            raise TstnApiError(TstnErrorCategory.INVALID_RESPONSE, status_code=200)
        parsed: list[TstnCity] = []
        failed = False
        try:
            parsed = [TstnCity.model_validate(item) for item in raw]
        except Exception:
            failed = True
        if failed:
            raise TstnApiError(TstnErrorCategory.INVALID_RESPONSE, status_code=200)
        return tuple(parsed)

    @staticmethod
    def _access_token(value: str | SecretStr) -> str:
        token = _credential_value(value)
        if token is None:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        return token

    @staticmethod
    def _validate_request(model_type, values):
        parsed = None
        failed = False
        try:
            parsed = model_type.model_validate(values)
        except Exception:
            failed = True
        if failed:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        return parsed

    @staticmethod
    def _catalog_filter_params(request_data: TstnCatalogFilterRequest) -> list[tuple[str, object]]:
        params: list[tuple[str, object]] = [
            ("access_token", _credential_value(request_data.access_token) or ""),
        ]
        params.extend((name, value) for name, value in (
            ("category", request_data.category),
            ("id", request_data.id),
            ("article", request_data.article),
            ("city", request_data.city),
        ) if value is not None)
        return params

    def _request(
        self,
        operation: str,
        path: str,
        method: str,
        params: tuple[tuple[str, object], ...] | list[tuple[str, object]],
        response_type: type[ResponseModel] | type[tuple],
    ) -> ResponseModel | tuple:
        documented_path = path
        if operation == "get_section":
            documented_path = "/catalog/sections/{id}/"
        elif operation == "get_item":
            documented_path = "/catalog/items/{id}/"
        if (operation, method, documented_path) not in _DOCUMENTED_OPERATIONS:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        try:
            query = urlencode(params, doseq=False, quote_via=quote, safe="")
            url = f"{TSTN_API_BASE_URL}{path}?{query}"
        except Exception:
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED) from None
        expected_path = f"/api/v1{path}"
        if not _validate_url(url, expected_path=expected_path):
            raise TstnApiError(TstnErrorCategory.MISCONFIGURED)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method=method,
        )
        if not self._reserve_attempt():
            raise TstnApiError(TstnErrorCategory.REQUEST_LIMIT)

        response = None
        transport_failure: TstnErrorCategory | None = None
        status_from_exception: int | None = None
        try:
            response = self._transport(request, self.timeout_seconds)
        except (TimeoutError, socket.timeout):
            transport_failure = TstnErrorCategory.TIMEOUT
        except urllib.error.HTTPError as exc:
            try:
                status_from_exception = int(exc.code)
            except Exception:
                transport_failure = TstnErrorCategory.TRANSPORT
            close = getattr(exc, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        except Exception:
            # Transport text can contain a complete URL or reflected query.
            transport_failure = TstnErrorCategory.TRANSPORT
        if transport_failure is not None:
            raise TstnApiError(transport_failure)
        if status_from_exception is not None:
            raise TstnApiError(
                _status_category(status_from_exception),
                status_code=status_from_exception,
            )
        if (not isinstance(response, TstnHttpResponse)
                or type(response.status_code) is not int
                or not isinstance(response.body, bytes)):
            raise TstnApiError(TstnErrorCategory.INVALID_RESPONSE)
        if len(response.body) > self.max_response_bytes:
            raise TstnApiError(
                TstnErrorCategory.RESPONSE_TOO_LARGE,
                status_code=response.status_code,
            )
        if response.status_code != 200:
            raise TstnApiError(
                _status_category(response.status_code),
                status_code=response.status_code,
            )

        parsed = None
        failed = False
        try:
            payload = _load_json(response.body)
            if response_type is tuple:
                if not isinstance(payload, list):
                    raise ValueError("array response expected")
                parsed = payload
            else:
                parsed = response_type.model_validate(payload)
        except Exception:
            failed = True
        if failed:
            raise TstnApiError(TstnErrorCategory.INVALID_RESPONSE, status_code=200)
        return parsed

    def _reserve_attempt(self) -> bool:
        with self._attempt_lock:
            if self._attempt_count >= self.max_attempts:
                return False
            self._attempt_count += 1
            return True
