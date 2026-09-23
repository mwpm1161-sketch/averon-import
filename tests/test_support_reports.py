from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PROXY_SECRET = "support-proxy-secret"


class ApiResponse:
    def __init__(self, status_code: int, body: bytes, headers):
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {
            key.decode(): value.decode()
            for key, value in headers
        }

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

    def patch(self, path, **kwargs):
        return self.request("PATCH", path, **kwargs)


def _asgi_request(app, method, path, *, headers=None, json=None):
    parsed_path = urlsplit(path)
    payload = b""
    request_headers = [
        (str(key).lower().encode(), str(value).encode())
        for key, value in (headers or {}).items()
    ]
    if json is not None:
        payload = __import__("json").dumps(json, ensure_ascii=False).encode("utf-8")
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
        "path": parsed_path.path,
        "raw_path": parsed_path.path.encode(),
        "query_string": parsed_path.query.encode(),
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }

    async def invoke():
        await app(scope, receive, send)

    asyncio.run(invoke())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return ApiResponse(start["status"], body, start.get("headers", []))


def _headers(username: str) -> dict[str, str]:
    return {"X-Averon-User": username, "X-Averon-Proxy": PROXY_SECRET}


@pytest.fixture()
def support_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AVERON_AUTH_MODE", "trusted_proxy")
    monkeypatch.setenv("AVERON_PROXY_SECRET", PROXY_SECRET)
    monkeypatch.setenv("AVERON_ADMIN_USERS", "averon")
    from averon_import import main
    from averon_import.services.support_reports import SupportRepository
    from averon_import.services.workspace import WorkspaceService

    repository = SupportRepository(tmp_path / "support")
    workspace_service = WorkspaceService(tmp_path / "data")
    monkeypatch.setattr(main, "support_repository", repository)
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    return ApiClient(main.app), main, repository, workspace_service, tmp_path


def _make_workspace(workspace_service, document_id="d" * 32):
    root = workspace_service.documents_dir / document_id
    root.mkdir(parents=True)
    workspace_service.write_json(
        root / "metadata.json",
        {
            "document_id": document_id,
            "filename": "specification.pdf",
            "title": "Test specification",
            "page_count": 3,
            "size": 1234,
        },
    )
    workspace_service.write_json(
        root / "result.json",
        {
            "summary": {"total_rows": 1},
            "page_statuses": {
                "1": {
                    "page": 1,
                    "output_status": "USABLE",
                    "page_disposition": "SPEC",
                    "blockers": [],
                }
            },
        },
    )
    return workspace_service.get(document_id)


def _export_payload(filename="failed.xlsx"):
    return {
        "columns": ["name"],
        "rows": [{"page": 1, "name": "Насос", "row_type": "item"}],
        "include_headers": True,
        "only_exportable": True,
        "filename": filename,
        "sheet_name": "Спецификация",
        "review_export": False,
    }


def _create_incident(repository, *, username="colleague", document_id="d" * 32):
    return repository.create_export_incident(
        document_id=document_id,
        username=username,
        role="user",
        app_version="test-version",
        export_kind="production",
        requested_filename="failed.xlsx",
        row_count=1,
        document={
            "filename": "specification.pdf",
            "title": "Test specification",
            "page_count": 3,
            "size": 1234,
        },
        export_request=_export_payload(),
        page_statuses={"1": {"output_status": "USABLE"}},
        result_summary={"total_rows": 1},
        resolved_filename="failed.xlsx",
        error_code="EXPORT_FAILED",
        public_message="Не удалось сформировать Excel.",
        http_status=500,
    )


def test_success_export_keeps_file_behavior_and_creates_no_incident(support_context, monkeypatch):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("success.xlsx"),
    )

    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert repository.list_reports(limit=100, offset=0) == []
    assert (workspace.exports_dir / "success.xlsx").is_file()


def test_value_error_creates_reportable_incident_with_safe_contract(support_context, monkeypatch):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    def fail(*args, **kwargs):
        raise ValueError("Не выбрано ни одного столбца для экспорта")

    monkeypatch.setattr(main.export_service, "export", fail)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload(),
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["detail"] == "Не выбрано ни одного столбца для экспорта"
    assert payload["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert payload["error"]["reportable"] is True
    incident = repository.get_incident(payload["error"]["incident_id"])
    assert incident["http_status"] == 400
    assert incident["snapshot_path"].startswith("incidents/")


def test_unexpected_export_error_is_generic_and_snapshot_has_no_raw_error(support_context, monkeypatch):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    def fail(*args, **kwargs):
        raise RuntimeError("authorization=top-secret traceback C:\\private\\file")

    monkeypatch.setattr(main.export_service, "export", fail)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload(),
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Не удалось сформировать Excel."
    assert "top-secret" not in response.text
    assert "private" not in response.text
    incident = repository.get_incident(response.json()["error"]["incident_id"])
    snapshot = repository.snapshot_for_report(
        repository.create_report(
            incident_id=incident["incident_id"],
            reporter_fio="Иван Петров",
            description="Подробное описание ошибки экспорта для проверки.",
            username="colleague",
        )["report_id"]
    )
    snapshot_text = json.dumps(snapshot, ensure_ascii=False)
    assert "top-secret" not in snapshot_text
    assert "private" not in snapshot_text


def test_support_persistence_failure_does_not_mask_export_error(support_context, monkeypatch):
    client, main, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    def fail_export(*args, **kwargs):
        raise RuntimeError("internal export failure")

    def fail_persistence(*args, **kwargs):
        raise OSError("support database unavailable")

    monkeypatch.setattr(main.export_service, "export", fail_export)
    monkeypatch.setattr(main.support_repository, "create_export_incident", fail_persistence)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload(),
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Не удалось сформировать Excel."
    assert response.json()["error"] == {
        "code": "EXPORT_FAILED",
        "incident_id": None,
        "reportable": False,
    }


def test_failed_export_preserves_previous_file_and_removes_temp_file(support_context, monkeypatch):
    client, main, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    previous = workspace.exports_dir / "failed.xlsx"
    previous.write_bytes(b"previous successful export")

    def fail(*args, **kwargs):
        output_path = kwargs["output_path"]
        Path(output_path).write_bytes(b"partial replacement")
        raise RuntimeError("xlsx failure")

    monkeypatch.setattr(main.export_service, "export", fail)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload(),
    )

    assert response.status_code == 500
    assert previous.read_bytes() == b"previous successful export"
    assert list(workspace.exports_dir.glob(".averon-export-*.tmp")) == []


def test_snapshot_is_immutable_and_hash_matches(support_context, monkeypatch):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    monkeypatch.setattr(main.export_service, "export", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad export")))
    request_payload = _export_payload()
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=request_payload,
    )
    incident = repository.get_incident(response.json()["error"]["incident_id"])
    snapshot_path = repository.incidents_dir / f"{incident['incident_id']}.json"
    before = snapshot_path.read_bytes()
    workspace_service.write_json(workspace.result_path, {"summary": {"total_rows": 999}})
    after = snapshot_path.read_bytes()

    assert before == after
    assert hashlib.sha256(after).hexdigest() == incident["snapshot_sha256"]
    snapshot = json.loads(after.decode("utf-8"))
    assert snapshot["export_request"] == request_payload
    assert snapshot["page_statuses"]["1"]["output_status"] == "USABLE"
    assert not (workspace.root / "source.pdf").exists()


def test_support_report_validates_ownership_duplicate_and_restart(support_context):
    client, _, repository, _, tmp_path = support_context
    incident = _create_incident(repository)
    payload = {
        "incident_id": incident["incident_id"],
        "reporter_fio": "  Иван Петров  ",
        "description": "  Подробное описание ошибки экспорта для поддержки.  ",
    }

    created = client.post("/api/support/reports", headers=_headers("colleague"), json=payload)
    assert created.status_code == 200
    report = created.json()
    assert report["status"] == "OPEN"
    assert report["reporter_fio"] == "Иван Петров"
    assert report["description"] == "Подробное описание ошибки экспорта для поддержки."
    assert report["incident"]["document_available"] is False

    duplicate = client.post("/api/support/reports", headers=_headers("colleague"), json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["report_id"] == report["report_id"]

    foreign = client.post("/api/support/reports", headers=_headers("other"), json=payload)
    assert foreign.status_code == 403

    admin_incident = _create_incident(repository, username="averon", document_id="e" * 32)
    admin_report = client.post(
        "/api/support/reports",
        headers=_headers("averon"),
        json={
            "incident_id": admin_incident["incident_id"],
            "reporter_fio": "Анна Смирнова",
            "description": "Подробное описание административного отчёта поддержки.",
        },
    )
    assert admin_report.status_code == 200

    reopened = repository.__class__(tmp_path / "support")
    assert reopened.get_report(report["report_id"])["status"] == "OPEN"


@pytest.mark.parametrize(
    "payload",
    [
        {"incident_id": "missing", "reporter_fio": "Иван", "description": "Подробное описание ошибки экспорта."},
        {"incident_id": "x", "reporter_fio": "И", "description": "Подробное описание ошибки экспорта."},
        {"incident_id": "x", "reporter_fio": "Иван", "description": "коротко"},
        {"incident_id": "x", "reporter_fio": "Иван", "description": "Подробное описание ошибки экспорта.", "role": "admin"},
    ],
)
def test_support_report_input_is_rejected_safely(support_context, payload):
    client, _, _, _, _ = support_context
    response = client.post("/api/support/reports", headers=_headers("colleague"), json=payload)
    assert response.status_code in {404, 422}


def test_admin_report_list_detail_snapshot_and_optimistic_status(support_context):
    client, _, repository, _, _ = support_context
    incident = _create_incident(repository)
    created = client.post(
        "/api/support/reports",
        headers=_headers("colleague"),
        json={
            "incident_id": incident["incident_id"],
            "reporter_fio": "Иван Петров",
            "description": "Подробное описание ошибки экспорта для поддержки.",
        },
    )
    report_id = created.json()["report_id"]

    user_list = client.get("/api/admin/support/reports", headers=_headers("colleague"))
    assert user_list.status_code == 403
    user_detail = client.get(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("colleague"),
    )
    assert user_detail.status_code == 403
    admin_list = client.get(
        "/api/admin/support/reports?limit=1&offset=0&status=OPEN",
        headers=_headers("averon"),
    )
    assert admin_list.status_code == 200
    assert len(admin_list.json()["reports"]) == 1
    detail = client.get(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("averon"),
    )
    assert detail.status_code == 200
    assert detail.json()["incident"]["document_available"] is False
    snapshot = client.get(
        f"/api/admin/support/reports/{report_id}/snapshot",
        headers=_headers("averon"),
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["incident_id"] == incident["incident_id"]

    user_snapshot = client.get(
        f"/api/admin/support/reports/{report_id}/snapshot",
        headers=_headers("colleague"),
    )
    assert user_snapshot.status_code == 403
    user_patch = client.patch(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("colleague"),
        json={"status": "RESOLVED", "version": 1},
    )
    assert user_patch.status_code == 403
    updated = client.patch(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("averon"),
        json={"status": "IN_PROGRESS", "version": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "IN_PROGRESS"
    assert updated.json()["version"] == 2
    stale = client.patch(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("averon"),
        json={"status": "RESOLVED", "version": 1},
    )
    assert stale.status_code == 409


def test_corrupt_snapshot_has_safe_error_but_detail_remains_available(support_context):
    client, _, repository, _, _ = support_context
    incident = _create_incident(repository)
    created = client.post(
        "/api/support/reports",
        headers=_headers("colleague"),
        json={
            "incident_id": incident["incident_id"],
            "reporter_fio": "Иван Петров",
            "description": "Подробное описание ошибки экспорта для поддержки.",
        },
    )
    report_id = created.json()["report_id"]
    (repository.incidents_dir / f"{incident['incident_id']}.json").write_text("{broken", encoding="utf-8")

    snapshot = client.get(
        f"/api/admin/support/reports/{report_id}/snapshot",
        headers=_headers("averon"),
    )
    detail = client.get(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("averon"),
    )
    assert snapshot.status_code == 409
    assert snapshot.json()["error"]["code"] == "SNAPSHOT_UNAVAILABLE"
    assert detail.status_code == 200
