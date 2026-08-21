from __future__ import annotations

from fastapi.testclient import TestClient

from averon_import.ai.models import AiReviewSuggestion
from averon_import.ai.service import AiService
from averon_import.application import create_app


class FakeProvider:
    def health(self):
        return {"available": True, "status": "ready", "provider": "fake", "model": "test"}

    def review_row(self, row, *, image_bytes=None):
        return AiReviewSuggestion(
            fields={"name": "Клапан исправленный"},
            uncertain_fields=[],
            provider="fake",
            model="test",
        )


def test_ai_review_endpoint_is_non_destructive(tmp_path):
    app = create_app(tmp_path / "data")
    services = app.state.services
    services.ai = AiService(FakeProvider(), enabled=True)

    source = tmp_path / "source.pdf"
    source.write_bytes(b"placeholder")
    workspace = services.workspace.create(
        source,
        {"filename": "source.pdf", "page_count": 1, "size": len(b"placeholder")},
    )
    original = {
        "pages": [1],
        "rows": [{
            "id": "row-1",
            "name": "Клапан",
            "page": 1,
            "confidence": 80.0,
            "critical_confidence": 60.0,
            "status": "review",
            "row_type": "item",
            "bbox": None,
        }],
        "page_tables": {},
        "errors": [],
        "summary": {},
        "ocr_mode": "standard",
    }
    services.workspace.write_result(workspace, original)
    before = services.workspace.read_result(workspace)

    response = TestClient(app).post(
        f"/api/documents/{workspace.document_id}/rows/row-1/ai-review"
    )
    assert response.status_code == 200
    assert response.json()["fields"]["name"] == "Клапан исправленный"
    assert services.workspace.read_result(workspace) == before
