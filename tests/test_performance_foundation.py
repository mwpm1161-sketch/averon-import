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


def test_manual_save_returns_authoritative_canonical_result_in_one_response(monkeypatch, tmp_path):
    from averon_import import main

    workspace_service = WorkspaceService(tmp_path)
    document_id = "e" * 32
    root = workspace_service.documents_dir / document_id
    root.mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    workspace_service.write_json(workspace.metadata_path, {
        "document_id": document_id,
        "source_sha256": "f" * 64,
    })
    initial_row = _review_row(10)
    workspace_service.write_json(workspace.result_path, {
        "document_fingerprint": "f" * 64,
        "revision": 0,
        "review_ledger_revision": 0,
        "rows": [initial_row],
        "page_statuses": {},
        "errors": [],
    })
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    edited = _review_row(10)
    edited["name"] = "Edited item"
    edited["status"] = "edited"
    edited["edited"] = True
    edited["edited_fields"] = ["name"]

    response = main.save_results(
        document_id,
        main.SaveRowsRequest(rows=[edited], expected_revision=0),
    )

    stored = workspace_service.read_json(workspace.result_path)
    assert response["saved"] is True
    assert response["revision"] == 1
    assert response["result"] == stored
    assert response["result"]["rows"][0]["name"] == "Edited item"


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
    start = source.index("function submitHumanDecision(")
    end = source.index("\nfunction rowHtml(", start)
    handler = source[start:end]

    assert "state.document?.document_id !== documentId" in handler
    assert "isCurrentDocumentNavigation(navigationGeneration)" in handler
    assert "Number(patch?.revision || 0) < Number(state.result?.revision || 0)" in handler
    assert "state.reviewMutationQueues.get(documentId)" in handler
    assert "previous.then(send, send)" in handler
    assert "mergeHumanReviewPatch(patch)" in handler
    assert "loadResult(response.result" not in handler


def test_incremental_review_decision_returns_only_affected_canonical_rows(monkeypatch, tmp_path):
    from averon_import import main
    from averon_import.services.row_assembler import SpecificationRowAssembler

    workspace_service = WorkspaceService(tmp_path)
    document_id = "d" * 32
    root = workspace_service.documents_dir / document_id
    root.mkdir()
    workspace = workspace_service.get(document_id)
    workspace.pdf_path.write_bytes(b"synthetic pdf")
    workspace_service.write_json(workspace.metadata_path, {
        "document_id": document_id,
        "source_sha256": "f" * 64,
    })
    rows = [_review_row(index) for index in range(10, 510)]
    source_evidence = json.loads(json.dumps(rows[0]["ocr_metadata"]))
    page_status = {
        "page": 1,
        "layout_status": "TRUSTED",
        "schema_status": "SUPPORTED",
        "page_disposition": "SPEC_OUTPUT",
        "output_status": "REVIEW_REQUIRED",
        "blockers": ["critical_value_missing"],
        "diagnostics": {
            "selected_mode": "geometry_first",
            "page_disposition": {"disposition": "SPEC_OUTPUT"},
        },
    }
    second_page_status = json.loads(json.dumps(page_status))
    second_page_status["page"] = 2
    second_page_status["blockers"] = ["structural_layout_ambiguous"]
    workspace_service.write_json(workspace.result_path, {
        "document_fingerprint": "f" * 64,
        "revision": 0,
        "review_ledger_revision": 0,
        "rows": rows,
        "page_statuses": {"1": page_status, "2": second_page_status},
        "errors": [],
        "summary": SpecificationRowAssembler.summary(rows, []),
    })
    monkeypatch.setattr(main, "workspace_service", workspace_service)
    monkeypatch.setattr(main.human_review_service, "document_fingerprint", lambda _path: "f" * 64)
    before = dict(main.human_review_service.metrics)
    untouched_page_status = workspace_service.read_json(workspace.result_path)["page_statuses"]["2"]
    io_before = dict(workspace_service.metrics)
    request = main.ReviewDecisionRequest(
        page=1,
        physical_refs=_physical_refs(10),
        decision="REJECT_CANDIDATE",
        field="quantity",
        candidate_value="2",
    )

    response = main.save_review_decision(document_id, request)
    io_after_action = dict(workspace_service.metrics)
    stored = workspace_service.read_json(workspace.result_path)
    patch = response["result_patch"]
    assert "result" not in response
    assert len(patch["rows"]) == 1
    assert patch["rows"][0]["id"] == "row-10"
    assert patch["revision"] == 1
    assert len(json.dumps(response).encode("utf-8")) < len(json.dumps(stored).encode("utf-8")) // 10
    assert stored["rows"][0]["ocr_metadata"] == source_evidence
    assert stored["rows"][1] == rows[1]
    assert stored["summary"] == SpecificationRowAssembler.summary(stored["rows"], [])
    assert set(patch["page_statuses"]) == {"1"}
    assert stored["page_statuses"]["2"] == untouched_page_status
    assert main.human_review_service.metrics["review_decisions_replayed"] == before["review_decisions_replayed"]
    assert main.human_review_service.metrics["full_result_copies"] == before["full_result_copies"]
    assert main.human_review_service.metrics["review_rows_detached"] == before["review_rows_detached"] + 1
    assert main.human_review_service.metrics["page_safety_pages_recalculated"] == before["page_safety_pages_recalculated"] + 1
    assert io_after_action["result_reads"] == io_before["result_reads"] + 1
    assert io_after_action["result_writes"] == io_before["result_writes"] + 1
    assert io_after_action["source_fingerprint_calculations"] == io_before["source_fingerprint_calculations"]


def test_review_patch_frontend_updates_only_changed_rows_and_keeps_view_state():
    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    start = source.index("function renderPatchedReviewRows(")
    end = source.index("\nfunction submitHumanDecision(", start)
    patch_renderer = source[start:end]

    assert "for (const row of changedRows)" in patch_renderer
    assert "body.innerHTML" not in patch_renderer
    assert "renderRows()" not in patch_renderer
    assert "const tableScroll = resultTableScrollPosition()" in patch_renderer
    assert "restoreResultTableScroll(tableScroll)" in patch_renderer
    assert "updateSummary()" in patch_renderer
    assert 'id="result-table-scroll"' in Path("averon_import/templates/index.html").read_text(encoding="utf-8")
    assert '$("#result-table-scroll")' in source
    assert "scroller.scrollTop" in source
    assert "scroller.scrollLeft" in source

    start = source.index("function mergeHumanReviewPatch(")
    end = source.index("\nfunction submitHumanDecision(", start)
    merge = source[start:end]
    assert "state.result.page_statuses" in merge
    assert "state.result.revision" in merge
    assert "state.result.review_ledger_revision" in merge
    assert "refreshClientReview(merged)" in merge


def test_review_mutation_toasts_are_fenced_by_document_navigation():
    source = Path("averon_import/static/app.js").read_text(encoding="utf-8")
    start = source.index("function submitHumanDecision(")
    end = source.index("\nfunction physicalRefs", start)
    handler = source[start:end]
    api = source.split("async function api(", 1)[1].split("function readCsrfCookie", 1)[0]

    assert "state.document?.document_id === documentId" in handler
    assert "isCurrentDocumentNavigation(navigationGeneration)" in handler
    assert "toast(error.message, \"error\")" in handler
    assert "if (response.status === 401 && url !== \"/api/auth/login\") handleSessionExpired();" in api


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
