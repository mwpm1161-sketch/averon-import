from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

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

    def post_multipart(self, path, *, headers=None, files, data=None):
        boundary = "----AveronTestBoundary7MA4YWxk"
        parts = []
        for name, value in (data or {}).items():
            parts.extend([
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ])
        for name, (filename, content, content_type) in files.items():
            parts.extend([
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ])
        parts.append(f"--{boundary}--\r\n".encode())
        request_headers = dict(headers or {})
        request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        return _asgi_request(self.app, "POST", path, headers=request_headers, raw_body=b"".join(parts))

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


def _asgi_request(app, method, path, *, headers=None, json=None, raw_body=None):
    payload = raw_body or b""
    request_headers = [(str(key).lower().encode(), str(value).encode()) for key, value in (headers or {}).items()]
    if json is not None:
        payload = json_module_dumps(json).encode("utf-8")
        if not any(key == b"content-type" for key, _ in request_headers):
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
            "one_c_history_import": True,
            "provider_maintenance": False,
            "admin_reports": False,
            "user_management": False,
            "document_management": False,
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
        ("get", "/api/admin/one-c-history", {}),
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


def test_one_c_history_status_is_available_to_admin_only(auth_client):
    assert auth_client.get("/api/admin/one-c-history", headers=_headers("colleague")).status_code == 403
    admin = auth_client.get("/api/admin/one-c-history", headers=_headers("averon"))
    assert admin.status_code == 200
    assert admin.json()["profiles"] == []


def test_sourcing_history_status_is_authenticated_and_safe_for_users(auth_client, monkeypatch):
    from averon_import import main

    allowed = {
        "available", "catalog_version", "period_start", "period_end", "imported_at",
        "item_count", "event_count", "usable_price_event_count",
    }
    status = {
        "available": True,
        "catalog_version": "v1",
        "period_start": "2026-06-23",
        "period_end": "2026-09-22",
        "imported_at": "2026-09-23T10:00:00+00:00",
        "item_count": 3,
        "event_count": 5,
        "usable_price_event_count": 4,
    }
    monkeypatch.setattr(main.one_c_history_repository, "sourcing_status", lambda: status)

    assert auth_client.get("/api/sourcing/history-status").status_code == 401
    response = auth_client.get("/api/sourcing/history-status", headers=_headers("colleague"))

    assert response.status_code == 200
    assert {key: value for key, value in response.json().items() if key != "activity"} == status
    assert {key for key in response.json() if key != "activity"} == allowed
    assert response.json()["activity"] == {
        "active_sourcing_count": 0,
        "update_in_progress": False,
        "replacement_allowed": True,
    }
    assert auth_client.get("/api/admin/one-c-history", headers=_headers("colleague")).status_code == 403


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
        def __init__(self):
            self.submissions = []

        def submit(self, function, **_job_options):
            self.submissions.append(_job_options)
            return FakeJob()

    jobs = FakeJobs()
    monkeypatch.setattr(main.sourcing_service, "provider", lambda key: FakeProvider())
    monkeypatch.setattr(main, "job_service", jobs)

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
        if method == "post":
            assert jobs.submissions[-1]["lane"] == "sourcing"
            assert jobs.submissions[-1]["fail_if_lane_occupied"] is True


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
    assert '$("#settings-button").hidden = capabilities.settings !== true && capabilities.one_c_history_import !== true' in boot
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


def test_one_c_history_routes_require_admin_dependency(auth_client):
    from averon_import import main
    from averon_import.services.auth import require_admin

    def has_admin_dependency(dependant):
        if dependant.call is require_admin:
            return True
        return any(has_admin_dependency(child) for child in dependant.dependencies)

    routes = [
        route for route in main.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/admin/one-c-history")
    ]
    assert routes
    assert all(has_admin_dependency(route.dependant) for route in routes)


def test_capability_one_c_history_routes_are_available_to_users_without_system_settings(auth_client):
    from averon_import import main
    from averon_import.services.auth import require_one_c_history_import

    def has_capability_dependency(dependant):
        if dependant.call is require_one_c_history_import:
            return True
        return any(has_capability_dependency(child) for child in dependant.dependencies)

    user = auth_client.get("/api/me", headers=_headers("colleague")).json()
    admin = auth_client.get("/api/me", headers=_headers("averon")).json()
    assert user["capabilities"]["settings"] is False
    assert user["capabilities"]["one_c_history_import"] is True
    assert admin["capabilities"]["settings"] is True
    assert admin["capabilities"]["one_c_history_import"] is True
    assert auth_client.get("/api/one-c-history", headers=_headers("colleague")).status_code == 200
    assert auth_client.get("/api/one-c-history", headers=_headers("averon")).status_code == 200
    assert auth_client.get("/api/settings", headers=_headers("colleague")).status_code == 403
    assert auth_client.put("/api/settings", headers=_headers("colleague"), json={"processing_mode": "cloud"}).status_code == 403
    assert auth_client.delete("/api/admin/one-c-history/profiles/missing", headers=_headers("colleague")).status_code == 403
    user_status = auth_client.get("/api/one-c-history", headers=_headers("colleague")).json()
    assert "recent_audit" not in user_status
    assert not {"username", "user_id", "document_id"}.intersection(user_status["activity"])

    routes = [
        route for route in main.app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/one-c-history")
    ]
    assert {route.path for route in routes} >= {
        "/api/one-c-history",
        "/api/one-c-history/previews",
        "/api/one-c-history/previews/{preview_id}/mapping",
        "/api/one-c-history/previews/{preview_id}/sheets/{sheet_name}",
        "/api/one-c-history/imports",
    }
    assert all(has_capability_dependency(route.dependant) for route in routes)


def test_user_can_create_and_use_only_owned_capability_scoped_preview(auth_client, monkeypatch):
    from averon_import import main

    observed = []

    async def create_preview(upload, *, profile_id=None, owner_key=None):
        observed.append(("create", upload.filename, owner_key))
        return {"preview_id": "a" * 32, "owner": owner_key}

    async def inspect_preview(preview_id, sheet_name, header_row=None, *, group_header_row=None, event_header_row=None, owner_key=None):
        observed.append(("inspect", preview_id, owner_key))
        return {"sheet_name": sheet_name}

    async def analyze_preview(preview_id, request, *, owner_key=None):
        observed.append(("analyze", preview_id, owner_key))
        return {"preview_id": preview_id}

    async def import_preview(request, *, owner_key=None, actor=None, can_manage_profiles=True):
        observed.append(("import", request.preview_id, owner_key, actor, can_manage_profiles))
        if request.save_profile and not can_manage_profiles:
            raise PermissionError("Недостаточно прав для сохранения профиля импорта.")
        return {"status": "succeeded"}

    monkeypatch.setattr(main.one_c_history_service, "create_preview", create_preview)
    monkeypatch.setattr(main.one_c_history_service, "inspect_preview_sheet", inspect_preview)
    monkeypatch.setattr(main.one_c_history_service, "analyze_preview", analyze_preview)
    monkeypatch.setattr(main.one_c_history_service, "import_confirmed", import_preview)

    preview = auth_client.post_multipart(
        "/api/one-c-history/previews",
        headers=_headers("colleague"),
        files={"file": ("report.xlsx", b"test-xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200
    assert preview.json()["owner"] == "username:colleague"
    assert auth_client.get(
        f"/api/one-c-history/previews/{'a' * 32}/sheets/TDSheet?header_row=1",
        headers=_headers("colleague"),
    ).status_code == 200
    body = {
        "sheet_name": "TDSheet", "header_row": 1, "layout_type": "flat",
        "field_mapping": {"item_name": 0},
    }
    assert auth_client.post(
        f"/api/one-c-history/previews/{'a' * 32}/mapping",
        headers=_headers("colleague"), json=body,
    ).status_code == 200
    assert auth_client.post(
        "/api/one-c-history/imports",
        headers=_headers("colleague"),
        json={"preview_id": "a" * 32, "layout_type": "flat", "save_profile": False},
    ).status_code == 200
    assert [item[0] for item in observed] == ["create", "inspect", "analyze", "import"]
    assert all(item[2] == "username:colleague" for item in observed if item[0] != "import")
    assert observed[-1][2] == "username:colleague"
    assert observed[-1][4] is False
    denied_profile = auth_client.post(
        "/api/one-c-history/imports",
        headers=_headers("colleague"),
        json={"preview_id": "a" * 32, "layout_type": "flat", "save_profile": True, "profile_name": "Global"},
    )
    assert denied_profile.status_code == 403

    assert auth_client.post_multipart(
        "/api/admin/one-c-history/previews",
        headers=_headers("colleague"),
        files={"file": ("report.xlsx", b"test-xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ).status_code == 403

def _make_cleanup_workspace(service, document_id: str):
    root = service.documents_dir / document_id
    (root / "pages").mkdir(parents=True)
    (root / "exports").mkdir()
    (root / "sourcing_runs").mkdir()
    service.write_json(root / "metadata.json", {
        "document_id": document_id,
        "filename": "pilot.pdf",
        "page_count": 1,
        "size": 4,
    })
    (root / "source.pdf").write_bytes(b"pdf!")
    service.write_json(root / "result.json", {"rows": [{"name": "test"}]})
    service.write_json(root / "review_decisions.json", {"revision": 0, "decisions": []})
    (root / "pages" / "page-1.png").write_bytes(b"page image")
    (root / "exports" / "result.xlsx").write_bytes(b"workbook")
    service.write_json(root / "sourcing_runs" / "run.json", {"result": "saved"})
    return root


def test_admin_document_cleanup_deletes_only_workspace_and_preserves_support_audit(
    auth_client, monkeypatch, tmp_path
):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    data_dir = tmp_path / "data"
    service = WorkspaceService(data_dir)
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", DocumentActivityRegistry())
    document_id = uuid.uuid4().hex
    other_id = uuid.uuid4().hex
    workspace_root = _make_cleanup_workspace(service, document_id)
    other_root = _make_cleanup_workspace(service, other_id)
    expected_freed_bytes = sum(
        path.stat().st_size for path in workspace_root.rglob("*") if path.is_file()
    )

    support_snapshot = data_dir / "support" / "incidents" / "incident-1.json"
    support_snapshot.parent.mkdir(parents=True)
    support_snapshot.write_bytes(b"immutable support snapshot")
    auth_marker = data_dir / "auth" / "users.json"
    auth_marker.parent.mkdir()
    auth_marker.write_bytes(b"auth data")
    catalog_marker = data_dir / "sourcing" / "catalog.db"
    catalog_marker.parent.mkdir()
    catalog_marker.write_bytes(b"catalog data")

    denied = auth_client.delete(
        f"/api/admin/documents/{document_id}", headers=_headers("colleague")
    )
    assert denied.status_code == 403
    assert workspace_root.is_dir()

    deleted = auth_client.delete(
        f"/api/admin/documents/{document_id}", headers=_headers("averon")
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "deleted": True,
        "document_id": document_id,
        "freed_bytes": expected_freed_bytes,
    }
    assert not workspace_root.exists()
    assert other_root.is_dir()
    assert support_snapshot.read_bytes() == b"immutable support snapshot"
    assert auth_marker.read_bytes() == b"auth data"
    assert catalog_marker.read_bytes() == b"catalog data"
    assert auth_client.get("/api/documents", headers=_headers("colleague")).json() == {
        "documents": [service.list_recent()[0]]
    }
    assert [item["document_id"] for item in service.list_recent()] == [other_id]


def test_document_delete_requires_admin_and_invalid_ids_fail_closed(auth_client, monkeypatch, tmp_path):
    from fastapi import HTTPException

    from averon_import import main
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    monkeypatch.setattr(main, "workspace_service", service)
    response = auth_client.delete("/api/admin/documents/../outside", headers=_headers("averon"))
    assert response.status_code in {404, 405}

    with pytest.raises(HTTPException) as error:
        main.delete_document_workspace("..\\outside")
    assert error.value.status_code == 404
    assert not (tmp_path / "outside").exists()


def test_document_delete_returns_conflict_while_a_document_operation_is_active(
    auth_client, monkeypatch, tmp_path
):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", DocumentActivityRegistry())
    document_id = uuid.uuid4().hex
    root = _make_cleanup_workspace(service, document_id)

    with main.document_activity_registry.lease(document_id):
        response = auth_client.delete(
            f"/api/admin/documents/{document_id}", headers=_headers("averon")
        )

    assert response.status_code == 409
    assert root.is_dir()
    assert service.list_recent()[0]["document_id"] == document_id


def test_document_jobs_hold_a_lifecycle_lease_from_queue_until_completion(monkeypatch):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.jobs import DOCUMENT_PROCESSING, JobCoordinator

    monkeypatch.setattr(main, "document_activity_registry", DocumentActivityRegistry())
    document_id = uuid.uuid4().hex
    jobs = JobCoordinator()
    started = threading.Event()
    release = threading.Event()
    blocker = jobs.submit(
        lambda _progress: (started.set(), release.wait(2))[1],
        lane=DOCUMENT_PROCESSING,
    )
    assert started.wait(1)
    monkeypatch.setattr(main, "job_service", jobs)
    job = main._submit_document_job(document_id, lambda _progress: "done")
    assert job.id != blocker.id
    assert job.status == "queued"
    assert main.document_activity_registry.active_operations(document_id) == 1
    assert main.document_activity_registry.begin_delete(document_id) is False
    try:
        release.set()
        deadline = time.monotonic() + 2
        while job.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert job.status == "completed"
        assert job.result == "done"
        assert main.document_activity_registry.active_operations(document_id) == 0
        assert main.document_activity_registry.begin_delete(document_id) is True
        main.document_activity_registry.cancel_delete(document_id)
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_workspace_rename_hides_document_before_recursive_cleanup(
    auth_client, monkeypatch, tmp_path
):
    import shutil

    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", DocumentActivityRegistry())
    document_id = uuid.uuid4().hex
    root = _make_cleanup_workspace(service, document_id)
    original_rmtree = shutil.rmtree
    observed = []

    def observe_tombstone(path, *args, **kwargs):
        observed.append(Path(path))
        assert not root.exists()
        assert service.list_recent() == []
        assert Path(path).parent == service.tombstones_dir
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(main.shutil, "rmtree", observe_tombstone)
    response = auth_client.delete(
        f"/api/admin/documents/{document_id}", headers=_headers("averon")
    )

    assert response.status_code == 200
    assert len(observed) == 1
    assert not observed[0].exists()


def test_failed_recursive_cleanup_returns_safe_error_and_keeps_tombstone_hidden(
    auth_client, monkeypatch, tmp_path
):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", DocumentActivityRegistry())
    document_id = uuid.uuid4().hex
    root = _make_cleanup_workspace(service, document_id)

    def fail_cleanup(_path):
        raise PermissionError("private path detail")

    monkeypatch.setattr(main.shutil, "rmtree", fail_cleanup)
    response = auth_client.delete(
        f"/api/admin/documents/{document_id}", headers=_headers("averon")
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["detail"] == "Документ удалён из списка, но не удалось полностью очистить его файлы."
    assert payload["error"] == {
        "code": "DOCUMENT_REMOVED_CLEANUP_PENDING",
        "deleted": True,
    }
    assert isinstance(payload["freed_bytes"], int)
    assert "private path detail" not in response.text
    assert str(service.tombstones_dir) not in response.text
    assert not root.exists()
    assert service.list_recent() == []
    assert len(list(service.tombstones_dir.iterdir())) == 1


def test_page_file_response_holds_document_lease_through_delivery(monkeypatch, tmp_path):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    document_id = uuid.uuid4().hex
    (service.documents_dir / document_id).mkdir()
    workspace = service.get(document_id)
    service.write_json(workspace.metadata_path, {"document_id": document_id, "page_count": 1})
    (workspace.pages_dir / "page-1-110.png").write_bytes(b"png")
    registry = DocumentActivityRegistry()
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", registry)

    response = main.page_image(document_id, 1, dpi=110)

    assert response.background is not None
    assert registry.active_operations(document_id) == 1
    assert registry.begin_delete(document_id) is False
    asyncio.run(response.background())
    assert registry.active_operations(document_id) == 0
    assert registry.begin_delete(document_id) is True
    registry.cancel_delete(document_id)


def test_export_file_response_holds_document_lease_through_delivery(monkeypatch, tmp_path):
    from averon_import import main
    from averon_import.core.schemas import ExportRequest
    from averon_import.services.auth import CurrentUser, Role
    from averon_import.services.document_lifecycle import DocumentActivityRegistry
    from averon_import.services.workspace import WorkspaceService

    service = WorkspaceService(tmp_path / "data")
    document_id = uuid.uuid4().hex
    (service.documents_dir / document_id).mkdir()
    workspace = service.get(document_id)
    service.write_json(workspace.result_path, {
        "revision": 7,
        "review_projection_version": main.REVIEW_PROJECTION_VERSION,
        "review_ledger_revision": 0,
        "rows": [],
        "page_statuses": {},
    })
    registry = DocumentActivityRegistry()

    class StubExportService:
        def export(self, *, output_path, **_kwargs):
            output_path.write_bytes(b"xlsx")
            return output_path

    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(main, "document_activity_registry", registry)
    monkeypatch.setattr(main, "export_service", StubExportService())

    response = main.export(
        document_id,
        ExportRequest(columns=["name"], rows=[], expected_revision=7),
        CurrentUser("admin", Role.ADMIN),
    )

    assert response.background is not None
    assert registry.active_operations(document_id) == 1
    assert registry.begin_delete(document_id) is False
    asyncio.run(response.background())
    assert registry.active_operations(document_id) == 0
    assert registry.begin_delete(document_id) is True
    registry.cancel_delete(document_id)


def test_file_response_construction_failure_releases_document_lease(monkeypatch):
    from averon_import import main
    from averon_import.services.document_lifecycle import DocumentActivityRegistry

    registry = DocumentActivityRegistry()
    document_id = uuid.uuid4().hex
    monkeypatch.setattr(main, "document_activity_registry", registry)

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(main, "FileResponse", fail_response)
    with pytest.raises(RuntimeError, match="response construction failed"):
        main.document_file_response(document_id, Path("unused"))

    assert registry.active_operations(document_id) == 0
    assert registry.begin_delete(document_id) is True
    registry.cancel_delete(document_id)


def test_sourcing_request_modes_default_to_provider_only_and_intent_route_rejects_history(auth_client):
    from averon_import import main

    assert main.SourcingRowRequest(row={}).source_mode.value == "provider_only"
    assert main.SourcingProjectRequest(rows=[]).source_mode.value == "provider_only"
    response = auth_client.post(
        "/api/sourcing/search-intent",
        headers=_headers("averon"),
        json={
            "intent": {
                "source_row_id": "row-1",
                "source_text": "Клапан",
                "normalized_name": "Клапан",
            },
            "source_mode": "one_c_only",
        },
    )

    assert response.status_code == 400
    assert "исходную строку" in response.json()["detail"]
