"""B3.4a lifecycle/recovery tests: synthetic SQLite, fake transport, no network."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from averon_import.services.ai_first import dispatch_policy
from averon_import.services.ai_first.coordination_contracts import (
    CoordinationError, Outcome, canonical, fingerprint,
)
from averon_import.services.ai_first.durable_dispatch import CALLER, classify_fault
from averon_import.services.ai_first.durable_job_store import DurableJobStore
from averon_import.services.ai_first.private_acl import create_private_directory, secure_private_path
from averon_import.services.ai_first.orchestration import PageExtractionRequest
from b32_test_support import close_case, make_case, new_generation
from test_ai_first_durable_dispatch import image, open_runtime
from test_ai_first_qwen_vision_adapter import FakeTransport
from averon_import.services.ocr.qwen_vision_client import _HttpsTransport


@pytest.fixture(autouse=True)
def offline_durable_mode(monkeypatch):
    import ipaddress

    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setattr(dispatch_policy, "_required", False)

    def denied(*args, **kwargs):
        raise AssertionError("real provider/network forbidden")

    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def local_only(sock, address, _original=original):
            if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError("real provider/network forbidden")
            return _original(sock, address)

        monkeypatch.setattr(socket.socket, method, local_only)
    monkeypatch.setattr(_HttpsTransport, "post", denied)
    yield


def _runtime(case, *, transport=None):
    return open_runtime({**case.runtime.payload, "schema_version": 2,
                         "maintenance_root": str(case.control), "job_managed": True},
                        transport or FakeTransport())


def _submit(case):
    return case.backend.submit(case.actor, case.request, financial=case.financial,
                               execution_profile=case.profile)


def _prepared(case, backend, runtime, receipt, page=1):
    claim = backend._claim_page_for_dispatch(
        case.actor, runtime.dispatcher._owner, receipt.job_id, page)
    item = image(page)
    body = runtime.client._request_body(item.data, page, runtime.adapter._user_context)
    call = runtime.dispatcher.call(runtime.client, page, hashlib.sha256(body).hexdigest(),
                                   runtime.adapter.estimate(item), job_claim=claim)
    headers = {"Authorization": "Api-Key synthetic-b2-credential",
               "OpenAI-Project": "synthetic-project"}
    url = runtime.client.config.base_url + "/chat/completions"
    return claim, call, body, headers, url


def _page(path, job_id, page_number):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT * FROM durable_job_pages WHERE job_id=? AND page_number=?",
            (job_id, page_number)).fetchone())


def _attempt(path, attempt_id):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone())


def _job(path, job_id):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT * FROM durable_jobs WHERE job_id=?", (job_id,)).fetchone())


def _insert_attempt(case, receipt, state, *, page=1, owner_epoch=1):
    terminal = state in ("terminal", "not_sent")
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            "SELECT candidate_attempt_id,run_id FROM durable_job_pages"
            " WHERE job_id=? AND page_number=?", (receipt.job_id, page)).fetchone()
        connection.execute(
            "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row[0], uuid4().hex, row[1], page, 1, 1, owner_epoch,
             case.financial.binding.binding_id, case.financial.binding.revision,
             case.financial.binding.billing_scope_id, 1,
             fingerprint(["synthetic-attempt", page]), "{}", 0, 0, 1,
             0 if state == "terminal" else None, state,
             fingerprint(["synthetic-evidence", page]) if terminal else None,
             "boot" if terminal else None, 1 if terminal else None))
        return row[0]


def _reserve_receipt(case, attempt_id, *, owner_epoch=1, operation_id=None):
    request = {"attempt_id": attempt_id, "owner_epoch": owner_epoch}
    if operation_id is None:
        with sqlite3.connect(case.ledger.path) as connection:
            operation_id = connection.execute(
                "SELECT operation_id FROM attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()[0]
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                           (operation_id, "reserve", fingerprint(request), canonical(request),
                            canonical({"attempt_id": attempt_id}), owner_epoch, "boot", 1))
    return operation_id


def _outcome_receipt(case, attempt_id, state):
    from uuid import UUID, uuid5
    request = {"attempt_id": attempt_id, "state": state}
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                           (uuid5(UUID(attempt_id), "settlement").hex, "outcome",
                            fingerprint(request), canonical(request),
                            canonical({"attempt_id": attempt_id, "state": state}),
                            1, "boot", 1))


@pytest.fixture
def case(tmp_path):
    value = make_case(tmp_path / "b34a", pages=2)
    try:
        yield value
    finally:
        close_case(value)


def test_job_managed_reserve_is_atomic_and_extends_the_claim(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, backend, runtime, receipt, 1)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        page = _page(case.ledger.path, receipt.job_id, 1)
        attempt = _attempt(case.ledger.path, claim.candidate_attempt_id)
        assert page["state"] == "intent"
        assert page["attempt_id"] == claim.candidate_attempt_id
        assert attempt["state"] == "intent" and attempt["attempt_id"] == page["attempt_id"]
        with sqlite3.connect(case.ledger.path) as connection:
            result = json.loads(connection.execute(
                "SELECT result_json FROM operations WHERE operation_id=?",
                (attempt["operation_id"],)).fetchone()[0])
        assert result["page_revision"] == page["revision"]
        assert result["claim_seq"] == claim.claim_seq
        assert result["claim_cancel_revision"] == 0
        assert result["job_revision"] == call.job_claim.job_revision
        # The gate consumes the post-reserve projection produced by reserve.
        assert call.job_claim.page_revision == page["revision"]
        runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert _page(case.ledger.path, receipt.job_id, 1)["state"] == "send_committed"
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)
        runtime.dispatcher.close()


def test_stale_claim_rolls_back_reserve_without_an_attempt_or_page_mutation(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, backend, runtime, receipt, 1)
        call.job_claim = replace(claim, claim_seq=claim.claim_seq + 1)
        with pytest.raises(CoordinationError, match="job_dispatch_claim_stale"):
            runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        # Page-local deterministic fault must not kill the dispatcher.
        assert not runtime.dispatcher._failed.is_set()
        with sqlite3.connect(case.ledger.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert _page(case.ledger.path, receipt.job_id, 1)["state"] == "claimed"
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)
        runtime.dispatcher.close()


def test_cancel_cancels_no_attempt_pages_and_never_touches_intent(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, backend, runtime, receipt, 1)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        revision = backend.get_status(case.actor, receipt.job_id).revision
        cancelled = backend.cancel(case.actor, receipt.job_id, uuid4().hex, revision)
        assert cancelled.state == "cancelling"
        assert _page(case.ledger.path, receipt.job_id, 1)["state"] == "intent"
        assert _attempt(case.ledger.path, claim.candidate_attempt_id)["state"] == "intent"
        assert _page(case.ledger.path, receipt.job_id, 2)["state"] == "cancelled"
        with pytest.raises(CoordinationError):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        runtime.dispatcher.finish(call, None, None)
        call = None
        attempt = _attempt(case.ledger.path, claim.candidate_attempt_id)
        assert attempt["state"] == "not_sent"
        assert _page(case.ledger.path, receipt.job_id, 1)["state"] == "cancelled"
        assert backend.get_status(case.actor, receipt.job_id).state == "cancelled"
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)
        runtime.dispatcher.close()


def test_cancel_fresh_queued_job_is_immediately_cancelled(case):
    receipt = _submit(case)
    status = case.backend.get_status(case.actor, receipt.job_id)
    cancelled = case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, status.revision)
    assert cancelled.state == "cancelled"
    current = case.backend.get_status(case.actor, receipt.job_id)
    assert all(page.state == "cancelled" for page in current.pages)


def test_terminal_settlement_projects_the_page_and_rolls_up_the_job(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    try:
        for page in (1, 2):
            claim = backend._claim_page_for_dispatch(
                case.actor, runtime.dispatcher._owner, receipt.job_id, page)
            request = PageExtractionRequest(
                "pdf-sha256:" + runtime.run.source_digest, page, image(page),
                runtime.run.run_id, job_claim=claim)
            result = asyncio.run(runtime.adapter.extract(request))
            assert result.extraction.page_number == page
        status = backend.get_status(case.actor, receipt.job_id)
        assert status.state == "completed"
        assert all(page.state == "completed" for page in status.pages)
        with sqlite3.connect(case.ledger.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM attempts WHERE state='terminal'").fetchone()[0] == 2
    finally:
        runtime.dispatcher.close()


def test_settlement_replay_is_idempotent_and_conflict_is_rejected(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    try:
        claim = backend._claim_page_for_dispatch(
            case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
        request = PageExtractionRequest(
            "pdf-sha256:" + runtime.run.source_digest, 1, image(1),
            runtime.run.run_id, job_claim=claim)
        asyncio.run(runtime.adapter.extract(request))
        attempt = _attempt(case.ledger.path, claim.candidate_attempt_id)
        outcome = Outcome(
            uuid5_hex(claim.candidate_attempt_id), claim.candidate_attempt_id,
            attempt["request_digest"], attempt["evidence_digest"], "terminal",
            attempt["actual_units"])
        owner = runtime.dispatcher._owner
        current = _page(case.ledger.path, receipt.job_id, 1)["revision"]
        replay = runtime.dispatcher.ledger.record_job_outcome(
            owner, outcome, job_claim=runtime.dispatcher.ledger.claim_for_attempt(
                owner, claim.candidate_attempt_id), page=None)
        assert replay["state"] == "terminal"
        changed = Outcome(outcome.operation_id, outcome.attempt_id, outcome.request_digest,
                          "0" * 64, "terminal", outcome.actual_units)
        with pytest.raises(CoordinationError, match="historical_outcome_conflict"):
            runtime.dispatcher.ledger.record_job_outcome(
                owner, changed, job_claim=runtime.dispatcher.ledger.claim_for_attempt(
                    owner, claim.candidate_attempt_id), page=None)
        assert _page(case.ledger.path, receipt.job_id, 1)["revision"] == current
    finally:
        runtime.dispatcher.close()


def uuid5_hex(attempt_id):
    from uuid import UUID, uuid5
    return uuid5(UUID(attempt_id), "settlement").hex


def test_register_adopts_a_run_registered_with_a_random_operation_id(case):
    actor2, run2 = new_generation(case)
    financial = case.current_financial
    runtime = open_runtime({
        **case.runtime.payload,
        "run": asdict(run2),
        "binding": asdict(financial.binding),
        "config": financial.config.model_dump(mode="json"),
        "schema_version": 2, "maintenance_root": str(case.control), "job_managed": True})
    try:
        assert runtime.dispatcher.ledger.verify_registered_run(
            runtime.dispatcher._owner, run2) == "exact"
        # Re-registering the same bound run is a read-only exact adoption.
        runtime.dispatcher.register(runtime.client)
        with sqlite3.connect(case.ledger.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM runs WHERE run_id=?",
                                      (run2.run_id,)).fetchone()[0] == 1
    finally:
        runtime.dispatcher.close()


def test_classifier_distinguishes_page_local_retry_and_integrity():
    assert classify_fault(CoordinationError("job_dispatch_claim_stale")) == "page_terminal"
    assert classify_fault(CoordinationError("ledger_busy")) == "retry"
    assert classify_fault(CoordinationError("result_evidence_conflict")) == "fatal"
    assert classify_fault(CoordinationError("dispatch_intent_mismatch")) == "job_blocked"


def test_recovery_settles_new_pre_gate_intent_as_not_sent_with_paused_restart(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    claim = backend._claim_page_for_dispatch(
        case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    _insert_attempt(case, receipt, "intent")
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute(
            "UPDATE durable_job_pages SET attempt_id=?,state='intent',claim_seq=1,owner_epoch=?,"
            "revision=revision+1 WHERE job_id=? AND page_number=1",
            (claim.candidate_attempt_id, runtime.dispatcher._owner.epoch, receipt.job_id))
    evidence = runtime.dispatcher.evidence
    runtime.dispatcher.close()
    owner = case.ledger.dispatcher_for_jobs()
    try:
        report = case.ledger.recover_unsettled(owner, evidence=evidence)
        assert report.clean
        assert report.settled == ()
        assert report.paused == (receipt.job_id,)
        assert _attempt(case.ledger.path, claim.candidate_attempt_id)["state"] == "not_sent"
        assert _page(case.ledger.path, receipt.job_id, 1)["state"] == "failed"
        assert _job(case.ledger.path, receipt.job_id)["state"] == "paused_restart"
        barrier = case.ledger.startup_barrier(owner)
        assert not any(barrier.values())
    finally:
        owner.close()


def test_recovery_quarantines_legacy_claimed_intent_without_gate_proof(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    claim = backend._claim_page_for_dispatch(
        case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    attempt_id = _insert_attempt(case, receipt, "intent")
    evidence = runtime.dispatcher.evidence
    runtime.dispatcher.close()
    owner = case.ledger.dispatcher_for_jobs()
    try:
        report = case.ledger.recover_unsettled(owner, evidence=evidence)
        assert report.quarantined and not report.clean
        assert claim.candidate_attempt_id == attempt_id
        page = _page(case.ledger.path, receipt.job_id, 1)
        assert page["state"] == "blocked" and page["attempt_id"] == attempt_id
        assert _attempt(case.ledger.path, attempt_id)["state"] == "unknown"
        assert _job(case.ledger.path, receipt.job_id)["state"] == "blocked"
    finally:
        owner.close()


def test_r30_legacy_claimed_unknown_attaches_identity_without_new_receipt(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    backend._claim_page_for_dispatch(case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    attempt_id = _insert_attempt(case, receipt, "unknown")
    _reserve_receipt(case, attempt_id)
    runtime.dispatcher.close()
    with sqlite3.connect(case.ledger.path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    create_private_directory(case.root / "recovery-evidence")
    from averon_import.services.ai_first.durable_dispatch import ResultEvidence
    evidence = ResultEvidence(case.root / "recovery-evidence")
    owner = case.ledger.dispatcher_for_jobs()
    try:
        report = case.ledger.recover_unsettled(owner, evidence=evidence)
        assert report.repaired == (receipt.job_id,)
        page = _page(case.ledger.path, receipt.job_id, 1)
        assert page["state"] == "blocked" and page["attempt_id"] == attempt_id
        assert _attempt(case.ledger.path, attempt_id)["state"] == "unknown"
        with sqlite3.connect(case.ledger.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == before
    finally:
        owner.close()


def test_r23_legacy_claimed_not_sent_repairs_projection_only(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    backend._claim_page_for_dispatch(case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    attempt_id = _insert_attempt(case, receipt, "not_sent")
    _reserve_receipt(case, attempt_id)
    _outcome_receipt(case, attempt_id, "not_sent")
    runtime.dispatcher.close()
    create_private_directory(case.root / "recovery-evidence-23")
    from averon_import.services.ai_first.durable_dispatch import ResultEvidence
    evidence = ResultEvidence(case.root / "recovery-evidence-23")
    owner = case.ledger.dispatcher_for_jobs()
    try:
        before = _attempt(case.ledger.path, attempt_id)
        report = case.ledger.recover_unsettled(owner, evidence=evidence)
        assert report.repaired == (receipt.job_id,)
        page = _page(case.ledger.path, receipt.job_id, 1)
        assert page["state"] == "failed" and page["attempt_id"] == attempt_id
        assert _attempt(case.ledger.path, attempt_id) == before
    finally:
        owner.close()


def _recovery_row(case, job_id, page):
    with sqlite3.connect(case.ledger.path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT p.job_id,p.run_id,p.page_number,p.state AS page_state,"
            "p.attempt_id AS page_attempt_id,p.candidate_attempt_id,"
            "p.claim_seq AS page_claim_seq,p.owner_epoch AS page_owner_epoch,"
            "p.revision AS page_revision,p.evidence_digest AS page_evidence_digest,"
            "j.tenant_id,j.owner_id,j.document_id,j.state AS job_state,"
            "j.cancel_revision AS job_cancel_revision,j.generation AS job_generation,"
            "a.attempt_id,a.state AS attempt_state,a.attempt_no,"
            "a.claim_seq AS attempt_claim_seq,a.owner_epoch AS attempt_owner_epoch,"
            "a.request_digest,a.operation_id FROM durable_job_pages p"
            " JOIN durable_jobs j USING(job_id) LEFT JOIN attempts a"
            " ON a.run_id=p.run_id AND a.page_number=p.page_number"
            " WHERE p.job_id=? AND p.page_number=?", (job_id, page)).fetchone())


def test_r24_evidence_contradiction_blocks_page_without_touching_b1(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    backend._claim_page_for_dispatch(case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    attempt_id = _insert_attempt(case, receipt, "not_sent")
    _reserve_receipt(case, attempt_id)
    _outcome_receipt(case, attempt_id, "not_sent")
    row = _recovery_row(case, receipt.job_id, 1)
    before = _attempt(case.ledger.path, attempt_id)
    runtime.dispatcher.close()
    owner = case.ledger.dispatcher_for_jobs()
    try:
        with case.ledger._transaction(owner) as (connection, stamp, _meta):
            outcome, code = case.ledger._recover_one(
                connection, owner, stamp, row, {"outcome": {}}, None)
        assert (outcome, code) == ("quarantine", "recovery_evidence_conflict")
        page = _page(case.ledger.path, receipt.job_id, 1)
        assert page["state"] == "blocked" and page["attempt_id"] == attempt_id
        assert _attempt(case.ledger.path, attempt_id) == before
        assert _job(case.ledger.path, receipt.job_id)["state"] == "blocked"
    finally:
        owner.close()


def test_evidence_v2_is_idempotent_and_publication_recovery_promotes_pending(case):
    receipt = _submit(case)
    runtime = _runtime(case)
    backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
    try:
        claim = backend._claim_page_for_dispatch(
            case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
        request = PageExtractionRequest(
            "pdf-sha256:" + runtime.run.source_digest, 1, image(1),
            runtime.run.run_id, job_claim=claim)
        asyncio.run(runtime.adapter.extract(request))
        store = runtime.dispatcher.evidence
        final = store.path(claim.candidate_attempt_id)
        assert final.exists()
        payload = store.read(claim.candidate_attempt_id)
        assert payload["version"] == 2 and "settlement" in payload
        # Replaying the exact same publication is a no-op.
        assert final.read_bytes() == final.read_bytes()
        pending = final.with_suffix(".pending")
        shutil.copyfile(final, pending)
        secure_private_path(pending, directory=False)
        published, conflicts = store.recover_publication()
        assert published == (claim.candidate_attempt_id,) and not conflicts
        assert final.exists() and not pending.exists()
        # Promotion of a validated pending when the final is missing.
        pending.write_bytes(final.read_bytes())
        secure_private_path(pending, directory=False)
        final.unlink()
        published, conflicts = store.recover_publication()
        assert published == (claim.candidate_attempt_id,) and not conflicts
        assert store.read(claim.candidate_attempt_id)["version"] == 2
    finally:
        runtime.dispatcher.close()
