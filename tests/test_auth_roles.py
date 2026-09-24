from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.routing import APIRoute


PROXY_SECRET = "proxy-secret-for-tests"


@pytest.fixture()
def auth_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AVERON_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("AVERON_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AVERON_ADMIN_USERS", "averon")
    from averon_import import main

    return ApiClient(main.app)


class ApiResponse:
    def __init__(self, status_code: int, body: bytes, headers: list[tuple[bytes, bytes]]):
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.raw_headers = headers
        self.headers = {key.decode(): value.decode() for key, value in headers}

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class ApiClient:
    def __init__(self, app):
        self.app = app

    def request(self, method: str, path: str, *, headers=None, json=None):
        return _asgi_request(self.app, method, path, headers=headers, json=json)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


def _asgi_request(app, method, path, *, headers=None, json=None):
    payload = b""
    request_headers = [(str(key).lower().encode(), str(value).encode()) for key, value in (headers or {}).items()]
    if json is not None:
        payload = json_module_dumps(json).encode("utf-8")
        request_headers.append((b"content-type", b"application/json"))
    messages = []
    sent_body = False

    async def receive():
        nonlocal sent_body
        if sent_body:
            return {"type": "http.disconnect"}
        sent_body = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }
    async def invoke():
        await app(scope, receive, send)

    asyncio.run(invoke())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return ApiResponse(start["status"], body, start.get("headers", []))


def json_module_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _headers(username: str, *, role: str | None = None) -> dict[str, str]:
    headers = {
        "X-Averon-User": username,
        "X-Averon-Proxy": PROXY_SECRET,
    }
    if role is not None:
        headers["X-Averon-Role"] = role
    return headers


def test_missing_proxy_assertion_is_unauthorized(auth_client):
    response = auth_client.get("/api/me")

    assert response.status_code == 401


def test_invalid_proxy_assertion_is_unauthorized(auth_client):
    response = auth_client.get(
        "/api/me",
        headers={"X-Averon-User": "averon", "X-Averon-Proxy": "wrong"},
    )

    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_valid_user_and_admin_identity(auth_client):
    user = auth_client.get("/api/me", headers=_headers("colleague"))
    admin = auth_client.get("/api/me", headers=_headers("averon"))

    assert user.status_code == 200
    assert user.json() == {
        "username": "colleague",
        "role": "user",
        "auth_mode": "trusted_proxy",
        "capabilities": {
            "settings": False,
            "provider_maintenance": False,
            "admin_reports": False,
            "user_management": False,
        },
    }
    assert admin.status_code == 200
    assert admin.json()["role"] == "admin"
    assert all(admin.json()["capabilities"].values())


def test_client_role_header_is_ignored(auth_client):
    response = auth_client.get(
        "/api/me",
        headers=_headers("colleague", role="admin"),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "user"


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/settings", {}),
        ("put", "/api/settings", {"json": {"processing_mode": "cloud"}}),
        ("delete", "/api/settings/yandex-api-key", {}),
        ("delete", "/api/settings/yandex-ai-api-key", {}),
        ("delete", "/api/settings/lemana-client-secret", {}),
        ("delete", "/api/settings/etm-ipro-login", {}),
        ("delete", "/api/settings/etm-ipro-password", {}),
        ("post", "/api/sourcing/providers/etm_ipro/manufacturers/sync", {}),
        ("get", "/api/sourcing/providers/etm_ipro/health", {}),
        ("post", "/api/sourcing/providers/etm_ipro/catalog/sync", {}),
        ("get", "/api/sourcing/providers/etm_ipro/catalog/status", {}),
        ("post", "/api/sourcing/providers/etm_ipro/catalog/import", {}),
        ("post", "/api/sourcing/providers/etm_ipro/catalog/reindex", {}),
        ("post", "/api/sourcing/providers/lemana_b2b/sync", {}),
    ],
)
def test_user_cannot_use_admin_endpoints(auth_client, method, path, kwargs):
    response = getattr(auth_client, method)(path, headers=_headers("colleague"), **kwargs)

    assert response.status_code == 403


def test_admin_settings_endpoint_remains_available(auth_client):
    response = auth_client.get("/api/settings", headers=_headers("averon"))

    assert response.status_code == 200
    assert "yandex" in response.json()


def test_admin_provider_maintenance_endpoints_remain_available(auth_client, monkeypatch):
    from averon_import import main

    class FakeProvider:
        def sync(self):
            return {"ok": True}

        def sync_manufacturers(self):
            return 1

        def health(self):
            return {"available": True}

        def start_catalog_sync(self):
            return {"state": 0}

        def catalog_sync_status(self):
            return {"state": 0}

        def import_completed_catalog(self, progress):
            return {"imported": 1}

        def rebuild_search_index(self, progress):
            return {"indexed": 1}

    class FakeJob:
        def public(self):
            return {"id": "auth-test-job", "status": "queued"}

    class FakeJobs:
        def submit(self, function):
            return FakeJob()

    monkeypatch.setattr(main.sourcing_service, "provider", lambda key: FakeProvider())
    monkeypatch.setattr(main, "job_service", FakeJobs())

    checks = [
        ("/api/sourcing/providers/lemana_b2b/sync", "post"),
        ("/api/sourcing/providers/etm_ipro/manufacturers/sync", "post"),
        ("/api/sourcing/providers/etm_ipro/health", "get"),
        ("/api/sourcing/providers/etm_ipro/catalog/sync", "post"),
        ("/api/sourcing/providers/etm_ipro/catalog/status", "get"),
        ("/api/sourcing/providers/etm_ipro/catalog/import", "post"),
        ("/api/sourcing/providers/etm_ipro/catalog/reindex", "post"),
    ]
    for path, method in checks:
        response = getattr(auth_client, method)(path, headers=_headers("averon"))
        assert response.status_code == 200, (path, response.text)


def test_user_can_reach_normal_document_and_export_routes(auth_client):
    headers = _headers("colleague")
    checks = [
        ("get", "/api/documents/not-found"),
        ("get", "/api/documents/not-found/page/1"),
        ("post", "/api/documents/not-found/suggest-pages"),
        ("post", "/api/documents/not-found/recognize"),
        ("get", "/api/documents/not-found/results"),
        ("put", "/api/documents/not-found/results"),
        ("post", "/api/documents/not-found/export"),
        ("get", "/api/documents/not-found/review"),
        ("post", "/api/documents/not-found/review/decision"),
        ("post", "/api/documents/not-found/sourcing/search"),
        ("post", "/api/documents/not-found/sourcing/search-all"),
        ("get", "/api/documents/not-found/sourcing/runs"),
        ("post", "/api/sourcing/understand"),
        ("post", "/api/sourcing/search"),
        ("post", "/api/sourcing/search-intent"),
        ("post", "/api/sourcing/search-all"),
    ]
    for method, path in checks:
        response = getattr(auth_client, method)(path, headers=headers)
        assert response.status_code not in {401, 403}, (method, path, response.text)


def test_safe_health_hides_operational_details_and_admin_health_keeps_them(auth_client, monkeypatch):
    from averon_import import main

    monkeypatch.setattr(main.coordinator, "ocr_health", lambda: {"available": True})
    monkeypatch.setattr(main.yandex_vision_provider, "health", lambda: {"available": True})
    monkeypatch.setattr(main.ai_service, "health", lambda: {"available": True})
    monkeypatch.setattr(main.sourcing_service, "health", lambda: {"available": True})

    user = auth_client.get("/api/health", headers=_headers("colleague"))
    admin = auth_client.get("/api/admin/health", headers=_headers("averon"))

    assert user.status_code == 200
    user_payload = user.json()
    assert "data_dir" not in user_payload
    assert "settings" not in user_payload
    assert "secret_backend" not in json.dumps(user_payload, ensure_ascii=False)
    assert admin.status_code == 200
    assert "data_dir" in admin.json()
    assert "settings" in admin.json()


def test_docs_are_admin_only(auth_client):
    user_docs = auth_client.get("/api/admin/docs", headers=_headers("colleague"))
    admin_docs = auth_client.get("/api/admin/docs", headers=_headers("averon"))
    user_openapi = auth_client.get("/api/admin/openapi.json", headers=_headers("colleague"))
    admin_openapi = auth_client.get("/api/admin/openapi.json", headers=_headers("averon"))
    public_docs = auth_client.get("/api/docs", headers=_headers("averon"))
    public_openapi = auth_client.get("/openapi.json", headers=_headers("averon"))

    assert user_docs.status_code == 403
    assert admin_docs.status_code == 200
    assert "SwaggerUIBundle" in admin_docs.text
    assert user_openapi.status_code == 403
    assert admin_openapi.status_code == 200
    assert "/api/settings" in admin_openapi.text
    assert public_docs.status_code == 404
    assert public_openapi.status_code == 404


def test_legacy_shared_workspace_remains_openable(auth_client, monkeypatch, tmp_path):
    from averon_import import main
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path)
    document_id = "a" * 32
    root = service.documents_dir / document_id
    root.mkdir()
    service.write_json(
        root / "metadata.json",
        {"document_id": document_id, "filename": "legacy.pdf", "page_count": 1},
    )
    service.write_json(root / "result.json", {"rows": []})
    monkeypatch.setattr(main, "workspace_service", service)

    response = auth_client.get(f"/api/documents/{document_id}", headers=_headers("colleague"))

    assert response.status_code == 200
    assert response.json()["has_result"] is True


def test_frontend_boot_loads_settings_only_for_admin_and_hides_button_for_user():
    root = __import__("pathlib").Path(__file__).parents[1]
    app_js = (root / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    boot = app_js.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]

    assert 'const currentUser = await api("/api/me")' in boot
    assert 'if (isAdmin)' in boot
    assert 'state.settings = await api("/api/settings")' in boot
    assert '$("#settings-button").hidden = !isAdmin' in boot
    assert '$("#logout-button").hidden = authMode !== "session"' in boot
    assert '$("#users-button").hidden = currentUser?.capabilities?.user_management !== true' in boot
    assert '<button class="button ghost" id="settings-button" hidden>' in html


def test_explicit_local_dev_mode_is_the_only_synthetic_admin_path(monkeypatch):
    from averon_import.services.auth import Role, resolve_current_user
    from starlette.requests import Request

    monkeypatch.setenv("AVERON_AUTH_MODE", "local_dev")
    monkeypatch.setenv("AVERON_DEV_USER", "desktop-admin")
    scope = {
        "type": "http",
        "headers": [(b"host", b"localhost:8765")],
        "client": ("127.0.0.1", 8765),
    }
    user = resolve_current_user(Request(scope))

    assert user.username == "desktop-admin"
    assert user.role is Role.ADMIN


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:8765", "[::1]:8765"])
def test_local_dev_accepts_loopback_hostnames(monkeypatch, host):
    from averon_import.services.auth import Role, resolve_current_user
    from starlette.requests import Request

    monkeypatch.setenv("AVERON_AUTH_MODE", "local_dev")
    scope = {
        "type": "http",
        "headers": [(b"host", host.encode("ascii"))],
        "client": ("127.0.0.1", 8765),
    }

    user = resolve_current_user(Request(scope))

    assert user.role is Role.ADMIN


def test_local_dev_rejects_public_host_from_loopback_client(monkeypatch):
    from averon_import.services.auth import resolve_current_user
    from starlette.requests import Request
    from fastapi import HTTPException

    monkeypatch.setenv("AVERON_AUTH_MODE", "local_dev")
    scope = {
        "type": "http",
        "headers": [(b"host", b"averon.nvss-home-new.ru")],
        "client": ("127.0.0.1", 8765),
    }

    with pytest.raises(HTTPException) as error:
        resolve_current_user(Request(scope))

    assert error.value.status_code == 401


def test_local_dev_rejects_remote_client_even_with_loopback_host(monkeypatch):
    from averon_import.services.auth import resolve_current_user
    from starlette.requests import Request
    from fastapi import HTTPException

    monkeypatch.setenv("AVERON_AUTH_MODE", "local_dev")
    scope = {
        "type": "http",
        "headers": [(b"host", b"localhost:8765")],
        "client": ("192.0.2.10", 8765),
    }

    with pytest.raises(HTTPException) as error:
        resolve_current_user(Request(scope))

    assert error.value.status_code == 401


def test_trusted_proxy_without_server_secret_fails_closed(auth_client, monkeypatch):
    monkeypatch.delenv("AVERON_PROXY_SECRET", raising=False)

    response = auth_client.get(
        "/api/me",
        headers={"X-Averon-User": "averon", "X-Averon-Proxy": PROXY_SECRET},
    )

    assert response.status_code == 503


def test_every_api_route_requires_application_authentication(auth_client):
    from averon_import import main
    from averon_import.services.auth import require_admin, require_authenticated

    def has_auth_dependency(dependant):
        if dependant.call in {require_authenticated, require_admin}:
            return True
        return any(has_auth_dependency(child) for child in dependant.dependencies)

    unauthenticated = [
        route.path
        for route in main.app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/")
        and not has_auth_dependency(route.dependant)
        and route.path != "/api/auth/login"
    ]

    assert unauthenticated == []
    login_route = next(route for route in main.app.routes if isinstance(route, APIRoute) and route.path == "/api/auth/login")
    assert not has_auth_dependency(login_route.dependant)
