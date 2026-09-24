from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    start = re.search(rf"(?m)^(?:async )?function {re.escape(name)}\(", APP_JS)
    assert start
    tail = APP_JS[start.end() :]
    endings = [match.start() for pattern in (
        rf"(?m)^(?:async )?function {re.escape(next_name)}\(",
        rf"(?m)^const {re.escape(next_name)}\b",
    ) if (match := re.search(pattern, tail))]
    assert endings
    return APP_JS[start.start() : start.end() + min(endings)]


def test_app_is_hidden_until_authoritative_identity_and_handles_initial_auth_states():
    boot = _function("boot", "updateCloudStatus")
    assert '<div class="app-shell" id="app-shell" hidden>' in HTML
    assert 'id="auth-check-screen"' in HTML
    assert 'id="auth-login-screen"' in HTML and 'id="auth-unavailable-screen"' in HTML
    assert 'const currentUser = await api("/api/me")' in boot
    assert 'currentUser?.auth_mode' in boot
    assert 'state.authState = "authenticated"' in boot
    assert 'error.status === 401) returnToLogin(' in boot
    assert "else showUnavailableScreen()" in boot
    assert '$("#app-shell").hidden = false' in boot
    assert boot.count('api("/api/me")') == 1
    assert "authBootPromise" in boot and "bootComplete" in boot


def test_csrf_is_central_for_mutations_and_preserves_callers_and_form_data():
    api = _function("api", "readCsrfCookie")
    csrf = _function("readCsrfCookie", "clearProtectedMemory")
    assert '["POST", "PUT", "PATCH", "DELETE"].includes(method)' in api
    assert 'new Headers(options.headers || {})' in api
    assert 'if (csrfToken && !headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", csrfToken)' in api
    assert 'if (!value.startsWith("averon_csrf=")) continue' in csrf
    assert 'requestOptions.headers = headers' in api
    assert 'fetch(url, requestOptions)' in api
    assert 'requestOptions.body' not in api
    assert "Content-Type" not in api
    assert "averon_session" not in APP_JS
    assert not re.search(r"(?:localStorage|sessionStorage)\.(?:getItem|setItem)\([^\n]*(?:csrf|session)", APP_JS, re.I)


def test_session_401_transitions_once_clears_protected_state_and_does_not_catch_login_401():
    api = _function("api", "readCsrfCookie")
    expired = _function("handleSessionExpired", "authLoginStatus")
    return_login = _function("returnToLogin", "handleSessionExpired")
    clear = _function("clearProtectedMemory", "showCheckingScreen")
    assert 'response.status === 401 && url !== "/api/auth/login"' in api
    assert "handleSessionExpired()" in api
    assert 'state.authState !== "authenticated" || state.authMode !== "session"' in expired
    assert 'returnToLogin("Сессия завершена. Войдите снова.")' in expired
    assert "clearProtectedMemory();" in return_login
    assert '$("#app-shell").hidden = true' in _function("showLoginScreen", "showUnavailableScreen")
    for protected in ("state.rows = []", "state.document = null", "state.settings = null", "state.currentUser = null", "state.support.pendingIncident = null"):
        assert protected in clear
    assert "clearProtectedUi();" in clear
    assert '$("#result-body").replaceChildren()' in clear
    assert '$("#users-list").replaceChildren()' in clear
    assert "response.status === 403" not in api


def test_login_and_logout_use_session_endpoints_without_persisting_credentials():
    login = _function("submitAuthLogin", "logoutSession")
    logout = _function("logoutSession", "accountErrorMessage")
    assert 'api("/api/auth/login"' in login
    assert 'body: JSON.stringify({username, password})' in login
    assert 'await boot()' in login
    assert 'error.status === 401) authLoginStatus("Неверный логин или пароль.")' in login
    assert 'error.status === 429) authLoginStatus("Слишком много попыток входа. Попробуйте немного позже.")' in login
    assert 'error.status === 503' in login and "Сервис авторизации временно недоступен" in login
    assert 'state.authMode !== "session"' in logout
    assert 'api("/api/auth/logout", {method:"POST"})' in logout
    assert 'returnToLogin("Вы вышли из системы.")' in logout
    assert '$("#logout-button").hidden = authMode !== "session"' in APP_JS
    assert "localStorage" not in login + logout
    assert "sessionStorage" not in login + logout
    assert "username" in HTML and 'autocomplete="current-password"' in HTML


def test_auth_login_rate_limit_has_a_distinct_safe_message():
    login = _function("submitAuthLogin", "logoutSession")
    assert 'error.status === 401) authLoginStatus("Неверный логин или пароль.")' in login
    assert 'error.status === 429) authLoginStatus("Слишком много попыток входа. Попробуйте немного позже.")' in login
    assert 'error.status === 503) authLoginStatus("Сервис авторизации временно недоступен. Попробуйте позже.")' in login
    assert "account" not in login.lower()


def test_password_dialog_close_lifecycle_scrubs_secrets_even_for_native_close():
    create_cleanup = _function("scrubUserCreateDialogSecrets", "scrubResetUserPasswordDialogSecrets")
    reset_cleanup = _function("scrubResetUserPasswordDialogSecrets", "accountTimestampLabel")
    events = _function("setupEvents", "setZoom")

    assert '$("#new-user-password").value = ""' in create_cleanup
    assert '$("#new-user-password-confirm").value = ""' in create_cleanup
    assert '$("#user-create-status").textContent = ""' in create_cleanup
    assert '$("#user-create-status").hidden = true' in create_cleanup
    assert '.close(' not in create_cleanup

    assert '$("#reset-user-password").value = ""' in reset_cleanup
    assert '$("#reset-user-password-confirm").value = ""' in reset_cleanup
    assert '$("#reset-user-password-status").textContent = ""' in reset_cleanup
    assert '$("#reset-user-password-status").hidden = true' in reset_cleanup
    assert 'state.users.selectedUser = null' in reset_cleanup
    assert '.close(' not in reset_cleanup

    assert '$("#users-modal").addEventListener("close", scrubUserCreateDialogSecrets)' in events
    assert '$("#reset-user-password-modal").addEventListener("close", scrubResetUserPasswordDialogSecrets)' in events
    assert 'const closePasswordReset = () => $("#reset-user-password-modal").close()' in events
    assert not re.search(r'(?:localStorage|sessionStorage)\.setItem\([^\n]*password', APP_JS, re.I)


def test_users_and_support_controls_use_independent_capabilities_and_users_are_lazy():
    boot = _function("boot", "updateCloudStatus")
    opener = _function("openAdminUsers", "refreshUsersAfterStaleVersion")
    assert 'currentUser?.capabilities?.user_management !== true' in boot
    assert 'if (currentUser?.capabilities?.admin_reports === true)' in boot
    assert '$("#users-button").hidden = currentUser?.capabilities?.user_management !== true' in boot
    assert 'if (state.currentUser?.capabilities?.user_management !== true) return;' in opener
    assert "loadAdminUsers();" in opener
    assert 'id="users-button" type="button" hidden>Пользователи' in HTML
    assert 'id="admin-reports-button" hidden>Обращения' in HTML
    assert "/api/admin/users" not in boot
    assert "/api/admin/users" in _function("loadAdminUsers", "openAdminUsers")


def test_admin_list_is_paginated_and_server_values_are_rendered_as_text():
    loader = _function("loadAdminUsers", "openAdminUsers")
    renderer = _function("renderAdminUsers", "loadAdminUsers")
    page = _function("changeAdminUsersPage", "setView")
    assert '`/api/admin/users?limit=${users.limit}&offset=${users.offset}`' in loader
    assert "users.items.length === users.limit" in loader
    assert "users.offset / users.limit" in renderer
    assert "username.textContent" in renderer and "role.textContent" in renderer
    assert "status.textContent" in renderer
    assert 'String(user.role || "").toLowerCase() === "user"' in renderer
    assert 'role.textContent = String(user.role || "").toLowerCase() === "admin" ? "Администратор" : "Сметчик"' in renderer
    assert 'metadata.textContent = `Создан: ${accountTimestampLabel(user.created_at)} · Последний вход: ${user.last_login_at ? accountTimestampLabel(user.last_login_at) : "не было"}`' in renderer
    timestamp = _function("accountTimestampLabel", "renderAdminUsers")
    assert "new Intl.DateTimeFormat" in timestamp and "new Date(value)" in timestamp
    assert ".innerHTML" not in renderer
    mutations = "".join(_function(name, following) for name, following in (
        ("setAccountUserEnabled", "openAccountPasswordReset"),
        ("submitAccountPasswordReset", "deleteAccountUser"),
        ("deleteAccountUser", "createAccountUser"),
    ))
    assert "encodeURIComponent(user.user_id)" in mutations
    assert "state.users.offset = nextOffset" in page
    assert 'id="users-previous"' in HTML and 'id="users-next"' in HTML


def test_create_user_has_exact_payload_bounded_validation_confirmation_and_clearing():
    create = _function("createAccountUser", "changeAdminUsersPage")
    assert "state.currentUser?.capabilities?.user_management !== true" in create
    assert "/^[a-z0-9._-]{3,64}$/" in create
    assert "password.length < 12 || password.length > 128" in create
    assert "password !== confirmation" in create
    assert 'api("/api/admin/users"' in create
    assert "JSON.stringify({username, password})" in create
    assert "role:" not in create
    for selector in ('$("#new-user-password").value = ""', '$("#new-user-password-confirm").value = ""'):
        assert selector in create
    assert "USERNAME_EXISTS" in _function("accountErrorMessage", "renderAdminUsers")
    assert 'name="username"' in HTML and 'pattern="[a-z0-9._-]{3,64}"' in HTML
    assert 'minlength="12" maxlength="128" autocomplete="new-password"' in HTML
    assert 'id="new-user-password-confirm"' in HTML
    assert not re.search(r"(?:localStorage|sessionStorage)\.(?:getItem|setItem)\([^\n]*password", APP_JS, re.I)


def test_user_mutations_are_versioned_confirmed_and_refresh_stale_records_without_retry():
    toggle = _function("setAccountUserEnabled", "openAccountPasswordReset")
    reset = _function("submitAccountPasswordReset", "deleteAccountUser")
    delete = _function("deleteAccountUser", "createAccountUser")
    assert 'Пользователь потеряет доступ и активные сессии будут завершены. Отключить?' in toggle
    assert "state.currentUser?.capabilities?.user_management !== true" in toggle
    assert 'method:"PATCH"' in toggle and "JSON.stringify({enabled, version:user.version})" in toggle
    assert '`/api/admin/users/${encodeURIComponent(user.user_id)}/reset-password`' in reset
    assert "state.currentUser?.capabilities?.user_management !== true" in reset
    assert "JSON.stringify({password, version:user.version})" in reset
    assert 'Пароль изменён. Активные сеансы пользователя завершены.' in reset
    assert 'method:"DELETE"' in delete and "JSON.stringify({version:user.version})" in delete
    assert "state.currentUser?.capabilities?.user_management !== true" in delete
    assert "Удалить пользователя ${username}?" in delete
    assert "История обращений останется сохранённой." in delete
    for action in (toggle, reset, delete):
        assert 'error?.code === "STALE_USER_VERSION"' in action
        assert "refreshUsersAfterStaleVersion(generation)" in action
    refresh = _function("refreshUsersAfterStaleVersion", "setAccountUserEnabled")
    assert "loadAdminUsers(" in refresh
    assert "repeat" in refresh or "повторите действие вручную" in refresh
    assert "await api" not in refresh


def test_auth_login_and_admin_users_markup_has_required_controls_and_no_role_selector():
    required_ids = {
        "auth-login-form", "auth-login-username", "auth-login-password", "auth-login-submit",
        "auth-login-error", "logout-button", "users-modal", "user-create-form", "users-list",
        "users-status", "users-page", "users-previous", "users-next", "reset-user-password-modal",
        "reset-user-password-form", "reset-user-password", "reset-user-password-confirm",
    }
    ids = set(re.findall(r'\bid="([^\"]+)"', HTML))
    assert required_ids <= ids
    assert not re.search(r'<select[^>]+(?:role|account-role)', HTML, re.I)
    assert "auth-unavailable-screen" in HTML
