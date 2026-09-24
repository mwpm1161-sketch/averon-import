"""Trusted-proxy authentication and application roles.

The public authentication challenge is owned by Caddy.  The application
accepts a username only together with a server-side proxy assertion.  This
keeps role decisions out of endpoint/UI code and fails closed when the
production proxy contract is missing or invalid.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, Request

from averon_import.services.account_auth import AccountRepository, normalize_username

logger = logging.getLogger(__name__)


class Role(str, Enum):
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    username: str
    role: Role
    user_id: str | None = None

    @property
    def capabilities(self) -> dict[str, bool]:
        is_admin = self.role is Role.ADMIN
        return {
            "settings": is_admin,
            "provider_maintenance": is_admin,
            "admin_reports": is_admin,
            "user_management": is_admin,
        }

    def public(self) -> dict[str, object]:
        return {
            "username": self.username,
            "role": self.role.value,
            "capabilities": dict(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class AuthConfig:
    mode: str
    proxy_secret: str
    admin_users: frozenset[str]
    dev_user: str


_account_repository: AccountRepository | None = None


def configure_account_repository(repository: AccountRepository) -> None:
    global _account_repository
    _account_repository = repository


class LoginRateLimiter:
    """Bounded in-process failed-login limiter keyed by username digest."""

    def __init__(self, *, max_attempts: int = 5, window_seconds: int = 900, block_seconds: int = 900, max_buckets: int = 4096):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, tuple[deque[float], float]] = OrderedDict()
        self._lock = threading.Lock()

    def _bucket(self, key: str, now: float) -> tuple[deque[float], float]:
        attempts, blocked_until = self._buckets.get(key, (deque(), 0.0))
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        self._buckets[key] = (attempts, blocked_until)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)
        return attempts, blocked_until

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            _, blocked_until = self._bucket(key, current)
            return max(0, int(blocked_until - current + 0.999))

    def failed(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            attempts, blocked_until = self._bucket(key, current)
            attempts.append(current)
            if len(attempts) >= self.max_attempts:
                blocked_until = current + self.block_seconds
            self._buckets[key] = (attempts, blocked_until)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


login_rate_limiter = LoginRateLimiter()


def login_bucket_key(username: object) -> str:
    raw = username if isinstance(username, str) else ""
    try:
        normalized = normalize_username(raw)
    except ValueError:
        normalized = raw.casefold()[:256]
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


_USERNAME_RE = re.compile(r"^[^\x00-\x1f\x7f\s]{1,128}$")


def auth_config() -> AuthConfig:
    """Read auth configuration without providing a production fail-open default."""

    mode = os.environ.get("AVERON_AUTH_MODE", "trusted_proxy").strip().casefold()
    admin_users = frozenset(
        item.strip().casefold()
        for item in os.environ.get("AVERON_ADMIN_USERS", "").split(",")
        if item.strip()
    )
    dev_user = os.environ.get("AVERON_DEV_USER", "local-admin").strip()
    return AuthConfig(
        mode=mode or "trusted_proxy",
        proxy_secret=os.environ.get("AVERON_PROXY_SECRET", ""),
        admin_users=admin_users,
        dev_user=dev_user or "local-admin",
    )


def _auth_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _valid_username(value: str) -> bool:
    return bool(_USERNAME_RE.fullmatch(value))


def _token_matches(candidate: str, expected: str) -> bool:
    """Compare a fixed-size digest so the secret comparison is constant-time."""

    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_digest, expected_digest)


def _trusted_proxy_user(request: Request, config: AuthConfig) -> CurrentUser:
    if not config.proxy_secret:
        raise _auth_error(503, "Сервис аутентификации не настроен")

    proxy_assertion = request.headers.get("X-Averon-Proxy", "")
    if not _token_matches(proxy_assertion, config.proxy_secret):
        raise _auth_error(401, "Требуется аутентификация")

    username = request.headers.get("X-Averon-User", "").strip()
    if not _valid_username(username):
        raise _auth_error(401, "Требуется аутентификация")

    role = (
        Role.ADMIN
        if username.casefold() in config.admin_users
        else Role.USER
    )
    return CurrentUser(username=username, role=role)


def _local_dev_user(request: Request, config: AuthConfig) -> CurrentUser:
    # local_dev is never selected implicitly.  It is enabled only by an
    # explicit development setting (the Windows developer launcher sets it).
    if config.mode != "local_dev":
        raise _auth_error(503, "Сервис аутентификации не настроен")
    if not _valid_username(config.dev_user):
        raise _auth_error(503, "Локальный режим аутентификации не настроен")
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise _auth_error(401, "Требуется аутентификация")
    host_header = request.headers.get("host", "").strip().casefold()
    if host_header.startswith("["):
        closing = host_header.find("]")
        hostname = host_header[1:closing] if closing >= 0 else ""
        port = host_header[closing + 1:] if closing >= 0 else "invalid"
        if port and (not port.startswith(":") or not port[1:].isdigit()):
            hostname = ""
    elif host_header.count(":") == 1:
        hostname, port = host_header.rsplit(":", 1)
        if not port.isdigit():
            hostname = ""
    else:
        hostname = host_header
    if hostname.rstrip(".") not in {"localhost", "127.0.0.1", "::1"}:
        raise _auth_error(401, "Требуется аутентификация")
    return CurrentUser(username=config.dev_user, role=Role.ADMIN)


def resolve_current_user(request: Request) -> CurrentUser:
    config = auth_config()
    if config.mode == "local_dev":
        return _local_dev_user(request, config)
    if config.mode == "trusted_proxy":
        return _trusted_proxy_user(request, config)
    if config.mode == "session":
        if _account_repository is None:
            raise _auth_error(503, "Сервис аутентификации не настроен")
        token = request.cookies.get("averon_session", "")
        try:
            session = _account_repository.resolve_session(token)
        except Exception as exc:
            logger.exception("Session lookup failed")
            raise _auth_error(503, "Сервис аутентификации временно недоступен") from exc
        if session is None:
            raise _auth_error(401, "Требуется аутентификация")
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            submitted_csrf = request.headers.get("X-CSRF-Token", "")
            if len(submitted_csrf) > 128:
                raise _auth_error(403, "Проверка запроса не пройдена")
            submitted_hash = hashlib.sha256(submitted_csrf.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(submitted_hash, session["csrf_hash"]):
                raise _auth_error(403, "Проверка запроса не пройдена")
        try:
            role = Role(session["role"])
        except (TypeError, ValueError) as exc:
            logger.error("Unsupported stored account role")
            raise _auth_error(503, "Сервис аутентификации временно недоступен") from exc
        return CurrentUser(username=session["username"], role=role, user_id=session["user_id"])
    raise _auth_error(503, "Сервис аутентификации не настроен")


def get_current_user(request: Request) -> CurrentUser:
    return resolve_current_user(request)


def require_authenticated(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return user


def require_admin(user: CurrentUser = Depends(require_authenticated)) -> CurrentUser:
    if user.role is not Role.ADMIN:
        raise _auth_error(403, "Недостаточно прав")
    return user


__all__ = [
    "AuthConfig",
    "CurrentUser",
    "Role",
    "auth_config",
    "configure_account_repository",
    "get_current_user",
    "login_bucket_key",
    "login_rate_limiter",
    "require_admin",
    "require_authenticated",
    "resolve_current_user",
]
