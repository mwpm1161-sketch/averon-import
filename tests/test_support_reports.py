from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
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
            "revision": 0,
            "review_ledger_revision": 0,
            "rows": [{
                "id": "canonical-row",
                "page": 1,
                "row_type": "item",
                "status": "recognized",
                "selected": True,
                "name": "Каноническая строка",
                "position": "1",
                "unit": "шт.",
                "quantity": "1",
            }],
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


def _export_payload(filename="failed.xlsx", *, expected_revision=0, review_export=False):
    return {
        "columns": ["name"],
        "rows": [{"page": 1, "name": "Насос", "row_type": "item"}],
        "expected_revision": expected_revision,
        "include_headers": True,
        "only_exportable": True,
        "filename": filename,
        "sheet_name": "Спецификация",
        "review_export": review_export,
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


def test_v1_export_incidents_reports_and_snapshot_hash_survive_transactional_upgrade(tmp_path):
    from averon_import.services.support_reports import SCHEMA_VERSION, SupportRepository

    support_dir = tmp_path / "support"
    incidents_dir = support_dir / "incidents"
    incidents_dir.mkdir(parents=True)
    incident_id = "exp-legacy"
    report_id = "rpt-legacy"
    snapshot = {
        "schema_version": 1,
        "incident_id": incident_id,
        "document_id": "d" * 32,
        "error_code": "EXPORT_FAILED",
    }
    snapshot_bytes = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    (incidents_dir / f"{incident_id}.json").write_bytes(snapshot_bytes)

    connection = sqlite3.connect(support_dir / "support.sqlite3")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE export_incidents (
            incident_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'export',
            error_code TEXT NOT NULL,
            public_message TEXT NOT NULL,
            http_status INTEGER NOT NULL,
            app_version TEXT NOT NULL,
            export_kind TEXT NOT NULL,
            requested_filename TEXT,
            row_count INTEGER,
            snapshot_path TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL
        );
        CREATE TABLE support_reports (
            report_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL UNIQUE,
            reporter_fio TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (incident_id) REFERENCES export_incidents(incident_id)
        );
        CREATE INDEX idx_support_reports_status_created
            ON support_reports(status, created_at);
        CREATE INDEX idx_export_incidents_document ON export_incidents(document_id);
        CREATE INDEX idx_export_incidents_created ON export_incidents(created_at);
        """
    )
    connection.execute(
        """
        INSERT INTO export_incidents (
            incident_id, document_id, created_at, username, role, stage,
            error_code, public_message, http_status, app_version, export_kind,
            requested_filename, row_count, snapshot_path, snapshot_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            incident_id, "d" * 32, "2026-01-02T03:04:05+00:00", "colleague",
            "user", "export", "EXPORT_FAILED", "Export failed", 500,
            "legacy-version", "production", "failed.xlsx", 3,
            f"incidents/{incident_id}.json", hashlib.sha256(snapshot_bytes).hexdigest(),
        ),
    )
    connection.execute(
        """
        INSERT INTO support_reports (
            report_id, incident_id, reporter_fio, description, status,
            created_at, updated_at, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id, incident_id, "Иван Петров", "Existing linked report description",
            "IN_PROGRESS", "2026-01-02T03:05:00+00:00", "2026-01-02T03:06:00+00:00", 4,
        ),
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    repository = SupportRepository(support_dir)
    incident = repository.get_incident(incident_id)
    report = repository.get_report(report_id)
    assert SCHEMA_VERSION == 2
    assert incident["incident_kind"] == "export_failure"
    assert incident["snapshot_sha256"] == hashlib.sha256(snapshot_bytes).hexdigest()
    assert report["report_id"] == report_id
    assert report["incident_kind"] == "export_failure"
    assert report["status"] == "IN_PROGRESS" and report["version"] == 4
    assert (incidents_dir / f"{incident_id}.json").read_bytes() == snapshot_bytes
    assert repository.snapshot_for_report(report_id) == snapshot
    with repository._connect() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        report_foreign_key = migrated.execute(
            "PRAGMA foreign_key_list(support_reports)"
        ).fetchone()
        assert report_foreign_key["table"] == "support_incidents"
        assert migrated.execute("SELECT COUNT(*) FROM support_reports").fetchone()[0] == 1


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
    with repository._connect() as connection:
        incident_count = connection.execute("SELECT COUNT(*) FROM support_incidents").fetchone()[0]
        report_count = connection.execute("SELECT COUNT(*) FROM support_reports").fetchone()[0]
    assert incident_count == 0
    assert list(repository.incidents_dir.glob("*.json")) == []
    assert report_count == 0
    assert repository.list_reports(limit=100, offset=0) == []
    assert (workspace.exports_dir / "success.xlsx").is_file()


@pytest.mark.parametrize("review_export", [False, True])
def test_export_rejects_stale_revision_without_creating_workbook(
    support_context, monkeypatch, review_export
):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    stored = workspace_service.read_json(workspace.result_path)
    stored["revision"] = 11
    workspace_service.write_json(workspace.result_path, stored)
    export_calls = []
    monkeypatch.setattr(main.export_service, "export", lambda **kwargs: export_calls.append(kwargs))
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("stale.xlsx", expected_revision=10, review_export=review_export),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Документ изменён. Обновите данные перед экспортом."
    assert export_calls == []
    assert not list(workspace.exports_dir.glob("*.xlsx"))
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_incidents").fetchone()[0] == 0


def test_matching_revision_export_uses_canonical_saved_rows_outside_document_lock(
    support_context, monkeypatch
):
    client, main, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    stored = workspace_service.read_json(workspace.result_path)
    stored["revision"] = 10
    workspace_service.write_json(workspace.result_path, stored)
    captured = {}

    def write_export(**kwargs):
        acquired = []

        def acquire_document_lock():
            with main.document_mutation_locks.for_document(workspace.document_id):
                acquired.append(True)

        import threading
        worker = threading.Thread(target=acquire_document_lock)
        worker.start()
        worker.join(timeout=2)
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"PK-test-workbook")
        assert acquired == [True]

    monkeypatch.setattr(main.export_service, "export", write_export)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("canonical.xlsx", expected_revision=10),
    )

    assert response.status_code == 200
    assert captured["rows"] == stored["rows"]
    assert captured["rows"][0]["name"] == "Каноническая строка"
    assert captured["page_statuses"] == stored["page_statuses"]


def test_dirty_save_revision_is_used_for_the_following_export(support_context, monkeypatch):
    client, main, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    monkeypatch.setattr(main, "_source_fingerprint", lambda _workspace: "f" * 64)
    rows = workspace_service.read_json(workspace.result_path)["rows"]
    rows[0]["name"] = "Сохранённая правка"
    saved = client.request(
        "PUT",
        f"/api/documents/{workspace.document_id}/results",
        headers=_headers("colleague"),
        json={"rows": rows, "expected_revision": 0},
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    captured = {}

    def write_export(**kwargs):
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"PK-test-workbook")

    monkeypatch.setattr(main.export_service, "export", write_export)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("saved.xlsx", expected_revision=saved.json()["revision"]),
    )

    assert response.status_code == 200
    assert captured["rows"][0]["name"] == "Сохранённая правка"


def test_second_client_mutation_blocks_first_clients_clean_export(support_context, monkeypatch):
    client, main, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    monkeypatch.setattr(main, "_source_fingerprint", lambda _workspace: "f" * 64)
    rows = workspace_service.read_json(workspace.result_path)["rows"]
    rows[0]["name"] = "Правка второго клиента"
    updated = client.request(
        "PUT",
        f"/api/documents/{workspace.document_id}/results",
        headers=_headers("second-client"),
        json={"rows": rows, "expected_revision": 0},
    )
    assert updated.status_code == 200
    export_calls = []
    monkeypatch.setattr(main.export_service, "export", lambda **kwargs: export_calls.append(kwargs))

    stale = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("first-client"),
        json=_export_payload("first-client.xlsx", expected_revision=0),
    )

    assert stale.status_code == 409
    assert export_calls == []
    assert not (workspace.exports_dir / "first-client.xlsx").exists()


def test_export_reports_corrupt_review_ledger_safely_without_repairing_it(support_context, monkeypatch):
    client, main, _, workspace_service, _ = support_context
    from averon_import.services.review_decisions import ReviewDecisionStore

    workspace = _make_workspace(workspace_service)
    store = ReviewDecisionStore(workspace.review_decisions_path)
    store.path.write_bytes(b"{broken ledger")
    store.revision_path.write_text("3", encoding="ascii")
    result_before = workspace.result_path.read_bytes()
    ledger_before = store.path.read_bytes()
    marker_before = store.revision_path.read_bytes()
    export_calls = []
    monkeypatch.setattr(main.export_service, "export", lambda **kwargs: export_calls.append(kwargs))

    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("corrupt-ledger.xlsx"),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "История ручной проверки повреждена. Требуется восстановление."
    assert str(store.path) not in response.text
    assert export_calls == []
    assert workspace.result_path.read_bytes() == result_before
    assert store.path.read_bytes() == ledger_before
    assert store.revision_path.read_bytes() == marker_before
    assert not (workspace.exports_dir / "corrupt-ledger.xlsx").exists()


def test_production_export_remains_blocked_by_canonical_structural_blocker(support_context):
    client, _, _, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    stored = workspace_service.read_json(workspace.result_path)
    stored["rows"][0]["critical_blockers"] = ["structural_layout_ambiguous"]
    workspace_service.write_json(workspace.result_path, stored)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload("blocked.xlsx"),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert not (workspace.exports_dir / "blocked.xlsx").exists()


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


def test_export_cleanup_failure_does_not_mask_safe_error(support_context, monkeypatch):
    client, main, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)

    def fail_export(*args, **kwargs):
        raise ValueError("Не удалось проверить экспорт")

    original_unlink = Path.unlink

    def fail_temp_unlink(path, missing_ok=False):
        if path.name.startswith(".averon-export-"):
            raise PermissionError("diagnostic cleanup failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(main.export_service, "export", fail_export)
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=_export_payload(),
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["detail"] == "Не удалось проверить экспорт"
    assert payload["error"]["code"] == "EXPORT_VALIDATION_FAILED"
    assert payload["error"]["incident_id"]
    assert payload["error"]["reportable"] is True
    assert "diagnostic cleanup failure" not in response.text
    assert repository.get_incident(payload["error"]["incident_id"])["error_code"] == "EXPORT_VALIDATION_FAILED"


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
    source_pdf = workspace.root / "source.pdf"
    source_pdf.write_bytes(b"%PDF-test-source")
    source_pdf_before = source_pdf.read_bytes()

    monkeypatch.setattr(main.export_service, "export", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad export")))
    request_payload = _export_payload()
    response = client.post(
        f"/api/documents/{workspace.document_id}/export",
        headers=_headers("colleague"),
        json=request_payload,
    )
    incident = repository.get_incident(response.json()["error"]["incident_id"])
    assert incident["incident_kind"] == "export_failure"
    snapshot_path = repository.incidents_dir / f"{incident['incident_id']}.json"
    before = snapshot_path.read_bytes()
    workspace_service.write_json(workspace.result_path, {"summary": {"total_rows": 999}})
    after = snapshot_path.read_bytes()

    assert before == after
    assert hashlib.sha256(after).hexdigest() == incident["snapshot_sha256"]
    snapshot = json.loads(after.decode("utf-8"))
    assert snapshot["incident_kind"] == "export_failure"
    assert snapshot["export_request"] == request_payload
    assert snapshot["page_statuses"]["1"]["output_status"] == "USABLE"
    assert source_pdf.exists()
    assert source_pdf.read_bytes() == source_pdf_before
    assert len(list(repository.incidents_dir.glob("*.json"))) == 1
    assert list(repository.support_dir.rglob("*.pdf")) == []


def test_snapshot_sanitizes_nested_secrets_and_paths_without_losing_rows(support_context):
    _, _, repository, _, _ = support_context
    incident = repository.create_export_incident(
        document_id="d" * 32,
        username="colleague",
        role="user",
        app_version="test-version",
        export_kind="production",
        requested_filename="failed.xlsx",
        row_count=1,
        document={"filename": "specification.pdf", "page_count": 1, "size": 10},
        export_request={
            "columns": ["name", "quantity", "unit"],
            "rows": [{
                "name": "Насос",
                "quantity": "2",
                "unit": "шт",
                "nested": {
                    "api_key": "api-secret",
                    "password": "password-secret",
                    "proxy_token": "proxy-secret",
                    "authorization": "Bearer secret",
                    "windows_path": "C:\\private\\source.pdf",
                    "linux_path": "/var/lib/averon/source.pdf",
                },
            }],
        },
        page_statuses={},
        result_summary={},
        resolved_filename="failed.xlsx",
        error_code="EXPORT_FAILED",
        public_message="Не удалось сформировать Excel.",
        http_status=500,
    )

    snapshot_path = repository.incidents_dir / f"{incident['incident_id']}.json"
    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(snapshot_text)
    row = snapshot["export_request"]["rows"][0]
    assert row["name"] == "Насос"
    assert row["quantity"] == "2"
    assert row["unit"] == "шт"
    assert "api_key" not in snapshot_text
    assert "password" not in snapshot_text
    assert "proxy-secret" not in snapshot_text
    assert "Bearer secret" not in snapshot_text
    assert "C:\\private\\source.pdf" not in snapshot_text
    assert "/var/lib/averon/source.pdf" not in snapshot_text
    assert "[REDACTED_PATH]" in snapshot_text


def test_user_reported_incident_validation_authorization_and_server_owned_snapshot(support_context):
    client, _, repository, workspace_service, _ = support_context
    workspace = _make_workspace(workspace_service)
    stored_result = {
        "rows": [{
            "page": 1,
            "name": "Насос",
            "quantity": "2",
            "api_key": "provider-api-key-secret",
            "password_hash": "password-hash-secret",
            "session_token": "session-token-secret",
            "csrf_token": "csrf-token-secret",
            "proxy_secret": "proxy-secret-value",
            "credentials": {"password": "credential-password-secret"},
            "authorization": "Bearer authorization-secret",
            "traceback": "C:\\private\\traceback.txt",
            "source_path": "C:\\private\\source.pdf",
        }],
        "page_statuses": {"1": {"output_status": "USABLE", "status": "REVIEW_REQUIRED"}},
        "summary": {"total_rows": 1, "exportable_rows": 0},
    }
    workspace_service.write_json(workspace.result_path, stored_result)
    payload = {"document_id": workspace.document_id, "stage": "review"}

    assert client.get("/api/me").status_code == 401
    unauthenticated = client.post("/api/support/incidents", json=payload)
    assert unauthenticated.status_code == 401
    invalid_stage = client.post(
        "/api/support/incidents",
        headers=_headers("colleague"),
        json={**payload, "stage": "arbitrary-stage"},
    )
    assert invalid_stage.status_code == 422
    injected_snapshot = client.post(
        "/api/support/incidents",
        headers=_headers("colleague"),
        json={**payload, "snapshot": {"password": "browser-secret", "rows": [{"name": "forged"}]}},
    )
    assert injected_snapshot.status_code == 422
    missing_document = client.post(
        "/api/support/incidents",
        headers=_headers("colleague"),
        json={"document_id": "e" * 32, "stage": "document"},
    )
    assert missing_document.status_code == 404
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_incidents").fetchone()[0] == 0

    created = client.post("/api/support/incidents", headers=_headers("colleague"), json=payload)
    assert created.status_code == 200
    incident_id = created.json()["incident_id"]
    assert created.json()["incident_kind"] == "user_reported"
    incident = repository.get_incident(incident_id)
    assert incident["incident_kind"] == "user_reported"
    assert incident["stage"] == "review"
    assert incident["error_code"] is None
    assert incident["http_status"] is None
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_reports").fetchone()[0] == 0

    admin_created = client.post(
        "/api/support/incidents",
        headers=_headers("averon"),
        json={"document_id": workspace.document_id, "stage": "pages"},
    )
    assert admin_created.status_code == 200
    assert repository.get_incident(admin_created.json()["incident_id"])["role"] == "admin"

    snapshot_path = repository.incidents_dir / f"{incident_id}.json"
    original_snapshot_bytes = snapshot_path.read_bytes()
    original_snapshot = json.loads(original_snapshot_bytes.decode("utf-8"))
    snapshot_text = original_snapshot_bytes.decode("utf-8")
    assert original_snapshot["schema_version"] == 1
    assert original_snapshot["incident_kind"] == "user_reported"
    assert original_snapshot["stage"] == original_snapshot["reported_stage"] == "review"
    assert original_snapshot["user"] == {"username": "colleague", "role": "user"}
    assert original_snapshot["document"] == {
        "filename": "specification.pdf",
        "title": "Test specification",
        "page_count": 3,
        "size": 1234,
    }
    assert original_snapshot["rows"][0]["name"] == "Насос"
    assert original_snapshot["page_statuses"]["1"]["output_status"] == "USABLE"
    assert original_snapshot["result_summary"]["total_rows"] == 1
    for forbidden in (
        "provider-api-key-secret", "password-hash-secret", "session-token-secret",
        "csrf-token-secret", "proxy-secret-value", "credential-password-secret",
        "authorization-secret", "C:\\private\\traceback.txt", "C:\\private\\source.pdf",
    ):
        assert forbidden not in snapshot_text
    assert '"api_key"' not in snapshot_text
    assert '"traceback"' not in snapshot_text
    assert "[REDACTED_PATH]" in snapshot_text

    report_payload = {
        "incident_id": incident_id,
        "reporter_fio": "Иван Петров",
        "description": "Пользователь сообщает о неверном отображении текущего результата.",
    }
    created_report = client.post("/api/support/reports", headers=_headers("colleague"), json=report_payload)
    assert created_report.status_code == 200
    assert created_report.json()["incident"]["incident_kind"] == "user_reported"
    assert created_report.json()["incident"]["error_code"] is None
    assert "snapshot_path" not in created_report.json()["incident"]

    foreign_report = client.post("/api/support/reports", headers=_headers("other"), json=report_payload)
    assert foreign_report.status_code == 403
    duplicate_report = client.post("/api/support/reports", headers=_headers("colleague"), json=report_payload)
    assert duplicate_report.status_code == 409

    report_id = created_report.json()["report_id"]
    user_detail = client.get(
        f"/api/admin/support/reports/{report_id}", headers=_headers("colleague")
    )
    assert user_detail.status_code == 403
    admin_list = client.get("/api/admin/support/reports?limit=100&offset=0", headers=_headers("averon"))
    assert admin_list.status_code == 200
    matching_summary = next(item for item in admin_list.json()["reports"] if item["report_id"] == report_id)
    assert matching_summary["incident_kind"] == "user_reported"
    admin_detail = client.get(
        f"/api/admin/support/reports/{report_id}", headers=_headers("averon")
    )
    assert admin_detail.status_code == 200
    assert admin_detail.json()["incident"]["incident_kind"] == "user_reported"
    snapshot_response = client.get(
        f"/api/admin/support/reports/{report_id}/snapshot", headers=_headers("averon")
    )
    assert snapshot_response.status_code == 200
    assert snapshot_response.json() == original_snapshot
    assert hashlib.sha256(original_snapshot_bytes).hexdigest() == incident["snapshot_sha256"]

    workspace_service.write_json(workspace.result_path, {"rows": [{"name": "changed"}]})
    assert snapshot_path.read_bytes() == original_snapshot_bytes
    shutil.rmtree(workspace.root)
    unavailable_detail = client.get(
        f"/api/admin/support/reports/{report_id}", headers=_headers("averon")
    )
    assert unavailable_detail.json()["incident"]["document_available"] is False
    retained_snapshot = client.get(
        f"/api/admin/support/reports/{report_id}/snapshot", headers=_headers("averon")
    )
    assert retained_snapshot.status_code == 200
    assert retained_snapshot.json() == original_snapshot


def test_snapshot_cleanup_failure_does_not_mask_persistence_error(support_context, monkeypatch):
    _, _, repository, _, _ = support_context

    def fail_connect():
        raise RuntimeError("primary persistence failure")

    def fail_unlink(path, missing_ok=False):
        raise PermissionError("diagnostic snapshot cleanup failure")

    monkeypatch.setattr(repository, "_connect", fail_connect)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(RuntimeError, match="primary persistence failure"):
        repository.create_export_incident(
            document_id="d" * 32,
            username="colleague",
            role="user",
            app_version="test-version",
            export_kind="production",
            requested_filename="failed.xlsx",
            row_count=1,
            document={"filename": "specification.pdf"},
            export_request=_export_payload(),
            page_statuses={},
            result_summary={},
            resolved_filename="failed.xlsx",
            error_code="EXPORT_FAILED",
            public_message="Не удалось сформировать Excel.",
            http_status=500,
        )

    monkeypatch.undo()
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_incidents").fetchone()[0] == 0


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
    assert admin_list.json()["reports"][0]["incident_kind"] == "export_failure"
    detail = client.get(
        f"/api/admin/support/reports/{report_id}",
        headers=_headers("averon"),
    )
    assert detail.status_code == 200
    assert detail.json()["incident"]["incident_kind"] == "export_failure"
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
