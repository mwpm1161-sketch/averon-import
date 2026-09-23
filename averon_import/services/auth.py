"""Trusted-proxy authentication and application roles.

The public authentication challenge is owned by Caddy.  The application
accepts a username only together with a server-side proxy assertion.  This
keeps role decisions out of endpoint/UI code and fails closed when the
production proxy contract is missing or invalid.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, Request


class Role(str, Enum):
    ADMIN = "admin"
    USER = "user"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    username: str
    role: Role

    @property
    def capabilities(self) -> dict[str, bool]:
        is_admin = self.role is Role.ADMIN
        return {
            "settings": is_admin,
            "provider_maintenance": is_admin,
            "admin_reports": is_admin,
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
    headers = {"WWW-Authenticate": "Basic"} if status_code == 401 else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


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
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise _auth_error(401, "Требуется аутентификация")
    return CurrentUser(username=config.dev_user, role=Role.ADMIN)


def resolve_current_user(request: Request) -> CurrentUser:
    config = auth_config()
    if config.mode == "local_dev":
        return _local_dev_user(request, config)
    if config.mode != "trusted_proxy":
        raise _auth_error(503, "Сервис аутентификации не настроен")
    return _trusted_proxy_user(request, config)


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
    "get_current_user",
    "require_admin",
    "require_authenticated",
    "resolve_current_user",
]
