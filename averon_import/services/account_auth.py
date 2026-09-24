"""SQLite-backed app accounts and server-side authentication sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_ROLE_VALUES = ("admin", "user")


class AccountAuthError(Exception):
    """Base class for safe account repository errors."""


class DuplicateUsername(AccountAuthError):
    pass


class UserNotFound(AccountAuthError):
    pass


class UserAlreadyExists(AccountAuthError):
    pass


class StaleUserVersion(AccountAuthError):
    pass


class AccountDisabled(AccountAuthError):
    pass


def normalize_username(username: str) -> str:
    """Validate an office login and normalize only its ASCII letter case."""

    if not isinstance(username, str) or not _USERNAME_RE.fullmatch(username):
        raise ValueError("Логин должен содержать 3–64 символа: латинские буквы, цифры, точку, дефис или подчёркивание")
    return username.lower()


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError("Пароль должен содержать от 12 до 128 символов")


def _derive_password_key(password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    key = _derive_password_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return "scrypt$v1${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(key).decode("ascii"),
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Fail closed on malformed or unsupported password hash encodings."""

    try:
        if not isinstance(password, str) or not isinstance(encoded_hash, str):
            return False
        if len(password) > PASSWORD_MAX_LENGTH or len(encoded_hash) > 256:
            return False
        fields = encoded_hash.split("$")
        if len(fields) != 7 or fields[0] != "scrypt" or fields[1] != "v1":
            return False
        n, r, p = (int(fields[index]) for index in (2, 3, 4))
        if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = base64.b64decode(fields[5].encode("ascii"), altchars=b"-_", validate=True)
        expected = base64.b64decode(fields[6].encode("ascii"), altchars=b"-_", validate=True)
        if len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False
        candidate = _derive_password_key(password, salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError, UnicodeError, OverflowError, MemoryError):
        return False


# Precompute one process-wide dummy hash so missing accounts use the same scrypt
# work as a real password verification without delaying the first failed login.
_DUMMY_PASSWORD_HASH = hash_password("averon-dummy-password-not-a-user")


def dummy_password_verification(password: str) -> bool:
    candidate = password if isinstance(password, str) and len(password) <= PASSWORD_MAX_LENGTH else "invalid-login-password"
    verify_password(candidate, _DUMMY_PASSWORD_HASH)
    return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountRepository:
    """Account and session persistence using short-lived SQLite connections."""

    def __init__(self, auth_dir: Path):
        self.auth_dir = Path(auth_dir)
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.auth_dir / "auth.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._read_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise RuntimeError("Unsupported account authentication schema version")
            if version == 0:
                connection.executescript(
                    f"""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_norm TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    version INTEGER NOT NULL CHECK (version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_users_created
                    ON users(created_at, user_id);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    csrf_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                    ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
                    ON sessions(expires_at);
                PRAGMA user_version={SCHEMA_VERSION};
                COMMIT;"""
                )

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "role": row["role"],
            "enabled": bool(row["enabled"]),
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }

    def create_user(self, username: str, password: str, *, role: str = "user") -> dict[str, Any]:
        normalized = normalize_username(username)
        validate_password(password)
        if role not in _ROLE_VALUES:
            raise ValueError("Недопустимая роль пользователя")
        encoded = hash_password(password)
        now = _timestamp(_utc_now())
        user_id = uuid.uuid4().hex
        try:
            with self._transaction() as connection:
                connection.execute(
                    """INSERT INTO users (
                        user_id, username, username_norm, password_hash, role, enabled,
                        version, created_at, updated_at, last_login_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, NULL)""",
                    (user_id, normalized, normalized, encoded, role, now, now),
                )
                row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise DuplicateUsername(normalized) from exc
        return self._public(row)

    def bootstrap_admin(self, username: str, password: str, *, replace_existing: bool = False) -> dict[str, Any]:
        normalized = normalize_username(username)
        validate_password(password)
        encoded = hash_password(password)
        now = _timestamp(_utc_now())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE username_norm=?", (normalized,)).fetchone()
            if row is None:
                user_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO users (
                        user_id, username, username_norm, password_hash, role, enabled,
                        version, created_at, updated_at, last_login_at
                    ) VALUES (?, ?, ?, ?, 'admin', 1, 1, ?, ?, NULL)""",
                    (user_id, normalized, normalized, encoded, now, now),
                )
                row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            elif not replace_existing:
                raise UserAlreadyExists(normalized)
            else:
                connection.execute(
                    """UPDATE users SET password_hash=?, role='admin', enabled=1,
                        version=version+1, updated_at=? WHERE user_id=?""",
                    (encoded, now, row["user_id"]),
                )
                connection.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
                row = connection.execute("SELECT * FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
        return self._public(row)

    def get_auth_user(self, username: str) -> dict[str, Any] | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            return None
        with self._read_connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE username_norm=?", (normalized,)).fetchone()
        return dict(row) if row is not None else None

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return self._public(row) if row is not None else None

    def list_users(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """SELECT user_id, username, role, enabled, version, created_at, updated_at, last_login_at
                   FROM users ORDER BY created_at, user_id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [self._public(row) for row in rows]

    def update_enabled(self, user_id: str, *, enabled: bool, version: int) -> dict[str, Any]:
        now = _timestamp(_utc_now())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise UserNotFound(user_id)
            if int(row["version"]) != version:
                raise StaleUserVersion(user_id)
            connection.execute(
                "UPDATE users SET enabled=?, version=version+1, updated_at=? WHERE user_id=? AND version=?",
                (int(enabled), now, user_id, version),
            )
            if not enabled:
                connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return self._public(updated)

    def reset_password(self, user_id: str, password: str, *, version: int) -> dict[str, Any]:
        validate_password(password)
        encoded = hash_password(password)
        now = _timestamp(_utc_now())
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise UserNotFound(user_id)
            if int(row["version"]) != version:
                raise StaleUserVersion(user_id)
            connection.execute(
                "UPDATE users SET password_hash=?, version=version+1, updated_at=? WHERE user_id=? AND version=?",
                (encoded, now, user_id, version),
            )
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return self._public(updated)

    def delete_user(self, user_id: str, *, version: int) -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT version FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise UserNotFound(user_id)
            if int(row["version"]) != version:
                raise StaleUserVersion(user_id)
            connection.execute("DELETE FROM users WHERE user_id=? AND version=?", (user_id, version))

    def create_session(self, user_id: str, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> dict[str, str]:
        if not 60 <= ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("Session lifetime is outside allowed bounds")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = _utc_now()
        created_at = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=ttl_seconds))
        with self._transaction() as connection:
            user = connection.execute("SELECT enabled FROM users WHERE user_id=?", (user_id,)).fetchone()
            if user is None or not bool(user["enabled"]):
                raise AccountDisabled(user_id)
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (_token_digest(token), user_id, _token_digest(csrf_token), created_at, expires_at),
            )
            connection.execute("UPDATE users SET last_login_at=? WHERE user_id=?", (created_at, user_id))
        return {"token": token, "csrf_token": csrf_token, "expires_at": expires_at}

    def resolve_session(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        token_hash = _token_digest(token)
        with self._read_connection() as connection:
            row = connection.execute(
                """SELECT session.token_hash, session.csrf_hash, session.expires_at,
                          user.user_id, user.username, user.role, user.enabled
                   FROM sessions AS session JOIN users AS user ON user.user_id=session.user_id
                   WHERE session.token_hash=?""",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            expires_at = datetime.min.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not bool(row["enabled"]) or expires_at <= _utc_now():
            self.delete_session_hash(token_hash)
            return None
        return dict(row)

    def delete_session_hash(self, token_hash: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_session(self, token: str) -> None:
        if token:
            self.delete_session_hash(_token_digest(token))

    def session_count(self, user_id: str) -> int:
        with self._read_connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0])


__all__ = [
    "AccountAuthError",
    "AccountDisabled",
    "AccountRepository",
    "DEFAULT_SESSION_TTL_SECONDS",
    "DuplicateUsername",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "SCHEMA_VERSION",
    "SCRYPT_DKLEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "StaleUserVersion",
    "UserAlreadyExists",
    "UserNotFound",
    "dummy_password_verification",
    "hash_password",
    "normalize_username",
    "validate_password",
    "verify_password",
]
