"""Offline-testable, bounded HTTP client for the VI B2B OpenAPI search."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from decimal import Decimal
from enum import Enum
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator
from urllib.parse import urlencode, urlsplit

from .models import (
    MAX_PRODUCTS_PER_PAGE,
    VseinstrumentiProductSearchResult,
    VseinstrumentiResponseError,
    parse_product_search_result,
)


VSEINSTRUMENTI_API_BASE_URLS = {
    "prod": "https://api.vseinstrumenti.ru/open-api",
    "test": "https://api.vseinstrumenti.ru/open-api/dev",
}
VSEINSTRUMENTI_PRODUCTS_PATH = "/v1/products"
VSEINSTRUMENTI_MAX_PAGE_SIZE = MAX_PRODUCTS_PER_PAGE
VSEINSTRUMENTI_MAX_RPM = 100
VSEINSTRUMENTI_RATE_WINDOW_SECONDS = 60.0
VSEINSTRUMENTI_DEFAULT_TIMEOUT_SECONDS = 10.0
VSEINSTRUMENTI_MAX_TIMEOUT_SECONDS = 30.0
VSEINSTRUMENTI_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FIAS_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_BEARER_RE = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
_MAX_BEARER_TOKEN_LENGTH = 8192


class VseinstrumentiErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"


_SAFE_MESSAGES = {
    VseinstrumentiErrorCategory.AUTHENTICATION: "Авторизация ВсеИнструменты OpenAPI не выполнена.",
    VseinstrumentiErrorCategory.INVALID_REQUEST: "ВсеИнструменты OpenAPI отклонил запрос.",
    VseinstrumentiErrorCategory.RATE_LIMITED: "ВсеИнструменты OpenAPI ограничил частоту запросов.",
    VseinstrumentiErrorCategory.TIMEOUT: "Время ожидания ВсеИнструменты OpenAPI истекло.",
    VseinstrumentiErrorCategory.TRANSPORT: "Не удалось связаться с ВсеИнструменты OpenAPI.",
    VseinstrumentiErrorCategory.INVALID_RESPONSE: "ВсеИнструменты OpenAPI вернул некорректный ответ.",
    VseinstrumentiErrorCategory.UNAVAILABLE: "ВсеИнструменты OpenAPI временно недоступен.",
    VseinstrumentiErrorCategory.MISCONFIGURED: "Настройки ВсеИнструменты OpenAPI некорректны.",
}


class VseinstrumentiApiError(RuntimeError):
    """Sanitized provider error; it never retains upstream body or token text."""

    def __init__(
        self,
        category: VseinstrumentiErrorCategory,
        *,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.status_code = status_code
        super().__init__(_SAFE_MESSAGES[category])


class VseinstrumentiRateLimiter:
    """Shared sliding-window limiter with one in-flight request per limiter."""

    def __init__(
        self,
        *,
        max_requests: int = VSEINSTRUMENTI_MAX_RPM,
        window_seconds: float = VSEINSTRUMENTI_RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(max_requests) is not int or not 1 <= max_requests <= VSEINSTRUMENTI_MAX_RPM:
            raise ValueError("max_requests must be between 1 and the documented VI limit")
        if not callable(clock) or not callable(sleeper):
            raise ValueError("clock and sleeper must be callable")
        try:
            window = float(window_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("window_seconds must be finite and positive") from None
        if not math.isfinite(window) or window != VSEINSTRUMENTI_RATE_WINDOW_SECONDS:
            raise ValueError("window_seconds must equal the documented 60-second window")
        self.max_requests = max_requests
        self.window_seconds = window
        self._clock = clock
        self._sleeper = sleeper
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._in_flight = threading.BoundedSemaphore(1)

    @contextmanager
    def request_slot(self) -> Iterator[None]:
        """Reserve one rate slot immediately before transport, conservatively."""

        self._in_flight.acquire()
        try:
            while True:
                with self._lock:
                    now = self._clock()
                    while self._timestamps and now >= math.nextafter(
                        self._timestamps[0] + self.window_seconds, math.inf
                    ):
                        self._timestamps.popleft()
                    if len(self._timestamps) < self.max_requests:
                        self._timestamps.append(now)
                        break
                    next_expiry = math.nextafter(
                        self._timestamps[0] + self.window_seconds, math.inf
                    )
                    wait = next_expiry - now
                self._sleeper(max(wait, 0.0))
            yield
        finally:
            self._in_flight.release()


_DEFAULT_CLOCK = time.monotonic
_DEFAULT_SLEEPER = time.sleep
# Kept at module scope so clients created after settings/runtime rebuilds still
# share one process-local provider budget. It starts no thread and does no I/O.
_SHARED_RATE_LIMITER = VseinstrumentiRateLimiter()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
Transport = Callable[[urllib.request.Request, float], Any]


def _default_transport(request: urllib.request.Request, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _validate_search_endpoint(url: str, environment: str) -> None:
    base_url = VSEINSTRUMENTI_API_BASE_URLS[environment]
    expected = urlsplit(base_url)
    try:
        actual = urlsplit(url)
        port = actual.port
    except ValueError:
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
    if (actual.scheme != "https" or actual.hostname != expected.hostname
            or port not in (None, 443) or actual.username is not None or actual.password is not None
            or actual.path != expected.path.rstrip("/") + VSEINSTRUMENTI_PRODUCTS_PATH
            or actual.query or actual.fragment):
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VseinstrumentiResponseError("response contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON numeric constant")


def _json_loads_exact(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise VseinstrumentiResponseError("response body is not valid JSON") from None


def _authentication_error_envelope(payload: object) -> bool:
    """Recognize only documented token/access phrases from a code-500 body."""

    if not isinstance(payload, dict):
        return False
    details = payload.get("details")
    messages = [payload.get("message")]
    if isinstance(details, dict):
        messages.append(details.get("message"))
    safe_markers = (
        "failed to parse token",
        "token is unverifiable",
        "нет доступа к ресурсам компании",
        "ошибка аутентификации некорректный токен",
    )
    return any(
        isinstance(message, str)
        and any(marker in message.casefold() for marker in safe_markers)
        for message in messages
    )


def _envelope_status(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    # The supplied VI PDF shows both `code` and `error` shapes. Accept only a
    # root integer status; nested product fields are never interpreted here.
    for field in ("code", "error"):
        value = payload.get(field)
        if type(value) is int and 400 <= value <= 599:
            return value
    return None


def _status_error(status: int, raw: bytes) -> VseinstrumentiApiError:
    envelope: object = None
    try:
        envelope = _json_loads_exact(raw) if raw else None
    except VseinstrumentiResponseError:
        pass
    if status == 500 and _authentication_error_envelope(envelope):
        category = VseinstrumentiErrorCategory.AUTHENTICATION
    elif status in (401, 403):
        category = VseinstrumentiErrorCategory.AUTHENTICATION
    elif status == 429:
        category = VseinstrumentiErrorCategory.RATE_LIMITED
    elif status == 400 or 400 <= status < 500:
        category = VseinstrumentiErrorCategory.INVALID_REQUEST
    elif status >= 500:
        category = VseinstrumentiErrorCategory.UNAVAILABLE
    else:
        category = VseinstrumentiErrorCategory.INVALID_RESPONSE
    return VseinstrumentiApiError(category, status_code=status)


def _read_bounded(response: Any) -> tuple[int, bytes]:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    if type(status) is not int:
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.INVALID_RESPONSE)
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.INVALID_RESPONSE)
    try:
        raw = reader(VSEINSTRUMENTI_MAX_RESPONSE_BYTES + 1)
    except (TimeoutError, socket.timeout):
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.TIMEOUT, status_code=status) from None
    except Exception:
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.TRANSPORT, status_code=status) from None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > VSEINSTRUMENTI_MAX_RESPONSE_BYTES:
        raise VseinstrumentiApiError(VseinstrumentiErrorCategory.INVALID_RESPONSE, status_code=status)
    return status, raw


class VseinstrumentiClient:
    """One-page VI product search client; construction and status are offline."""

    def __init__(
        self,
        bearer_token: str,
        *,
        environment: str = "prod",
        timeout_seconds: float = VSEINSTRUMENTI_DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
        rate_limiter: VseinstrumentiRateLimiter | None = None,
        clock: Callable[[], float] = _DEFAULT_CLOCK,
        sleeper: Callable[[float], None] = _DEFAULT_SLEEPER,
    ) -> None:
        if not isinstance(environment, str) or environment not in VSEINSTRUMENTI_API_BASE_URLS:
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        if (not isinstance(bearer_token, str) or len(bearer_token) > _MAX_BEARER_TOKEN_LENGTH
                or bearer_token != bearer_token.strip() or not _BEARER_RE.fullmatch(bearer_token)):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        if (not math.isfinite(timeout) or not 0.1 <= timeout <= VSEINSTRUMENTI_MAX_TIMEOUT_SECONDS):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        self._bearer_token = bearer_token
        self.environment = environment
        self.timeout_seconds = timeout
        self._transport = _default_transport if transport is None else transport
        if rate_limiter is not None:
            self.rate_limiter = rate_limiter
        elif clock is _DEFAULT_CLOCK and sleeper is _DEFAULT_SLEEPER:
            self.rate_limiter = _SHARED_RATE_LIMITER
        else:
            self.rate_limiter = VseinstrumentiRateLimiter(clock=clock, sleeper=sleeper)

    def __repr__(self) -> str:
        return (
            f"VseinstrumentiClient(environment={self.environment!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, bearer_token=<redacted>)"
        )

    def search_products(
        self,
        search: str,
        *,
        region_id: str,
        limit: int = VSEINSTRUMENTI_MAX_PAGE_SIZE,
        sort: str = "asc",
        order_by: str = "price",
    ) -> VseinstrumentiProductSearchResult:
        """Search one page at offset zero; no retries or follow-up requests."""

        if not isinstance(search, str):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None
        search = search.strip()
        if (not search or len(search) > 500 or any(ord(char) < 32 for char in search)
                or any(0xD800 <= ord(char) <= 0xDFFF for char in search)
                or not isinstance(region_id, str) or not _FIAS_RE.fullmatch(region_id)
                or type(limit) is not int or not 1 <= limit <= VSEINSTRUMENTI_MAX_PAGE_SIZE
                or not isinstance(sort, str) or sort not in {"asc", "desc"}
                or not isinstance(order_by, str) or order_by not in {"price", "popularity"}):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.MISCONFIGURED) from None

        base_url = VSEINSTRUMENTI_API_BASE_URLS[self.environment]
        endpoint = base_url.rstrip("/") + VSEINSTRUMENTI_PRODUCTS_PATH
        _validate_search_endpoint(endpoint, self.environment)
        query = urlencode({
            "search": search,
            "regionId": region_id,
            "limit": limit,
            "offset": 0,
            "sort": sort,
            "orderBy": order_by,
        })
        request = urllib.request.Request(
            f"{endpoint}?{query}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._bearer_token}"},
            method="GET",
        )
        try:
            with self.rate_limiter.request_slot():
                try:
                    response = self._transport(request, self.timeout_seconds)
                except urllib.error.HTTPError as exc:
                    try:
                        try:
                            status, raw = _read_bounded(exc)
                        except VseinstrumentiApiError:
                            status, raw = int(exc.code), b""
                        raise _status_error(status, raw) from None
                    finally:
                        exc.close()
                try:
                    status, raw = _read_bounded(response)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
        except VseinstrumentiApiError:
            raise
        except (TimeoutError, socket.timeout):
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.TIMEOUT) from None
        except urllib.error.URLError:
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.TRANSPORT) from None
        except Exception:
            # Transport exceptions can embed request headers or URLs. Never
            # retain their text, cause, repr, or traceback in the public error.
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.TRANSPORT) from None

        if status != 200:
            raise _status_error(status, raw) from None
        try:
            payload = _json_loads_exact(raw)
            api_status = _envelope_status(payload)
            if api_status is not None:
                if api_status == 500 and _authentication_error_envelope(payload):
                    raise VseinstrumentiApiError(
                        VseinstrumentiErrorCategory.AUTHENTICATION, status_code=api_status,
                    )
                raise _status_error(api_status, raw)
            return parse_product_search_result(
                payload,
                expected_search=search,
                expected_region_id=region_id,
                requested_limit=limit,
            )
        except VseinstrumentiApiError:
            raise
        except VseinstrumentiResponseError:
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.INVALID_RESPONSE) from None
        except Exception:
            raise VseinstrumentiApiError(VseinstrumentiErrorCategory.INVALID_RESPONSE) from None
