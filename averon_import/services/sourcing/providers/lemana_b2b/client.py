from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.parse import urlencode

from averon_import.services.app_settings import LemanaB2BSettings
from averon_import.services.sourcing.providers.base import SourcingProviderError

from .models import LemanaPriceRecord, LemanaProductsPage, parse_price_payload, parse_products_payload

LEMANA_AUTH_URL = (
    "https://customer.auth.lemanapro.ru/realms/b2b/protocol/openid-connect/token"
)
LEMANA_API_URLS: dict[str, str] = {
    "test": "https://api-test.lemanapro.ru",
    "prod": "https://api.lemanapro.ru",
}
LEMANA_PRODUCTS_PATH = "/b2bintegration-products/v1/products"
LEMANA_PRICE_PATH = "/b2bintegration/sale-prices/v1/sales-prices"
LEMANA_PRICE_BATCH_PATH = "/b2bintegration/sale-prices/v1/sale-prices:search"

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_BATCH_SIZE = 100
_TOKEN_MARGIN_SECONDS = 30.0


class LemanaNotModified(Exception):
    """The remote product endpoint confirmed that the mirror is current."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
Transport = Callable[[urllib.request.Request, float], Any]


def _default_transport(request: urllib.request.Request, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class _Token:
    access_token: str
    expires_at: float


class LemanaB2BClient:
    """Small, provider-owned HTTP boundary for the documented B2B API."""

    def __init__(
        self,
        settings: LemanaB2BSettings,
        client_secret: str | None,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._client_secret = str(client_secret or "").strip()
        self._transport = transport or _default_transport
        self._clock = clock
        self._token: _Token | None = None
        self._token_lock = threading.Lock()

    @property
    def api_base_url(self) -> str:
        return LEMANA_API_URLS[self.settings.environment]

    @property
    def auth_url(self) -> str:
        return LEMANA_AUTH_URL

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.enabled
            and self.settings.client_id.strip()
            and self._client_secret
            and self.settings.region_id is not None
        )

    def check_access(self) -> bool:
        self._get_token()
        return True

    def get_products(
        self,
        *,
        region_id: int | None = None,
        page: int = 1,
        per_page: int = _DEFAULT_PAGE_SIZE,
        if_modified_since: str | None = None,
    ) -> LemanaProductsPage | None:
        region = self._region(region_id)
        bounded_page = max(1, min(int(page), 100_000))
        bounded_per_page = max(1, min(int(per_page), _DEFAULT_PAGE_SIZE))
        query = urlencode(
            {"regionId": region, "page": bounded_page, "perPage": bounded_per_page}
        )
        headers = {}
        if if_modified_since:
            headers["If-Modified-Since"] = str(if_modified_since)
        try:
            payload = self._request_json(
                "GET", f"{LEMANA_PRODUCTS_PATH}?{query}", headers=headers
            )
        except LemanaNotModified:
            return None
        try:
            return parse_products_payload(
                payload, page=bounded_page, per_page=bounded_per_page
            )
        except ValueError as exc:
            raise SourcingProviderError(
                "Лемана ПРО B2B вернула некорректный каталог",
                category="invalid_response",
            ) from exc

    def get_prices(
        self,
        product_items: list[str] | tuple[str, ...],
        *,
        region_id: int | None = None,
    ) -> tuple[LemanaPriceRecord, ...]:
        """Fetch bounded regional prices through the documented batch endpoint."""

        region = self._region(region_id)
        normalized = []
        for item in product_items:
            value = str(item).strip()
            if value and value not in normalized:
                normalized.append(value)
        if len(normalized) > _DEFAULT_BATCH_SIZE:
            raise SourcingProviderError(
                "Слишком много товаров для одного запроса цен",
                category="invalid_request",
            )
        if not normalized:
            return ()
        payload = self._request_json(
            "POST",
            LEMANA_PRICE_BATCH_PATH,
            body={
                "limit": _DEFAULT_BATCH_SIZE,
                "offset": 0,
                "productItem": [self._json_product_item(item) for item in normalized],
                "regionId": region,
                "retailPrice": False,
            },
        )
        try:
            return parse_price_payload(payload)
        except ValueError as exc:
            raise SourcingProviderError(
                "Лемана ПРО B2B вернула некорректные цены",
                category="invalid_response",
            ) from exc

    def _region(self, region_id: int | None) -> int:
        value = self.settings.region_id if region_id is None else region_id
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SourcingProviderError(
                "Для Лемана ПРО B2B не указан регион",
                code="NOT_CONFIGURED",
                category="not_configured",
            )
        if not self.configured:
            raise SourcingProviderError(
                "Лемана ПРО B2B не настроен",
                code="NOT_CONFIGURED",
                category="not_configured",
            )
        return value

    @staticmethod
    def _json_product_item(value: str) -> int | str:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return value
        return parsed if parsed >= 0 else value

    def _get_token(self, *, force: bool = False) -> str:
        if not self.settings.enabled or not self.settings.client_id.strip() or not self._client_secret:
            raise SourcingProviderError(
                "Лемана ПРО B2B не настроен",
                code="NOT_CONFIGURED",
                category="not_configured",
            )
        now = self._clock()
        with self._token_lock:
            if not force and self._token is not None and self._token.expires_at > now:
                return self._token.access_token
            form = urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.settings.client_id,
                    "client_secret": self._client_secret,
                }
            ).encode("utf-8")
            try:
                payload = self._request_json(
                    "POST",
                    self.auth_url,
                    body_bytes=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth_request=True,
                )
            except SourcingProviderError:
                raise
            if not isinstance(payload, dict):
                raise SourcingProviderError(
                    "Лемана ПРО B2B не вернула токен доступа",
                    category="invalid_response",
                )
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            token_type = payload.get("token_type")
            if (
                not isinstance(token, str)
                or not token.strip()
                or isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))
                or expires_in <= 0
                or not isinstance(token_type, str)
                or token_type.casefold() != "bearer"
            ):
                raise SourcingProviderError(
                    "Лемана ПРО B2B вернула некорректный токен",
                    category="invalid_response",
                )
            safety_margin = min(_TOKEN_MARGIN_SECONDS, max(1.0, float(expires_in) * 0.1))
            self._token = _Token(token.strip(), now + float(expires_in) - safety_margin)
            return self._token.access_token

    def _invalidate_token(self, token: str) -> None:
        with self._token_lock:
            if self._token is not None and self._token.access_token == token:
                self._token = None

    def _request_json(
        self,
        method: str,
        url_or_path: str,
        *,
        body: dict[str, Any] | None = None,
        body_bytes: bytes | None = None,
        headers: dict[str, str] | None = None,
        auth_request: bool = False,
    ) -> Any:
        if body is not None and body_bytes is not None:
            raise ValueError("body and body_bytes are mutually exclusive")
        raw_headers = {"Accept": "application/json", **(headers or {})}
        payload = body_bytes
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            raw_headers.setdefault("Content-Type", "application/json")
        for attempt in range(2):
            token = None
            if not auth_request:
                token = self._get_token(force=attempt == 1)
                raw_headers["Authorization"] = f"Bearer {token}"
            try:
                status, raw = self._request_raw(
                    method,
                    url_or_path,
                    payload=payload,
                    headers=raw_headers,
                )
                if status == 304:
                    raise LemanaNotModified()
                return self._decode_json(raw)
            except SourcingProviderError as exc:
                if (
                    not auth_request
                    and attempt == 0
                    and exc.status_code == 401
                    and token is not None
                ):
                    self._invalidate_token(token)
                    continue
                raise
        raise SourcingProviderError(
            "Лемана ПРО B2B не вернула ответ",
            category="upstream_error",
        )

    def _request_raw(
        self,
        method: str,
        url_or_path: str,
        *,
        payload: bytes | None,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            url = url_or_path
        else:
            url = f"{self.api_base_url}{url_or_path}"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            response = self._transport(request, float(self.settings.request_timeout_s))
            try:
                status = getattr(response, "status", None) or getattr(response, "code", None)
                if not isinstance(status, int):
                    raise SourcingProviderError(
                        "Лемана ПРО B2B вернула ответ без HTTP-статуса",
                        category="invalid_response",
                    )
                if status == 304:
                    return status, b""
                if not 200 <= status < 300:
                    raise self._status_error(status)
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
                raise SourcingProviderError(
                    "Лемана ПРО B2B вернула слишком большой ответ",
                    category="invalid_response",
                )
            return status, raw
        except urllib.error.HTTPError as exc:
            raise self._status_error(int(exc.code)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                raise SourcingProviderError(
                    "Лемана ПРО B2B: истекло время ожидания",
                    category="timeout",
                ) from exc
            raise SourcingProviderError(
                "Лемана ПРО B2B: ошибка соединения",
                category="network",
            ) from exc
        except TimeoutError as exc:
            raise SourcingProviderError(
                "Лемана ПРО B2B: истекло время ожидания",
                category="timeout",
            ) from exc
        except OSError as exc:
            raise SourcingProviderError(
                "Лемана ПРО B2B: ошибка соединения",
                category="network",
            ) from exc

    @staticmethod
    def _status_error(status: int) -> SourcingProviderError:
        if status == 400:
            return SourcingProviderError(
                "Лемана ПРО B2B отклонила запрос",
                code="INVALID_REQUEST",
                category="invalid_request",
                status_code=status,
            )
        if status in {401, 403}:
            return SourcingProviderError(
                "Лемана ПРО B2B не разрешила доступ",
                code="ACCESS_DENIED",
                category="auth",
                status_code=status,
            )
        if status == 404:
            return SourcingProviderError(
                "Лемана ПРО B2B не нашла запрошенный ресурс",
                code="NOT_FOUND",
                category="upstream_error",
                status_code=status,
            )
        if status == 429:
            return SourcingProviderError(
                "Лемана ПРО B2B временно ограничила частоту запросов",
                code="RATE_LIMITED",
                category="rate_limited",
                status_code=status,
            )
        if status in {500, 523}:
            return SourcingProviderError(
                "Лемана ПРО B2B временно недоступна",
                code="UPSTREAM_ERROR",
                category="upstream_error",
                status_code=status,
            )
        return SourcingProviderError(
            "Лемана ПРО B2B вернула ошибку сервера",
            code="UPSTREAM_ERROR",
            category="upstream_error",
            status_code=status,
        )

    @staticmethod
    def _decode_json(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcingProviderError(
                "Лемана ПРО B2B вернула некорректный JSON",
                category="invalid_response",
            ) from exc
