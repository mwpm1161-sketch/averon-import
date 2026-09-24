from __future__ import annotations

import sqlite3
import threading

import pytest

from averon_import.services.account_auth import (
    AccountRepository,
    AdminAccountProtected,
    DuplicateUsername,
    LazyAccountRepository,
    StaleUserVersion,
    UserAlreadyExists,
    hash_password,
    normalize_username,
    validate_password,
    verify_password,
)


PASSWORD = "correct-horse-battery-staple"


def test_password_hashing_and_username_validation(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("Office.Admin", PASSWORD, role="admin")
    stored = repository.get_auth_user("OFFICE.ADMIN")

    assert normalize_username("Office.Admin") == "office.admin"
    assert stored is not None
    assert stored["password_hash"].startswith("scrypt$v1$")
    assert PASSWORD not in stored["password_hash"]
    assert verify_password(PASSWORD, stored["password_hash"])
    assert not verify_password("another-password", stored["password_hash"])
    assert not verify_password("x" * 129, stored["password_hash"])
    assert not verify_password(PASSWORD, "scrypt$v999$broken")
    assert "password_hash" not in user
    assert "password_hash" not in repository.list_users(limit=10, offset=0)[0]
    with pytest.raises(ValueError):
        normalize_username("bad username")
    with pytest.raises(ValueError):
        validate_password("short")
    with pytest.raises(ValueError):
        validate_password("x" * 129)


def test_accounts_normalize_duplicates_and_use_optimistic_versions(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("Colleague", PASSWORD)
    with pytest.raises(DuplicateUsername):
        repository.create_user("COLLEAGUE", PASSWORD)

    changed = repository.update_enabled(user["user_id"], enabled=False, version=user["version"])
    assert changed["enabled"] is False
    assert changed["version"] == user["version"] + 1
    with pytest.raises(StaleUserVersion):
        repository.update_enabled(user["user_id"], enabled=True, version=user["version"])

    reset = repository.reset_password(user["user_id"], "another-valid-password", version=changed["version"])
    stored = repository.get_auth_user("colleague")
    assert reset["version"] == changed["version"] + 1
    assert stored is not None
    assert verify_password("another-valid-password", stored["password_hash"])


def test_sessions_store_only_token_hashes_and_expire(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("colleague", PASSWORD)
    session = repository.create_session(user["user_id"])
    assert repository.resolve_session(session["token"])["user_id"] == user["user_id"]
    assert repository.session_count(user["user_id"]) == 1

    raw_database = repository.db_path.read_bytes()
    assert session["token"].encode() not in raw_database
    assert session["csrf_token"].encode() not in raw_database

    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    assert repository.resolve_session(session["token"]) is None
    assert repository.session_count(user["user_id"]) == 0


def test_disabling_and_deleting_users_revoke_sessions(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("colleague", PASSWORD)
    session = repository.create_session(user["user_id"])
    repository.update_enabled(user["user_id"], enabled=False, version=user["version"])
    assert repository.resolve_session(session["token"]) is None
    assert repository.session_count(user["user_id"]) == 0

    enabled = repository.update_enabled(user["user_id"], enabled=True, version=user["version"] + 1)
    next_session = repository.create_session(user["user_id"])
    reset = repository.reset_password(user["user_id"], "another-valid-password", version=enabled["version"])
    assert repository.resolve_session(next_session["token"]) is None
    assert repository.session_count(user["user_id"]) == 0
    final_session = repository.create_session(user["user_id"])
    repository.delete_user(user["user_id"], version=reset["version"])
    assert repository.resolve_session(final_session["token"]) is None
    assert repository.get_user(user["user_id"]) is None


def test_admin_accounts_are_protected_from_repository_management(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    admin = repository.bootstrap_admin("bootstrap-admin", PASSWORD)
    with pytest.raises(AdminAccountProtected):
        repository.update_enabled(admin["user_id"], enabled=False, version=admin["version"])
    with pytest.raises(AdminAccountProtected):
        repository.reset_password(admin["user_id"], "another-valid-password", version=admin["version"])
    with pytest.raises(AdminAccountProtected):
        repository.delete_user(admin["user_id"], version=admin["version"])
    assert repository.get_user(admin["user_id"])["enabled"] is True


@pytest.mark.parametrize("invalid_state", ["expired", "disabled"])
def test_invalid_session_cleanup_is_best_effort(tmp_path, monkeypatch, invalid_state):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("colleague", PASSWORD)
    session = repository.create_session(user["user_id"])
    with sqlite3.connect(repository.db_path) as connection:
        if invalid_state == "expired":
            connection.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        else:
            connection.execute("UPDATE users SET enabled=0 WHERE user_id=?", (user["user_id"],))

    def fail_cleanup(_token_hash):
        raise sqlite3.OperationalError("cleanup failed")

    monkeypatch.setattr(repository, "delete_session_hash", fail_cleanup)
    assert repository.resolve_session(session["token"]) is None


def test_expired_sessions_are_pruned_during_new_session_creation(tmp_path):
    repository = AccountRepository(tmp_path / "auth")
    user = repository.create_user("colleague", PASSWORD)
    old_session = repository.create_session(user["user_id"])
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")

    new_session = repository.create_session(user["user_id"])
    assert repository.resolve_session(new_session["token"]) is not None
    assert repository.resolve_session(old_session["token"]) is None
    assert repository.session_count(user["user_id"]) == 1


def test_lazy_account_store_does_not_initialize_until_first_operation():
    calls = []

    def fail_initialization():
        calls.append(True)
        raise sqlite3.OperationalError("auth store unavailable")

    repository = LazyAccountRepository(fail_initialization)
    assert repository.initialized is False
    assert calls == []
    with pytest.raises(sqlite3.OperationalError):
        repository.list_users(limit=10, offset=0)
    assert calls == [True]


def test_bootstrap_admin_requires_explicit_replacement(tmp_path, monkeypatch, capsys):
    from averon_import import auth_cli

    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda _prompt: next(answers))
    args = ["bootstrap-admin", "--username", "first-admin", "--data-dir", str(tmp_path)]

    assert auth_cli.main(args) == 0
    assert PASSWORD not in capsys.readouterr().out

    answers = iter(["another-valid-password", "another-valid-password"])
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda _prompt: next(answers))
    assert auth_cli.main(args) == 2
    assert "--reset-existing" in capsys.readouterr().err

    answers = iter(["another-valid-password", "another-valid-password"])
    monkeypatch.setattr(auth_cli.getpass, "getpass", lambda _prompt: next(answers))
    assert auth_cli.main(args + ["--reset-existing"]) == 0
    repository = AccountRepository(tmp_path / "auth")
    stored = repository.get_auth_user("first-admin")
    assert stored is not None
    assert verify_password("another-valid-password", stored["password_hash"])


@pytest.fixture
def session_api(monkeypatch, tmp_path):
    from averon_import import main
    from averon_import.services import auth
    from averon_import.services.auth import LoginRateLimiter
    from test_auth_roles import ApiClient

    repository = AccountRepository(tmp_path / "auth")
    assert repository.list_users(limit=10, offset=0) == []
    limiter = LoginRateLimiter()
    monkeypatch.setattr(main, "auth_repository", repository)
    monkeypatch.setattr(main, "login_rate_limiter", limiter)
    monkeypatch.setattr(auth, "_account_repository", repository)
    monkeypatch.setattr(auth, "login_rate_limiter", limiter)
    monkeypatch.setenv("AVERON_AUTH_MODE", "session")
    monkeypatch.delenv("AVERON_PROXY_SECRET", raising=False)
    return ApiClient(main.app), repository


def _cookie(response, name):
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            text = value.decode("latin-1")
            if text.startswith(name + "="):
                return text.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"missing {name} cookie")


def _session_headers(token, csrf=None):
    headers = {"cookie": f"averon_session={token}"}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


def test_session_login_generic_errors_cookies_csrf_and_logout(session_api, monkeypatch):
    from averon_import import main

    client, repository = session_api
    user = repository.bootstrap_admin("admin-user", PASSWORD)
    dummy_passwords = []
    monkeypatch.setattr(main, "dummy_password_verification", lambda password: dummy_passwords.append(password) or False)

    missing = client.post("/api/auth/login", json={"username": "not-found", "password": PASSWORD})
    wrong = client.post("/api/auth/login", json={"username": "admin-user", "password": "incorrect-password"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json() == {"detail": "Неверный логин или пароль"}
    assert dummy_passwords == [PASSWORD]

    login = client.post("/api/auth/login", json={"username": "ADMIN-USER", "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["user"] == {
        "username": "admin-user",
        "role": "admin",
        "capabilities": {
            "settings": True,
            "provider_maintenance": True,
            "admin_reports": True,
            "user_management": True,
        },
    }
    token = _cookie(login, "averon_session")
    csrf = _cookie(login, "averon_csrf")
    cookie_headers = [value.decode("latin-1") for key, value in login.raw_headers if key.lower() == b"set-cookie"]
    assert any("HttpOnly" in value and "Secure" in value and "SameSite=strict" in value for value in cookie_headers if value.startswith("averon_session="))
    assert any("Secure" in value and "SameSite=strict" in value and "HttpOnly" not in value for value in cookie_headers if value.startswith("averon_csrf="))

    me = client.get("/api/me", headers=_session_headers(token))
    assert me.status_code == 200
    assert me.json()["capabilities"]["user_management"] is True
    rejected = client.post("/api/admin/users", headers=_session_headers(token), json={"username": "colleague", "password": PASSWORD})
    assert rejected.status_code == 403
    for method, path, payload in (
        ("POST", "/api/admin/users", {"username": "blocked", "password": PASSWORD}),
        ("PUT", "/api/settings", {"processing_mode": "local"}),
        ("PATCH", f"/api/admin/users/{user['user_id']}", {"enabled": False, "version": user["version"]}),
        ("DELETE", f"/api/admin/users/{user['user_id']}", {"version": user["version"]}),
    ):
        wrong_csrf = client.request(
            method,
            path,
            headers=_session_headers(token, "wrong-csrf-token"),
            json=payload,
        )
        assert wrong_csrf.status_code == 403, (method, path, wrong_csrf.text)

    created = client.post(
        "/api/admin/users",
        headers=_session_headers(token, csrf),
        json={"username": "colleague", "password": PASSWORD},
    )
    assert created.status_code == 201
    new_user = created.json()["user"]
    assert new_user["role"] == "user"
    assert "password_hash" not in created.text
    assert "password" not in created.text

    escalation = client.post(
        "/api/admin/users",
        headers=_session_headers(token, csrf),
        json={"username": "second-admin", "password": PASSWORD, "role": "admin"},
    )
    assert escalation.status_code == 400
    assert repository.get_auth_user("second-admin") is None

    logout = client.post("/api/auth/logout", headers=_session_headers(token, csrf))
    assert logout.status_code == 204
    cleared = [value.decode("latin-1") for key, value in logout.raw_headers if key.lower() == b"set-cookie"]
    assert any(value.startswith("averon_session=") and "Max-Age=0" in value for value in cleared)
    assert any(value.startswith("averon_csrf=") and "Max-Age=0" in value for value in cleared)
    assert repository.resolve_session(token) is None
    assert client.get("/api/me", headers=_session_headers(token)).status_code == 401


def test_user_management_versions_and_self_protection(session_api):
    client, repository = session_api
    admin = repository.bootstrap_admin("admin-user", PASSWORD)
    other_admin = repository.bootstrap_admin("another-admin", PASSWORD)
    admin_session = repository.create_session(admin["user_id"])
    headers = _session_headers(admin_session["token"], admin_session["csrf_token"])

    listed = client.get("/api/admin/users", headers=_session_headers(admin_session["token"]))
    assert listed.status_code == 200
    assert "password_hash" not in listed.text

    for method, path, payload in (
        ("patch", f"/api/admin/users/{admin['user_id']}", {"enabled": False, "version": admin["version"]}),
        ("post", f"/api/admin/users/{admin['user_id']}/reset-password", {"password": "another-valid-password", "version": admin["version"]}),
        ("delete", f"/api/admin/users/{admin['user_id']}", {"version": admin["version"]}),
    ):
        response = client.request(method.upper(), path, headers=headers, json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "SELF_PROTECTION"

    for method, path, payload in (
        ("PATCH", f"/api/admin/users/{other_admin['user_id']}", {"enabled": False, "version": other_admin["version"]}),
        ("POST", f"/api/admin/users/{other_admin['user_id']}/reset-password", {"password": "third-valid-password", "version": other_admin["version"]}),
        ("DELETE", f"/api/admin/users/{other_admin['user_id']}", {"version": other_admin["version"]}),
    ):
        response = client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ADMIN_ACCOUNT_PROTECTED"
    assert repository.get_user(other_admin["user_id"])["enabled"] is True

    colleague = repository.create_user("colleague", PASSWORD)
    disabled = client.request(
        "PATCH",
        f"/api/admin/users/{colleague['user_id']}",
        headers=headers,
        json={"enabled": False, "version": colleague["version"]},
    )
    assert disabled.status_code == 200
    user_reset = client.post(
        f"/api/admin/users/{colleague['user_id']}/reset-password",
        headers=headers,
        json={"password": "another-valid-password", "version": disabled.json()["user"]["version"]},
    )
    assert user_reset.status_code == 200
    assert user_reset.json()["user"]["role"] == "user"
    stale_delete = client.delete(
        f"/api/admin/users/{colleague['user_id']}",
        headers=headers,
        json={"version": colleague["version"]},
    )
    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["code"] == "STALE_USER_VERSION"
    current_delete = client.delete(
        f"/api/admin/users/{colleague['user_id']}",
        headers=headers,
        json={"version": user_reset.json()["user"]["version"]},
    )
    assert current_delete.status_code == 204
    assert repository.get_user(colleague["user_id"]) is None


def test_user_role_cannot_call_management_api_in_session_mode(session_api):
    client, repository = session_api
    user = repository.create_user("colleague", PASSWORD)
    session = repository.create_session(user["user_id"])
    response = client.get("/api/admin/users", headers=_session_headers(session["token"]))
    assert response.status_code == 403


def test_session_login_rate_limit_is_bounded_and_generic(session_api):
    client, _repository = session_api
    for _ in range(5):
        response = client.post("/api/auth/login", json={"username": "unknown-user", "password": PASSWORD})
        assert response.status_code == 401
    limited = client.post("/api/auth/login", json={"username": "unknown-user", "password": PASSWORD})
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Неверный логин или пароль"}
    assert int(limited.headers["retry-after"]) > 0


def test_saturated_login_work_guard_returns_generic_nonblocking_429(session_api, monkeypatch):
    from averon_import import main
    from averon_import.services.auth import LoginWorkGuard

    client, repository = session_api
    repository.bootstrap_admin("admin-user", PASSWORD)
    guard = LoginWorkGuard(max_concurrent=1)
    assert guard.try_acquire() is True
    monkeypatch.setattr(main, "login_work_guard", guard)
    response = client.post("/api/auth/login", json={"username": "arbitrary-name", "password": PASSWORD})
    guard.release()

    assert response.status_code == 429
    assert response.json() == {"detail": "Неверный логин или пароль"}
    assert response.headers["retry-after"] == "1"


def test_login_hashing_and_sqlite_work_run_in_worker_thread(session_api, monkeypatch):
    from averon_import import main

    client, repository = session_api
    repository.bootstrap_admin("admin-user", PASSWORD)
    event_loop_thread = threading.get_ident()
    observed_threads = []

    original_lookup = repository.get_auth_user
    original_verify = main.verify_password
    original_create_session = repository.create_session

    def track_lookup(username):
        observed_threads.append(threading.get_ident())
        return original_lookup(username)

    def track_verify(password, encoded_hash):
        observed_threads.append(threading.get_ident())
        return original_verify(password, encoded_hash)

    def track_create_session(user_id):
        observed_threads.append(threading.get_ident())
        return original_create_session(user_id)

    monkeypatch.setattr(repository, "get_auth_user", track_lookup)
    monkeypatch.setattr(main, "verify_password", track_verify)
    monkeypatch.setattr(repository, "create_session", track_create_session)
    response = client.post("/api/auth/login", json={"username": "admin-user", "password": PASSWORD})

    assert response.status_code == 200
    assert observed_threads
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


def test_async_account_create_reset_update_delete_run_in_worker_threads(session_api, monkeypatch):
    from averon_import import main

    client, repository = session_api
    admin = repository.bootstrap_admin("admin-user", PASSWORD)
    admin_session = repository.create_session(admin["user_id"])
    headers = _session_headers(admin_session["token"], admin_session["csrf_token"])
    event_loop_thread = threading.get_ident()
    observed_threads = []

    def track_method(method_name):
        original = getattr(repository, method_name)

        def tracked(*args, **kwargs):
            observed_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(repository, method_name, tracked)

    track_method("create_user")
    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={"username": "colleague", "password": PASSWORD},
    )
    assert created.status_code == 201
    user = created.json()["user"]

    track_method("reset_password")
    reset = client.post(
        f"/api/admin/users/{user['user_id']}/reset-password",
        headers=headers,
        json={"password": "another-valid-password", "version": user["version"]},
    )
    assert reset.status_code == 200

    track_method("update_enabled")
    disabled = client.request(
        "PATCH",
        f"/api/admin/users/{user['user_id']}",
        headers=headers,
        json={"enabled": False, "version": reset.json()["user"]["version"]},
    )
    assert disabled.status_code == 200

    track_method("delete_user")
    deleted = client.delete(
        f"/api/admin/users/{user['user_id']}",
        headers=headers,
        json={"version": disabled.json()["user"]["version"]},
    )
    assert deleted.status_code == 204
    assert len(observed_threads) == 4
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


def test_trusted_proxy_self_protection_lookup_runs_in_worker_thread(session_api, monkeypatch):
    from averon_import import main
    from test_auth_roles import PROXY_SECRET, _headers

    client, repository = session_api
    admin = repository.bootstrap_admin("admin-user", PASSWORD)
    event_loop_thread = threading.get_ident()
    observed_threads = []
    original_lookup = repository.get_user

    def track_lookup(user_id):
        observed_threads.append(threading.get_ident())
        return original_lookup(user_id)

    monkeypatch.setattr(repository, "get_user", track_lookup)
    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("AVERON_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AVERON_ADMIN_USERS", "admin-user")
    response = client.request(
        "PATCH",
        f"/api/admin/users/{admin['user_id']}",
        headers=_headers("admin-user"),
        json={"enabled": False, "version": admin["version"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SELF_PROTECTION"
    assert observed_threads
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


def test_lazy_account_store_initializes_in_worker_thread(session_api, monkeypatch):
    from averon_import import main
    from test_auth_roles import PROXY_SECRET, _headers

    client, repository = session_api
    event_loop_thread = threading.get_ident()
    initialized_threads = []

    def initialize_store():
        initialized_threads.append(threading.get_ident())
        return repository

    lazy_repository = LazyAccountRepository(initialize_store)
    monkeypatch.setattr(main, "auth_repository", lazy_repository)
    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("AVERON_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AVERON_ADMIN_USERS", "admin-user")
    response = client.post(
        "/api/admin/users",
        headers=_headers("admin-user"),
        json={"username": "colleague", "password": PASSWORD},
    )

    assert response.status_code == 201
    assert lazy_repository.initialized is True
    assert len(initialized_threads) == 1
    assert initialized_threads[0] != event_loop_thread


def test_broken_account_store_does_not_break_rollback_modes(session_api, monkeypatch):
    from averon_import import main
    from averon_import.services import auth
    from test_auth_roles import PROXY_SECRET, _headers

    client, _repository = session_api
    attempts = []

    def unavailable_store():
        attempts.append(True)
        raise sqlite3.OperationalError("private auth.sqlite3 path")

    broken_store = LazyAccountRepository(unavailable_store)
    monkeypatch.setattr(main, "auth_repository", broken_store)
    monkeypatch.setattr(auth, "_account_repository", broken_store)
    monkeypatch.setenv("AVERON_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AVERON_ADMIN_USERS", "admin-user")

    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    trusted_me = client.get("/api/me", headers=_headers("admin-user"))
    assert trusted_me.status_code == 200
    assert attempts == []
    account_api = client.get("/api/admin/users", headers=_headers("admin-user"))
    assert account_api.status_code == 503
    assert "private auth.sqlite3 path" not in account_api.text

    monkeypatch.setenv("AVERON_AUTH_MODE", "local_dev")
    local_me = client.get("/api/me", headers={"host": "localhost:8765"})
    assert local_me.status_code == 200

    monkeypatch.setenv("AVERON_AUTH_MODE", "session")
    session_me = client.get(
        "/api/me",
        headers={"cookie": "averon_session=opaque-token", **_headers("admin-user")},
    )
    assert session_me.status_code == 503
    assert "private auth.sqlite3 path" not in session_me.text
    assert attempts == [True, True]


def test_login_rate_limiter_bounds_buckets_and_success_clears():
    from averon_import.services.auth import LoginRateLimiter, LoginWorkGuard

    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60, block_seconds=30, max_buckets=2)
    limiter.failed("first", now=100)
    limiter.failed("first", now=101)
    assert limiter.retry_after("first", now=102) == 29
    limiter.failed("second", now=102)
    limiter.failed("third", now=103)
    assert len(limiter._buckets) == 2
    limiter.succeeded("third")
    assert limiter.retry_after("third", now=104) == 0

    guard = LoginWorkGuard(max_concurrent=2)
    assert guard.try_acquire() is True
    assert guard.try_acquire() is True
    assert guard.try_acquire() is False
    guard.release()
    assert guard.try_acquire() is True
    guard.release()
    guard.release()


def test_disabled_account_login_uses_generic_error(session_api):
    client, repository = session_api
    user = repository.create_user("disabled-user", PASSWORD)
    repository.update_enabled(user["user_id"], enabled=False, version=user["version"])
    response = client.post("/api/auth/login", json={"username": "disabled-user", "password": PASSWORD})
    assert response.status_code == 401
    assert response.json() == {"detail": "Неверный логин или пароль"}


def test_unsupported_auth_mode_fails_closed(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from averon_import.services.auth import resolve_current_user

    monkeypatch.setenv("AVERON_AUTH_MODE", "unknown-mode")
    request = Request({"type": "http", "method": "GET", "headers": [], "client": ("127.0.0.1", 80)})
    with pytest.raises(HTTPException) as error:
        resolve_current_user(request)
    assert error.value.status_code == 503
