from __future__ import annotations

import hashlib
import json
import ipaddress
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from averon_import.services.app_settings import EtmIproSettings
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.outbound import OutboundAttemptObserver

from .models import parse_manufacturers

ETM_API_URLS = {
    "prod": "https://ipro.etm.ru/api/v1",
    "test": "https://itest2.etm.ru/api/v1",
}
ETM_LOGIN_PATH = "/user/login"
ETM_MANUFACTURERS_PATH = "/info/search/r-manuf/"
ETM_CATALOG_JOB_CREATE_PATH = "/job/create/40029846"
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 1 * 1024 * 1024 * 1024
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024
_SESSION_LIFETIME_SECONDS = 8 * 60 * 60
_SESSION_MARGIN_SECONDS = 5 * 60
_LOGIN_INTERVAL_SECONDS = 120.0
_AUTH_QUARANTINE_SECONDS = 12 * 60 * 60
_AUTH_STATE_MAX_BYTES = 4096
_AUTH_QUARANTINE_MESSAGE = (
    "Повторная авторизация ЭТМ временно приостановлена, "
    "чтобы избежать автоматической блокировки поставщиком."
)
_ETM_OUTBOUND_GATE = threading.RLock()

Transport = Callable[[urllib.request.Request, float], Any]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _default_transport(request: urllib.request.Request, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class EtmRateLimiter:
    """Provider-owned monotonic limiter; callers never issue parallel ETM calls."""

    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._last_by_bucket: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, bucket: str) -> None:
        with self._lock:
            now = self._clock()
            previous = self._last_by_bucket.get(str(bucket))
            if previous is not None:
                wait = self.interval_seconds - (now - previous)
                if wait > 0:
                    self._sleeper(wait)
                    now = max(self._clock(), previous + self.interval_seconds)
            self._last_by_bucket[str(bucket)] = now


@dataclass(frozen=True)
class _Session:
    value: str
    expires_at: float


@dataclass(frozen=True)
class EtmSnapshotDownload:
    path: Path
    size_bytes: int
    sha256: str
    content_type: str
    content_length: int | None


class EtmIproClient:
    """Safe HTTP/session boundary for the documented ETM iPRO API."""

    def __init__(
        self,
        settings: EtmIproSettings,
        login: str | None,
        password: str | None,
        *,
        auth_state_path: str | Path,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        rate_limiter: EtmRateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self._login_value = str(login or "").strip()
        self._password = str(password or "").strip()
        self._auth_state_path = Path(auth_state_path)
        self._transport = transport or _default_transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._session: _Session | None = None
        self._last_login_at: float | None = None
        self._local_quarantine_until: float | None = None
        self._session_lock = threading.Lock()
        self.rate_limiter = rate_limiter or EtmRateLimiter(clock=clock, sleeper=sleeper)

    @property
    def api_base_url(self) -> str:
        return (self.settings.base_url_override or ETM_API_URLS[self.settings.environment]).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.settings.enabled and self._login_value and self._password)

    def check_access(
        self,
        *,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> bool:
        self._get_session(outbound_attempt_observer=outbound_attempt_observer)
        return True

    def get_goods(
        self,
        source_item_id: str,
        *,
        lookup_type: str = "etm",
        manufacturer_code: str | None = None,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> dict[str, Any]:
        item = self._id(source_item_id)
        params: dict[str, str] = {"type": lookup_type}
        if manufacturer_code:
            params["mnf"] = str(manufacturer_code).strip()
        return self._request_json(
            "GET",
            f"/goods/{quote(item, safe='')}",
            query=params,
            bucket="goods",
            outbound_attempt_observer=outbound_attempt_observer,
        )

    def get_prices(
        self,
        source_item_ids: list[str] | tuple[str, ...],
        *,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> dict[str, Any]:
        items = self._ids(source_item_ids, limit=50)
        if not items:
            return {"data": []}
        joined = quote(",".join(items), safe="")
        return self._request_json(
            "GET",
            f"/goods/{joined}/price",
            query={"type": "etm"},
            bucket="price",
            outbound_attempt_observer=outbound_attempt_observer,
        )

    def get_price(self, source_item_id: str) -> dict[str, Any]:
        return self.get_prices([source_item_id])

    def get_remains(
        self,
        source_item_id: str,
        *,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> dict[str, Any]:
        item = self._id(source_item_id)
        return self._request_json(
            "GET",
            f"/goods/{quote(item, safe='')}/remains",
            query={"type": "etm"},
            bucket="remains",
            outbound_attempt_observer=outbound_attempt_observer,
        )

    def get_manufacturers(self) -> tuple:
        payload = self._request_json("GET", ETM_MANUFACTURERS_PATH, bucket="manufacturer")
        try:
            return parse_manufacturers(payload)
        except ValueError as exc:
            raise SourcingProviderError(
                "ЭТМ iPRO вернул некорректный справочник производителей",
                code="INVALID_RESPONSE",
                category="invalid_response",
            ) from exc

    def create_catalog_job(self) -> str:
        payload = self._request_json("POST", ETM_CATALOG_JOB_CREATE_PATH)
        value = self._data_value(payload, "uuid")
        if not value:
            raise SourcingProviderError(
                "ЭТМ iPRO не вернул идентификатор синхронизации каталога",
                code="INVALID_RESPONSE",
                category="invalid_response",
            )
        return str(value).strip()

    def get_catalog_job(self, job_uuid: str) -> dict[str, Any]:
        value = self._id(job_uuid)
        return self._request_json("GET", f"/job/{quote(value, safe='')}", bucket="catalog")

    def download_snapshot(self, url: str) -> Any:
        safe_url = self._validate_snapshot_url(url)
        return self._request_json("GET", safe_url, auth=False, bucket="catalog")

    def download_snapshot_to_file(
        self,
        url: str,
        destination: str | Path,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> EtmSnapshotDownload:
        """Stream a completed SgGds snapshot without buffering it in memory."""

        with _ETM_OUTBOUND_GATE:
            return self._download_snapshot_to_file_unlocked(
                url, destination, progress=progress
            )

    def _download_snapshot_to_file_unlocked(
        self,
        url: str,
        destination: str | Path,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> EtmSnapshotDownload:

        safe_url = self._validate_snapshot_url(url)
        output_path = Path(destination)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        response = None
        try:
            request = urllib.request.Request(
                safe_url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            self.rate_limiter.acquire("catalog")
            try:
                response = self._transport(request, float(self.settings.request_timeout_s))
            except urllib.error.HTTPError as exc:
                raise self._status_error(int(exc.code)) from None
            status = getattr(response, "status", None) or getattr(response, "code", None)
            if not isinstance(status, int):
                raise SourcingProviderError(
                    "ЭТМ iPRO вернул ответ без HTTP-статуса",
                    category="invalid_response",
                )
            if not 200 <= status < 300:
                raise self._status_error(status)
            headers = getattr(response, "headers", {})
            raw_content_length = headers.get("Content-Length")
            content_length: int | None = None
            if raw_content_length not in (None, ""):
                try:
                    content_length = int(raw_content_length)
                except (TypeError, ValueError) as exc:
                    raise SourcingProviderError(
                        "ЭТМ iPRO вернул некорректный размер каталога",
                        code="INVALID_RESPONSE",
                        category="invalid_response",
                    ) from exc
                if content_length < 0:
                    raise SourcingProviderError(
                        "ЭТМ iPRO вернул некорректный размер каталога",
                        code="INVALID_RESPONSE",
                        category="invalid_response",
                    )
                if content_length > _MAX_SNAPSHOT_BYTES:
                    raise SourcingProviderError(
                        "Файл каталога ЭТМ iPRO превышает безопасный лимит",
                        code="SNAPSHOT_TOO_LARGE",
                        category="invalid_response",
                    )
            with output_path.open("wb") as output:
                if progress is not None:
                    progress(0, content_length or 0, "Загружаем каталог ЭТМ iPRO")
                while True:
                    chunk = response.read(_SNAPSHOT_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_SNAPSHOT_BYTES:
                        raise SourcingProviderError(
                            "Файл каталога ЭТМ iPRO превышает безопасный лимит",
                            code="SNAPSHOT_TOO_LARGE",
                            category="invalid_response",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(size, content_length or 0, "Загружаем каталог ЭТМ iPRO")
            return EtmSnapshotDownload(
                path=output_path,
                size_bytes=size,
                sha256=digest.hexdigest(),
                content_type=str(headers.get("Content-Type") or ""),
                content_length=content_length,
            )
        except SourcingProviderError:
            output_path.unlink(missing_ok=True)
            raise
        except (OSError, TimeoutError, ValueError) as exc:
            output_path.unlink(missing_ok=True)
            raise SourcingProviderError(
                "ЭТМ iPRO временно недоступен; повторите запрос позже",
                code="UPSTREAM_UNAVAILABLE",
                category="network",
            ) from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _validate_snapshot_url(self, url: str) -> str:
        safe_url = str(url or "").strip()
        parsed = urlsplit(safe_url)
        try:
            port = parsed.port
        except ValueError:
            port = None
            valid_port = False
        else:
            valid_port = port in {None, 443}
        allowed_hosts = {
            str(urlsplit(self.api_base_url).hostname or "").casefold(),
            "ipro.etm.ru",
        }
        hostname = str(parsed.hostname or "").casefold()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        private_or_local = (
            hostname in {"localhost", "localhost.localdomain"}
            or address is not None
            and (address.is_loopback or address.is_private or address.is_link_local or address.is_reserved or address.is_unspecified)
        )
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or hostname not in allowed_hosts
            or private_or_local
            or not valid_port
        ):
            raise SourcingProviderError(
                "ЭТМ iPRO не вернул безопасный адрес каталога",
                code="INVALID_RESPONSE",
                category="invalid_response",
            )
        return safe_url

    def _get_session(
        self,
        *,
        force: bool = False,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> str:
        # Keep lock ordering consistent with authenticated requests, which take
        # the process-wide outbound gate before touching this client's session
        # state. The gate is reentrant so login can use _request_raw safely.
        with _ETM_OUTBOUND_GATE:
            return self._resolve_session(
                force=force,
                outbound_attempt_observer=outbound_attempt_observer,
            )

    def _resolve_session(
        self,
        *,
        force: bool = False,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> str:
        if not self.configured:
            raise SourcingProviderError(
                "ЭТМ iPRO не настроен: укажите логин и пароль",
                code="NOT_CONFIGURED",
                category="not_configured",
            )
        with self._session_lock:
            now = self._clock()
            quarantine = self._load_auth_quarantine_locked()
            if quarantine is not None and quarantine > self._wall_clock():
                raise self._auth_quarantined_error()
            if not force and self._session is not None and self._session.expires_at > now:
                return self._session.value
            if (
                self._last_login_at is not None
                and now - self._last_login_at < _LOGIN_INTERVAL_SECONDS
            ):
                raise SourcingProviderError(
                    "ЭТМ iPRO временно ограничил обновление сессии; повторите позже",
                    code="AUTH_RATE_LIMITED",
                    category="rate_limit",
                )
            self.rate_limiter.acquire("auth")
            try:
                # Persist a closed circuit before login so a process crash or
                # restart during the request cannot immediately retry it.
                self._write_auth_quarantine_locked("login_attempt", None)
            except OSError:
                raise self._auth_state_unavailable_error() from None
            try:
                query = urlencode({"log": self._login_value, "pwd": self._password})
                payload = self._request_json(
                    "POST",
                    f"{ETM_LOGIN_PATH}?{query}",
                    auth=False,
                    bucket="auth",
                    outbound_attempt_observer=outbound_attempt_observer,
                )
                session = self._data_value(payload, "session")
                if not isinstance(session, str) or not session.strip():
                    raise SourcingProviderError(
                        "ЭТМ iPRO не вернул рабочую сессию",
                        code="INVALID_RESPONSE",
                        category="invalid_response",
                    )
            except SourcingProviderError as exc:
                status_code = getattr(exc, "status_code", None)
                category = getattr(exc, "category", "auth")
                safe_category = "network" if category == "network" else "auth"
                try:
                    self._write_auth_quarantine_locked(safe_category, status_code)
                except OSError:
                    # The pre-request record remains in place if this update
                    # fails; the request must still fail with a safe message.
                    pass
                self._session = None
                raise self._auth_failure_error(status_code=status_code) from None
            try:
                self._auth_state_path.unlink(missing_ok=True)
            except OSError:
                self._session = None
                self._local_quarantine_until = self._wall_clock() + _AUTH_QUARANTINE_SECONDS
                raise self._auth_state_unavailable_error() from None
            self._local_quarantine_until = None
            self._last_login_at = now
            self._session = _Session(
                str(session).strip(),
                now + _SESSION_LIFETIME_SECONDS - _SESSION_MARGIN_SECONDS,
            )
            return self._session.value

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        query: dict[str, Any] | None = None,
        auth: bool = True,
        bucket: str = "general",
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{self.api_base_url}{path_or_url}"
        headers = {"Accept": "application/json"}
        request_query = dict(query or {})
        session: str | None = None
        if auth:
            session = self._get_session(
                outbound_attempt_observer=outbound_attempt_observer,
            )
            request_query["session-id"] = session
        request_url = self._replace_query(url, request_query, authenticated=auth)
        try:
            status, raw = self._request_raw(
                method,
                request_url,
                headers=headers,
                bucket=bucket,
                authenticated=auth,
                outbound_attempt_observer=outbound_attempt_observer,
            )
            if not 200 <= status < 300:
                raise self._status_error(status)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourcingProviderError(
                    "ЭТМ iPRO вернул некорректный JSON",
                    code="INVALID_RESPONSE",
                    category="invalid_response",
                ) from exc
        except SourcingProviderError as exc:
            if auth and exc.status_code == 403:
                raise self._auth_quarantined_error(status_code=403) from None
            raise

    def _load_auth_quarantine_locked(self) -> float | None:
        now = self._wall_clock()
        if self._local_quarantine_until is not None and self._local_quarantine_until > now:
            return self._local_quarantine_until
        try:
            with self._auth_state_path.open("rb") as source:
                raw = source.read(_AUTH_STATE_MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise self._auth_state_unavailable_error() from None
        try:
            if len(raw) > _AUTH_STATE_MAX_BYTES:
                raise ValueError("state too large")
            state = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(state, dict)
                or type(state.get("version")) is not int
                or state.get("version") != 1
            ):
                raise ValueError("unsupported state")
            blocked_until = state.get("blocked_until")
            updated_at = state.get("updated_at")
            category = state.get("failure_category")
            status_code = state.get("status_code")
            if (
                isinstance(blocked_until, bool)
                or not isinstance(blocked_until, (int, float))
                or not math.isfinite(float(blocked_until))
                or isinstance(updated_at, bool)
                or not isinstance(updated_at, (int, float))
                or not math.isfinite(float(updated_at))
                or not isinstance(category, str)
                or category not in {"login_attempt", "auth", "network", "persisted_state_invalid"}
                or status_code is not None
                and (isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599)
            ):
                raise ValueError("invalid state fields")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            try:
                self._write_auth_quarantine_locked("persisted_state_invalid", None)
            except OSError:
                raise self._auth_state_unavailable_error() from None
            raise self._auth_quarantined_error() from None
        return float(blocked_until)

    def _write_auth_quarantine_locked(self, category: str, status_code: int | None) -> None:
        now = self._wall_clock()
        self._local_quarantine_until = now + _AUTH_QUARANTINE_SECONDS
        state = {
            "version": 1,
            "blocked_until": self._local_quarantine_until,
            "failure_category": category,
            "status_code": status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None,
            "updated_at": now,
        }
        path = self._auth_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary_path = output.name
                os.chmod(temporary_path, 0o600)
                json.dump(state, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)

    @staticmethod
    def _auth_quarantined_error(*, status_code: int | None = None) -> SourcingProviderError:
        return SourcingProviderError(
            _AUTH_QUARANTINE_MESSAGE,
            code="ETM_AUTH_QUARANTINED",
            category="auth",
            status_code=status_code,
        )

    @staticmethod
    def _auth_failure_error(*, status_code: int | None = None) -> SourcingProviderError:
        return SourcingProviderError(
            "Не удалось подтвердить авторизацию; автоматические повторные входы приостановлены "
            "на 12 часов, чтобы избежать временной блокировки поставщиком",
            code="ETM_AUTH_FAILURE",
            category="auth",
            status_code=status_code,
        )

    @staticmethod
    def _auth_state_unavailable_error() -> SourcingProviderError:
        return SourcingProviderError(
            "Не удалось безопасно сохранить состояние авторизации ЭТМ iPRO; запрос остановлен",
            code="ETM_AUTH_STATE_UNAVAILABLE",
            category="auth",
        )

    @staticmethod
    def _replace_query(url: str, query: dict[str, Any], *, authenticated: bool) -> str:
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if authenticated:
            pairs = [(key, value) for key, value in pairs if key != "session-id"]
        pairs.extend((str(key), str(value)) for key, value in query.items())
        if authenticated:
            session_values = [pair for pair in pairs if pair[0] == "session-id"]
            pairs = [(key, value) for key, value in pairs if key != "session-id"]
            if session_values:
                pairs.append(session_values[-1])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), parsed.fragment))

    def _request_raw(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        bucket: str,
        authenticated: bool = False,
        outbound_attempt_observer: OutboundAttemptObserver | None = None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=headers, method=method)
        if bucket not in {"auth", "general"}:
            self.rate_limiter.acquire(bucket)
        with _ETM_OUTBOUND_GATE:
            if authenticated:
                with self._session_lock:
                    quarantine = self._load_auth_quarantine_locked()
                if quarantine is not None and quarantine > self._wall_clock():
                    raise self._auth_quarantined_error()
            try:
                if outbound_attempt_observer is not None:
                    outbound_attempt_observer.record_outbound_attempt()
                response = self._transport(request, float(self.settings.request_timeout_s))
                try:
                    status = getattr(response, "status", None) or getattr(response, "code", None)
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                if not isinstance(status, int):
                    raise SourcingProviderError("ЭТМ iPRO вернул ответ без HTTP-статуса", category="invalid_response")
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise SourcingProviderError("Ответ ЭТМ iPRO превышает безопасный размер", category="invalid_response")
                if authenticated and status == 403:
                    self._quarantine_authenticated_403()
                    raise self._auth_quarantined_error(status_code=403)
                return status, raw
            except urllib.error.HTTPError as exc:
                if authenticated and int(exc.code) == 403:
                    self._quarantine_authenticated_403()
                    raise self._auth_quarantined_error(status_code=403) from None
                raise self._status_error(int(exc.code)) from None
            except SourcingProviderError:
                raise
            except (OSError, TimeoutError, ValueError) as exc:
                raise SourcingProviderError(
                    "ЭТМ iPRO временно недоступен; повторите запрос позже",
                    code="UPSTREAM_UNAVAILABLE",
                    category="network",
                ) from exc

    def _quarantine_authenticated_403(self) -> None:
        with self._session_lock:
            self._session = None
            self._local_quarantine_until = self._wall_clock() + _AUTH_QUARANTINE_SECONDS
            try:
                self._write_auth_quarantine_locked("auth", 403)
            except OSError:
                raise self._auth_state_unavailable_error() from None

    @staticmethod
    def _status_error(status: int) -> SourcingProviderError:
        if status == 403:
            message = "ЭТМ iPRO отклонил сессию; повторите запрос позже"
            category = "auth"
        elif status == 429:
            message, category = "ЭТМ iPRO ограничил частоту запросов; повторите позже", "rate_limit"
        elif 400 <= status < 500:
            message, category = "ЭТМ iPRO отклонил запрос", "invalid_request"
        else:
            message, category = "ЭТМ iPRO временно недоступен", "upstream_error"
        return SourcingProviderError(message, status_code=status, category=category)

    @staticmethod
    def _data_value(payload: Any, key: str) -> Any:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, dict) and key in data:
            return data[key]
        return payload.get(key)

    @staticmethod
    def _id(value: Any) -> str:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise SourcingProviderError("ЭТМ iPRO получил некорректный идентификатор", category="invalid_request")
        normalized = str(value).strip()
        if not normalized:
            raise SourcingProviderError("ЭТМ iPRO получил пустой идентификатор", category="invalid_request")
        return normalized

    @classmethod
    def _ids(cls, values, *, limit: int) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = cls._id(value)
            if normalized not in result:
                result.append(normalized)
        if len(result) > limit:
            raise SourcingProviderError(
                f"ЭТМ iPRO принимает не более {limit} товаров за запрос",
                category="invalid_request",
            )
        return result
