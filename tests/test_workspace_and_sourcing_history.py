from __future__ import annotations

import json
import os
import re
from decimal import Decimal

from averon_import.services.jobs import JobService
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import Offer
from averon_import.services.sourcing.run_history import SourcingRunHistory
from averon_import.services.sourcing.service import SourcingService
from averon_import.services.workspace import WorkspaceService


def _write_metadata(service: WorkspaceService, document_id: str, *, title: str = "Документ") -> None:
    root = service.documents_dir / document_id
    root.mkdir(parents=True, exist_ok=True)
    service.write_json(root / "metadata.json", {
        "document_id": document_id,
        "filename": f"{title}.pdf",
        "title": title,
        "page_count": 58,
        "size": 1024,
    })


def test_document_listing_is_safe_newest_first_and_bounded(tmp_path):
    service = WorkspaceService(tmp_path)
    _write_metadata(service, "a" * 32, title="Old")
    _write_metadata(service, "b" * 32, title="New")
    old = service.documents_dir / ("a" * 32) / "metadata.json"
    new = service.documents_dir / ("b" * 32) / "metadata.json"
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    service.write_json(service.documents_dir / ("b" * 32) / "result.json", {"rows": []})

    listed = service.list_recent(limit=1)

    assert [item["document_id"] for item in listed] == ["b" * 32]
    assert listed[0]["has_result"] is True
    assert "root" not in listed[0]
    assert "pdf_path" not in listed[0]


def test_document_listing_skips_corrupt_metadata_and_marks_corrupt_result(tmp_path):
    service = WorkspaceService(tmp_path)
    corrupt_metadata = service.documents_dir / ("c" * 32)
    corrupt_metadata.mkdir()
    (corrupt_metadata / "metadata.json").write_text("{", encoding="utf-8")
    _write_metadata(service, "d" * 32, title="Incomplete")
    incomplete = service.documents_dir / ("d" * 32)
    (incomplete / "result.json").write_text("not-json", encoding="utf-8")
    (incomplete / "review_decisions.json").write_text("{}", encoding="utf-8")

    listed = service.list_recent()

    assert [item["document_id"] for item in listed] == ["d" * 32]
    assert listed[0]["available"] is False
    assert listed[0]["has_result"] is False
    assert listed[0]["has_review_decisions"] is True


def test_workspace_open_api_is_retrieval_only(monkeypatch, tmp_path):
    from averon_import import main

    service = WorkspaceService(tmp_path)
    _write_metadata(service, "e" * 32, title="Saved")
    workspace = service.get("e" * 32)
    service.write_json(workspace.result_path, {"rows": [{"id": "row-1"}]})
    monkeypatch.setattr(main, "workspace_service", service)

    payload = main.get_document("e" * 32)

    assert payload["document_id"] == "e" * 32
    assert payload["has_result"] is True
    assert payload["has_review_decisions"] is False


def test_sourcing_run_history_persists_sanitized_detail_and_reuses_retention(tmp_path):
    history = SourcingRunHistory(tmp_path / "sourcing_runs", retention_limit=3)
    base_result = {
        "positions_total": 1,
        "positions_processed": 1,
        "positions_matched": 1,
        "positions_alternatives": 0,
        "positions_review": 0,
        "positions_without_offers": 0,
        "confirmed_total": "100",
        "confirmed_totals": {"RUB": "100"},
        "confirmed_currency": "RUB",
        "alternative_total": None,
        "alternative_totals": {},
        "alternative_currency": None,
        "matched_unpriced_count": 0,
        "alternative_unpriced_count": 0,
        "unresolved_count": 0,
        "timings": {"total_s": 0.25},
    }
    telemetry = [{
        "source_row_id": "row-1",
        "source_page": 58,
        "source_row": 7,
        "intent_fingerprint": "f" * 64,
        "ai_mode": "qwen",
        "understanding_cache_hit": False,
        "understanding_provenance": {
            "kind": "fresh_qwen",
            "provider": "yandex",
            "model": "qwen-test",
            "parser_revision": "2",
            "latency_ms": 10.5,
        },
        "search_cache_hit": True,
        "decision": "MATCH",
        "recommended_offer_id": "offer-1",
        "review_candidate_offer_id": None,
        "matched_attributes": ["model"],
        "missing_attributes": [],
        "conflicting_attributes": [],
        "preferred_differences": [],
        "raw_qwen_payload": {"api_key": "secret"},
    }]

    run_ids = []
    for index in range(4):
        run_id = history.new_run_id()
        run_ids.append(run_id)
        history.write_completed(
            run_id=run_id,
            document_id="d" * 32,
            created_at=f"2026-01-01T00:00:0{index}+00:00",
            completed_at=f"2026-01-01T00:01:0{index}+00:00",
            provider_key="demo_store_http",
            provider_label="Averon Demo Store",
            catalog_version="3",
            result=base_result,
            row_telemetry=telemetry,
        )

    records = history.list_records()
    stored_text = " ".join(path.read_text(encoding="utf-8") for path in (tmp_path / "sourcing_runs").glob("*.json"))

    assert len(records) == 3
    assert history.get(run_ids[-1])["rows"][0]["intent_fingerprint"] == "f" * 64
    assert "raw_qwen_payload" not in stored_text
    assert "api_key" not in stored_text
    assert "secret" not in stored_text
    assert all("rows" not in item for item in history.list_public())


def test_failed_sourcing_run_is_safe_and_has_progress(tmp_path):
    history = SourcingRunHistory(tmp_path / "sourcing_runs")

    record = history.write_failed(
        run_id=history.new_run_id(),
        document_id="d" * 32,
        created_at="2026-01-01T00:00:00+00:00",
        provider_key="demo_store_http",
        provider_label="Averon Demo Store",
        catalog_version="3",
        positions_total=19,
        progress_current=4,
        progress_total=19,
        exc=RuntimeError("Authorization token must not appear"),
    )

    assert record["status"] == "failed"
    assert record["progress_current"] == 4
    assert record["progress_total"] == 19
    assert "token" not in json.dumps(record, ensure_ascii=False).casefold()
    assert "traceback" not in json.dumps(record, ensure_ascii=False).casefold()


class _HistoryProvider:
    key = "stub"
    label = "Тестовый поставщик"

    def stats(self):
        return {"catalog_version": "1", "item_count": 1}

    def search(self, intent, *, limit=20):
        return [Offer(
            offer_id="offer-1",
            provider=self.key,
            title="Клапан",
            price=Decimal("10"),
            currency="RUB",
            availability=True,
        )][:limit]


def test_project_telemetry_distinguishes_fresh_and_cached_fallback(tmp_path):
    provider = _HistoryProvider()
    service = SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    rows = [{"id": "row-1", "row_type": "item", "name": "Клапан", "quantity": "1"}]
    first_telemetry = []
    second_telemetry = []

    first = service.search_project(rows, telemetry=first_telemetry.append)
    second = service.search_project(rows, telemetry=second_telemetry.append)

    assert first.provider_key == "stub"
    assert first.catalog_version == "1"
    assert first_telemetry[0]["understanding_provenance"]["kind"] == "offline_deterministic_fallback"
    assert first_telemetry[0]["understanding_cache_hit"] is False
    assert second_telemetry[0]["understanding_provenance"]["kind"] == "cached_fallback"
    assert second_telemetry[0]["understanding_cache_hit"] is True
    assert second_telemetry[0]["search_cache_hit"] is True


def test_run_history_api_lists_and_returns_document_scoped_runs(monkeypatch, tmp_path):
    from averon_import import main

    service = WorkspaceService(tmp_path)
    _write_metadata(service, "f" * 32, title="Saved")
    workspace = service.get("f" * 32)
    history = SourcingRunHistory(workspace.sourcing_runs_dir)
    run_id = history.new_run_id()
    history.write_failed(
        run_id=run_id,
        document_id="f" * 32,
        created_at="2026-01-01T00:00:00+00:00",
        provider_key="stub",
        provider_label="Stub",
        catalog_version="1",
        positions_total=0,
        progress_current=0,
        progress_total=0,
        exc=RuntimeError("temporary failure"),
    )
    monkeypatch.setattr(main, "workspace_service", service)

    listed = main.list_sourcing_runs("f" * 32)
    detail = main.get_sourcing_run("f" * 32, run_id)

    assert listed["runs"][0]["run_id"] == run_id
    assert "rows" not in listed["runs"][0]
    assert detail["document_id"] == "f" * 32
    assert detail["status"] == "failed"


def test_document_sourcing_job_creates_completed_run_and_returns_run_identity(monkeypatch, tmp_path):
    from averon_import import main

    workspace_service = WorkspaceService(tmp_path)
    _write_metadata(workspace_service, "a" * 32, title="Saved")
    service = SourcingService(
        {"stub": _HistoryProvider()},
        default_provider="stub",
        cache=SourcingCache(tmp_path / "cache.json"),
    )
    jobs = JobService(max_workers=1)
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    monkeypatch.setattr(main, "sourcing_service", service)
    monkeypatch.setattr(main, "job_service", jobs)
    try:
        job = main.document_sourcing_search_all(
            "a" * 32,
            main.SourcingProjectRequest(rows=[{
                "id": "row-1",
                "row_type": "item",
                "name": "Клапан",
                "quantity": "1",
            }]),
        )
        deadline = 2
        while deadline and jobs.get(job["id"]).status not in {"completed", "failed"}:
            import time
            time.sleep(0.01)
            deadline -= 0.01
        completed = jobs.get(job["id"])
        assert completed.status == "completed"
        payload = completed.public()["result"]
        assert payload["run_id"]
        assert payload["catalog_version"] == "1"
        runs = main.list_sourcing_runs("a" * 32)["runs"]
        assert runs[0]["run_id"] == payload["run_id"]
        assert runs[0]["status"] == "completed"
    finally:
        jobs.executor.shutdown(wait=True)


def test_recent_document_ui_has_explicit_open_flow_without_recognition_call():
    from pathlib import Path

    root = Path(__file__).parents[1]
    html = (root / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    app_js = (root / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    open_flow = app_js[app_js.index("async function openExistingDocument"):app_js.index("async function uploadFile")]

    assert 'id="recent-documents-panel"' in html
    assert 'id="recent-documents-list"' in html
    assert 'api("/api/documents?limit=50")' in app_js
    assert "openExistingDocument(button.dataset.documentId)" in app_js
    assert "/recognize" not in open_flow
    assert "/suggest-pages" not in open_flow
    assert "localStorage.setItem(\"averonCurrentDocument\"" in open_flow
    assert re.search(r"api\(`/api/documents/\$\{encodedId\}/results`\)", open_flow)
