from averon_import.application import create_app
from averon_import.core.constants import APP_VERSION


def test_application_factory_uses_isolated_data_dir(tmp_path):
    app = create_app(tmp_path / "data")
    assert app.version == APP_VERSION
    assert app.state.services.workspace.data_dir == (tmp_path / "data").resolve()


def test_health_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "health-data")
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["version"] == APP_VERSION


def test_upload_limit_returns_413(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from averon_import.api import documents

    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 4)
    app = create_app(tmp_path / "upload-data")
    response = TestClient(app).post(
        "/api/documents",
        files={"file": ("large.pdf", b"12345", "application/pdf")},
    )
    assert response.status_code == 413
