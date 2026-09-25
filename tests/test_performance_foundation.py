from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
from io import BytesIO
import json
from pathlib import Path
import threading

import pytest
from fastapi import HTTPException

from averon_import.services.document_mutation import DocumentMutationLocks
from averon_import.services.workspace import WorkspaceService


def _physical_refs(index: int) -> list[dict]:
    return [{"table": {"page_number": 1, "table_index": 0}, "row_index": index}]


def _review_row(index: int) -> dict:
    refs = _physical_refs(index)
    candidate = {"value_candidate": "2", "review_reason": "numeric_suspect"}
    return {
        "id": f"row-{index}",
        "page": 1,
        "row_type": "item",
        "status": "review",
        "name": f"Item {index}",
        "unit": "шт.",
        "quantity": "",
        "mass": "",
        "physical_row_refs": refs,
        "value_candidates": {"quantity": candidate},
        "ocr_metadata": {
            "physical_row_refs": refs,
            "raw_physical_cells": [{"row_index": index, "raw_text": str(index)}],
            "value_candidates": {"quantity": dict(candidate)},
        },
    }


def test_document_locks_serialize_one_document_without_cross_document_blocking():
    locks = DocumentMutationLocks()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()

    def hold_first():
        with locks.for_document("a"):
            first_entered.set()
            assert release_first.wait(2)

    def wait_same_document():
        second_started.set()
        with locks.for_document("a"):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(hold_first)
        assert first_entered.wait(2)
        second = pool.submit(wait_same_document)
        assert second_started.wait(2)
        with locks.for_document("b"):
            assert not second_entered.is_set()
        assert not second_entered.wait(0.05)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_entered.is_set()


def test_parallel_review_decisions_preserve_both_canonical_effects(monkeypatch, tmp_path):
    from averon_import import main

    workspace_service = WorkspaceService(tmp_path)
    document_id = "a" * 32
    root = workspace_service.documents_dir / document_id
    root.mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    workspace_service.write_json(workspace.metadata_path, {"document_id": document_id})
    workspace_service.write_json(workspace.result_path, {
        "document_fingerprint": "f" * 64,
        "revision": 0,
        "review_ledger_revision": 0,
        "rows": [_review_row(10), _review_row(20)],
        "page_statuses": {},
        "errors": [],
    })
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    monkeypatch.setattr(main.human_review_service, "document_fingerprint", lambda _path: "f" * 64)
    requests = [
        main.ReviewDecisionRequest(
            page=1,
            physical_refs=_physical_refs(index),
            decision="REJECT_CANDIDATE",
            field="quantity",
            candidate_value="2",
        )
        for index in (10, 20)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda request: main.save_review_decision(document_id, request),
                requests,
            )
        )

    stored_result = workspace_service.read_json(workspace.result_path)
    stored_ledger = json.loads(workspace.review_decisions_path.read_text(encoding="utf-8"))
    assert len(stored_ledger["decisions"]) == 2
    assert stored_ledger["revision"] == 2
    assert all(len(row["human_rejected_candidates"]) == 1 for row in stored_result["rows"])
    assert stored_result["revision"] == 2
    assert all(response["saved"] for response in responses)


def test_manual_save_rejects_stale_canonical_revision(monkeypatch, tmp_path):
    from averon_import import main

    workspace_service = WorkspaceService(tmp_path)
    document_id = "b" * 32
    root = workspace_service.documents_dir / document_id
    root.mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    workspace_service.write_json(workspace.metadata_path, {"document_id": document_id})
    workspace_service.write_json(workspace.result_path, {
        "revision": 3,
        "review_ledger_revision": 0,
        "rows": [_review_row(10)],
        "page_statuses": {},
        "errors": [],
    })
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    request = main.SaveRowsRequest(rows=[_review_row(10)], expected_revision=2)

    with pytest.raises(HTTPException) as error:
        main.save_results(document_id, request)

    assert error.value.status_code == 409
    assert workspace_service.read_json(workspace.result_path)["revision"] == 3


def test_recognition_final_commit_cannot_overwrite_concurrent_review(monkeypatch, tmp_path):
    from averon_import import main

    workspace_service = WorkspaceService(tmp_path)
    document_id = "c" * 32
    root = workspace_service.documents_dir / document_id
    root.mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    workspace_service.write_json(workspace.metadata_path, {
        "document_id": document_id,
        "page_count": 1,
    })
    base = {
        "document_fingerprint": "f" * 64,
        "revision": 0,
        "review_ledger_revision": 0,
        "rows": [_review_row(10)],
        "page_statuses": {},
        "errors": [],
    }
    workspace_service.write_json(workspace.result_path, base)
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    monkeypatch.setattr(main.human_review_service, "document_fingerprint", lambda _path: "f" * 64)
    monkeypatch.setattr(main.coordinator, "resolve", lambda *_args, **_kwargs: object())
    started = threading.Event()
    finish = threading.Event()

    def process_document(*_args, **_kwargs):
        started.set()
        assert finish.wait(3)
        return {
            "document_fingerprint": "f" * 64,
            "rows": [_review_row(10)],
            "page_statuses": {},
            "errors": [],
        }

    monkeypatch.setattr(main.coordinator, "process_document", process_document)

    class DeferredJobService:
        def submit(self, function):
            self.function = function
            return type("Job", (), {"public": lambda _self: {"id": "job"}})()

    jobs = DeferredJobService()
    monkeypatch.setattr(main, "job_service", jobs)
    main.recognize(
        document_id,
        main.RecognitionRequest(pages=[1], processing_mode="cloud"),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        recognition = pool.submit(jobs.function, lambda *_args: None)
        assert started.wait(2)
        main.save_review_decision(
            document_id,
            main.ReviewDecisionRequest(
                page=1,
                physical_refs=_physical_refs(10),
                decision="REJECT_CANDIDATE",
                field="quantity",
                candidate_value="2",
            ),
        )
        finish.set()
        with pytest.raises(RuntimeError, match="Документ изменён"):
            recognition.result(timeout=3)

    latest = workspace_service.read_json(workspace.result_path)
    assert latest["revision"] == 1
    assert latest["rows"][0]["human_rejected_candidates"]


def test_review_client_ignores_out_of_order_or_cross_document_response():
    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    start = source.index("async function submitHumanDecision(")
    end = source.index("\nfunction rowHtml(", start)
    handler = source[start:end]

    assert "state.document?.document_id !== documentId" in handler
    assert "isCurrentDocumentNavigation(navigationGeneration)" in handler
    assert "Number(response.revision || 0) < Number(state.result?.revision || 0)" in handler


def test_automatic_restore_does_not_gate_boot_and_is_cancelable():
    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    boot_start = source.index("async function boot()")
    boot_end = source.index("\nfunction updateCloudStatus(", boot_start)
    boot = source[boot_start:boot_end]
    restore_start = source.index("async function resumeLastDocument()")
    restore_end = source.index("\nasync function loadRecentDocuments(", restore_start)
    restore = source[restore_start:restore_end]
    open_start = source.index("async function openExistingDocument(")
    open_end = source.index("\nasync function uploadFile(", open_start)
    open_document = source[open_start:open_end]

    assert "await resumeLastDocument()" not in boot
    assert boot.index("state.bootComplete = true") < boot.index("void resumeLastDocument()")
    assert "beginDocumentNavigation({automaticRestore: true})" in restore
    assert "openExistingDocument(documentId, {announce: false, navigation})" in restore
    assert "navigation.signal" in open_document
    assert "isCurrentDocumentNavigation(navigation.generation)" in open_document
    assert open_document.index("/results`, {signal:navigation.signal})") < open_document.index("state.document = documentData")
    assert "setTimeout(" not in restore
    assert 'state.documentNavigationController.abort()' in source
    assert 'state.documentNavigationKind === "automatic_restore"' in source
    assert "function openManualWorkspace() {\n  cancelDocumentNavigation();" in source
    clear_start = source.index("function clearProtectedMemory()")
    clear_end = source.index("\nfunction cancelDocumentNavigation()", clear_start)
    assert "cancelDocumentNavigation();" in source[clear_start:clear_end]
    assert "performanceCounters.httpRequests" in source
    assert "performanceCounters.responseBytes" in source
    assert "performanceCounters.fullTableRenders" in source


def test_streamed_upload_persists_server_computed_pdf_fingerprint(monkeypatch, tmp_path):
    from fastapi import UploadFile

    from averon_import import main

    payload = b"streamed synthetic pdf bytes"
    service = WorkspaceService(tmp_path)
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(
        main.pdf_service,
        "inspect",
        lambda path: {"page_count": 1, "title": Path(path).stem},
    )

    response = asyncio.run(
        main.upload_document(UploadFile(filename="sample.pdf", file=BytesIO(payload)))
    )

    import hashlib

    workspace = service.get(response["document_id"])
    assert service.read_json(workspace.metadata_path)["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert "source_sha256" not in response
    assert service.metrics["source_fingerprint_calculations"] == 1


def test_cached_page_preview_does_not_rasterize_again(monkeypatch, tmp_path):
    from averon_import import main

    service = WorkspaceService(tmp_path)
    document_id = "d" * 32
    root = service.documents_dir / document_id
    root.mkdir()
    workspace = service.get(document_id)
    service.write_json(workspace.metadata_path, {"document_id": document_id, "page_count": 1})
    cached_page = workspace.pages_dir / "page-1-110.png"
    cached_page.write_bytes(b"cached png")
    monkeypatch.setattr(main, "workspace_service", service)
    monkeypatch.setattr(
        main.pdf_service,
        "render_page_to_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    response = main.page_image(document_id, 1, dpi=110)

    assert Path(response.path) == cached_page
