"""B3.3 atomic cancel/send gate tests using real schema-v2 SQLite."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import sqlite3
import threading
from dataclasses import replace
from uuid import uuid4
import asyncio

import pytest

from averon_import.services.ai_first.coordination_contracts import (
    CommitReceiptUncertain, CoordinationError, DeferredWithoutIntent, DispatchGateRejected,
)
from averon_import.services.ai_first.durable_dispatch import CALLER
from averon_import.services.ai_first.durable_job_store import DurableJobStore
from averon_import.services.ai_first.orchestration import PageExtractionRequest
from averon_import.services.ai_first import dispatch_policy
from averon_import.services.ocr.qwen_vision_client import _HttpsTransport
from b32_test_support import close_case, make_case
from test_ai_first_durable_dispatch import image, open_runtime
from test_ai_first_qwen_vision_adapter import FakeTransport


@pytest.fixture(autouse=True)
def offline_mode(monkeypatch):
    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setattr(dispatch_policy, "_required", False)

    def deny_network(*_args, **_kwargs):
        raise AssertionError("b33_network_forbidden")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local_only(original):
        def guarded(sock, address):
            if not isinstance(address, tuple) or not address:
                return original(sock, address)
            try:
                loopback = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                return deny_network(sock, address)
            return original(sock, address)
        return guarded

    monkeypatch.setattr(socket.socket, "connect", local_only(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", local_only(original_connect_ex))
    monkeypatch.setattr(_HttpsTransport, "post", deny_network)


@pytest.fixture
def job_case(tmp_path):
    case = make_case(tmp_path / "b33", pages=1)
    try:
        receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                      execution_profile=case.profile)
        transport = FakeTransport()
        runtime_payload = {**case.runtime.payload, "schema_version": 2,
                           "maintenance_root": str(case.control)}
        runtime = open_runtime(runtime_payload, transport)
        case.backend = DurableJobStore(runtime.dispatcher.ledger, case.execution_store)
        yield case, receipt, runtime, transport
        runtime.dispatcher.close()
    finally:
        close_case(case)


def _prepared(case, receipt, runtime):
    claim = case.backend._claim_page_for_dispatch(
        case.actor, runtime.dispatcher._owner, receipt.job_id, 1)
    page = image(1)
    body = runtime.client._request_body(page.data, 1, runtime.adapter._user_context)
    call = runtime.dispatcher.call(runtime.client, 1, hashlib.sha256(body).hexdigest(),
                                   runtime.adapter.estimate(page), job_claim=claim)
    headers = {"Authorization": "Api-Key synthetic-b2-credential",
               "OpenAI-Project": "synthetic-project"}
    url = runtime.client.config.base_url + "/chat/completions"
    return claim, call, body, headers, url


def _page(case, job_id):
    with sqlite3.connect(case.ledger.path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(
            "SELECT state,attempt_id,claim_seq,owner_epoch,revision FROM durable_job_pages "
            "WHERE job_id=? AND page_number=1", (job_id,)).fetchone())


def test_cancel_after_reserve_wins_gate_and_records_proven_not_sent(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        with pytest.raises(CoordinationError):
            runtime.dispatcher.post(
                runtime.client, call, url=url, body=body, headers=headers,
                on_dispatch=lambda: None,
                before_dispatch=lambda: case.backend.cancel(
                    case.actor, receipt.job_id, uuid4().hex,
                    case.backend.get_status(case.actor, receipt.job_id).revision),
            )
        assert transport.calls == []
        assert call.fresh and not call.delegated and not call.uncertain
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "not_sent"
    assert _page(case, receipt.job_id)["state"] == "cancelled"


def test_gate_then_cancel_retains_send_commit_and_liability(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        permit = call.permit
        runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
        assert call.gate_committed and call.delegated
        with pytest.raises(CoordinationError, match="dispatch_permit_consumed"):
            runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
        status = case.backend.get_status(case.actor, receipt.job_id)
        case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, status.revision)
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    page = _page(case, receipt.job_id)
    assert page["state"] == "send_committed"
    assert page["attempt_id"] == call.attempt.attempt_id
    attempt = runtime.dispatcher.ledger.snapshot()["attempts"][0]
    assert attempt["state"] == "unknown"


def test_cancel_and_gate_race_has_one_sqlite_winner(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    cancel_errors = []
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        revision = case.backend.get_status(case.actor, receipt.job_id).revision
        barrier = threading.Barrier(2)

        def cancel_job():
            try:
                barrier.wait(timeout=5)
                case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, revision)
            except BaseException as exc:
                cancel_errors.append(exc)

        worker = threading.Thread(target=cancel_job)
        worker.start()
        barrier.wait(timeout=5)
        gate_error = None
        try:
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        except CoordinationError as exc:
            gate_error = exc
        worker.join(timeout=10)
        assert not worker.is_alive() and not cancel_errors
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    page = _page(case, receipt.job_id)
    attempt = runtime.dispatcher.ledger.snapshot()["attempts"][0]
    if call.gate_committed:
        assert gate_error is None
        assert page["state"] == "send_committed"
        assert attempt["state"] == "unknown"
    else:
        assert isinstance(gate_error, CoordinationError)
        assert page["state"] == "cancelled"
        assert attempt["state"] == "not_sent"


def test_reserve_commit_receipt_loss_keeps_liability_and_never_posts(job_case, monkeypatch):
    case, receipt, runtime, transport = job_case
    from averon_import.services.ai_first import coordination as coordination_module

    original_connect = coordination_module.sqlite3.connect

    class LostReceipt(sqlite3.Connection):
        def commit(self):
            super().commit()
            if self.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                raise sqlite3.OperationalError("synthetic lost reserve receipt")

    def connect(*args, **kwargs):
        kwargs["factory"] = LostReceipt
        return original_connect(*args, **kwargs)

    token = CALLER.set(runtime.adapter._caller)
    call = None
    with monkeypatch.context() as patch:
        patch.setattr(coordination_module.sqlite3, "connect", connect)
        try:
            _claim, call, body, headers, url = _prepared(case, receipt, runtime)
            with pytest.raises(CoordinationError):
                runtime.dispatcher.post(runtime.client, call, url=url, body=body, headers=headers,
                                        on_dispatch=lambda: None)
            assert transport.calls == []
            assert call.uncertain
        finally:
            if call is not None:
                runtime.dispatcher.finish(call, None, None)
            CALLER.reset(token)

    assert len(runtime.dispatcher.ledger.snapshot()["attempts"]) == 1
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"
    assert _page(case, receipt.job_id)["state"] == "claimed"


def test_gate_commit_receipt_loss_fails_closed_after_page_commit(job_case, monkeypatch):
    case, receipt, runtime, transport = job_case
    from averon_import.services.ai_first import coordination as coordination_module

    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        original_connect = coordination_module.sqlite3.connect

        class LostReceipt(sqlite3.Connection):
            def commit(self):
                super().commit()
                committed = self.execute(
                    "SELECT COUNT(*) FROM durable_job_pages WHERE job_id=? AND page_number=1 "
                    "AND state='send_committed'", (receipt.job_id,)).fetchone()[0]
                if committed:
                    raise sqlite3.OperationalError("synthetic lost gate receipt")

        def connect(*args, **kwargs):
            kwargs["factory"] = LostReceipt
            return original_connect(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(coordination_module.sqlite3, "connect", connect)
            with pytest.raises(CommitReceiptUncertain):
                runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert call.uncertain and not call.gate_committed
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert _page(case, receipt.job_id)["state"] == "send_committed"
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"


def test_gate_post_commit_clock_error_is_uncertain_and_liability_is_retained(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    ledger = runtime.dispatcher.ledger
    original_clock = ledger.clock
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        calls = 0

        def clock_fails_after_commit():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise CoordinationError("synthetic_post_commit_clock_failure")
            return original_clock()

        ledger.clock = clock_fails_after_commit
        with pytest.raises(CommitReceiptUncertain):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert call.uncertain
        assert transport.calls == []
    finally:
        ledger.clock = original_clock
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert _page(case, receipt.job_id)["state"] == "send_committed"
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"


def test_gate_post_commit_select_error_is_uncertain_and_liability_is_retained(job_case, monkeypatch):
    case, receipt, runtime, transport = job_case
    from averon_import.services.ai_first import coordination as coordination_module

    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        original_connect = coordination_module.sqlite3.connect

        class FailingPostCommitSelect(sqlite3.Connection):
            _did_commit = False

            def commit(self):
                super().commit()
                self._did_commit = True

            def execute(self, sql, parameters=()):
                statement = " ".join(sql.split()).casefold()
                if self._did_commit and statement == "select lease_until_ms from meta where id=1":
                    raise sqlite3.OperationalError("synthetic post-commit I/O failure")
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            kwargs["factory"] = FailingPostCommitSelect
            return original_connect(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(coordination_module.sqlite3, "connect", connect)
            with pytest.raises(CommitReceiptUncertain):
                runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert call.uncertain
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert _page(case, receipt.job_id)["state"] == "send_committed"
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"


def test_gate_rollback_failure_is_uncertain_not_a_deterministic_rejection(job_case, monkeypatch):
    case, receipt, runtime, transport = job_case
    from averon_import.services.ai_first import coordination as coordination_module

    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        call.job_claim = replace(claim, cancel_revision=claim.cancel_revision + 1)
        original_connect = coordination_module.sqlite3.connect

        class FailingRollback(sqlite3.Connection):
            def rollback(self):
                raise sqlite3.OperationalError("synthetic rollback I/O failure")

        def connect(*args, **kwargs):
            kwargs["factory"] = FailingRollback
            return original_connect(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(coordination_module.sqlite3, "connect", connect)
            with pytest.raises(CommitReceiptUncertain):
                runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert call.uncertain
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"


@pytest.mark.parametrize("field", ["cancel_revision", "page_revision", "claim_seq", "owner_epoch"])
def test_stale_claim_fences_are_rejected_before_http(job_case, field):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        call.job_claim = replace(claim, **{field: getattr(claim, field) + 1})
        with pytest.raises(CoordinationError):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert not call.uncertain and not call.delegated
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)


@pytest.mark.parametrize("field", [
    "tenant_id", "owner_id", "document_id", "job_id", "run_id", "generation",
    "page_number", "candidate_attempt_id",
])
def test_claim_identity_mismatch_is_rejected_before_http(job_case, field):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        wrong = {
            "tenant_id": "other-tenant", "owner_id": "other-owner",
            "document_id": "other-document", "job_id": "dj_" + "f" * 32,
            "run_id": uuid4().hex, "generation": claim.generation + 1,
            "page_number": claim.page_number + 1,
            "candidate_attempt_id": uuid4().hex,
        }[field]
        call.job_claim = replace(claim, **{field: wrong})
        with pytest.raises(CoordinationError):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert not call.uncertain and not call.delegated
        assert transport.calls == []
    finally:
        if call is not None:
            try:
                runtime.dispatcher.finish(call, None, None)
            except CoordinationError:
                # A forged candidate identity cannot be attached to the real
                # B1 outcome; leave that immutable intent for reconciliation.
                if field != "candidate_attempt_id":
                    raise
        CALLER.reset(token)


def test_inactive_run_and_generation_are_rejected(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        inactive = replace(runtime.run, run_id=uuid4().hex, generation=runtime.run.generation + 1)
        runtime.dispatcher.ledger.register_run(
            runtime.dispatcher._owner, uuid4().hex, inactive)
        with pytest.raises(DispatchGateRejected):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)


@pytest.mark.parametrize("mismatch", ["binding", "billing_policy", "quota_policy"])
def test_binding_and_policy_revision_mismatch_fail_closed(job_case, mismatch):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        owner = runtime.dispatcher._owner
        binding = runtime.adapter.dispatch_binding.binding
        if mismatch == "binding":
            revised = replace(binding, revision=2, credential_generation="b33-rotated",
                              credential_tag=hashlib.sha256(b"synthetic b33 rotation").hexdigest())
            runtime.dispatcher.ledger.put_binding(owner, uuid4().hex, revised)
        elif mismatch == "billing_policy":
            ceiling = runtime.dispatcher.ledger.snapshot()["billing"][binding.billing_scope_id]["ceiling_units"]
            runtime.dispatcher.ledger.revise_billing(
                owner, uuid4().hex, binding.billing_scope_id, 1, ceiling + 1_000)
        else:
            with runtime.dispatcher.ledger._connection() as connection:
                quota = connection.execute(
                    "SELECT revision,requests,tokens,concurrency,window_ms FROM quota_scopes WHERE scope_id=?",
                    (binding.quota_scope_ids[0],)).fetchone()
            runtime.dispatcher.ledger.revise_quota(
                owner, uuid4().hex, binding.quota_scope_ids[0], quota[0],
                requests=quota[1] + 1, tokens=quota[2] + 1,
                concurrency=quota[3], window_ms=quota[4])
        with pytest.raises(DispatchGateRejected):
            runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)


def test_cancel_after_gate_allows_one_fake_http_post_and_drains(job_case):
    case, receipt, runtime, transport = job_case
    claim = case.backend._claim_page_for_dispatch(
        case.actor, runtime.dispatcher._owner, receipt.job_id, 1)

    def cancel_when_fake_http_starts(page_number, _count, _kwargs):
        status = case.backend.get_status(case.actor, receipt.job_id)
        case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, status.revision)
        from test_ai_first_qwen_vision_adapter import _response
        return _response(page_number)

    transport.handler = cancel_when_fake_http_starts
    request = PageExtractionRequest(
        "pdf-sha256:" + runtime.run.source_digest, 1, image(1), runtime.run.run_id,
        job_claim=claim,
    )
    result = asyncio.run(runtime.adapter.extract(request))
    assert result.extraction.page_number == 1
    assert len(transport.calls) == 1
    assert case.backend.get_status(case.actor, receipt.job_id).state == "cancelling"
    assert _page(case, receipt.job_id)["state"] == "send_committed"
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "terminal"


def _reserve_other_run_attempt(runtime, call, url, body, headers):
    ledger = runtime.dispatcher.ledger
    owner = runtime.dispatcher._owner
    other_run = replace(runtime.run, run_id=uuid4().hex, document_id=uuid4().hex)
    ledger.register_run(owner, uuid4().hex, other_run)
    template = runtime.dispatcher._request(runtime.client, call, url, body, headers)
    occupant = replace(template, operation_id=uuid4().hex, attempt_id=uuid4().hex,
                       run_id=other_run.run_id)
    ledger.reserve(owner, occupant)
    return occupant


@pytest.mark.parametrize("limit", ["global_capacity", "quota"])
def test_deferred_without_intent_is_retryable_and_later_reserve_posts_once(job_case, limit):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        occupant = _reserve_other_run_attempt(runtime, call, url, body, headers)
        with sqlite3.connect(case.ledger.path) as connection:
            if limit == "global_capacity":
                connection.execute("UPDATE meta SET global_concurrency=1 WHERE id=1")
            else:
                connection.execute("UPDATE quota_scopes SET requests=1 WHERE scope_id=?",
                                   (call.grant.binding.quota_scope_ids[0],))
        attempts_before = len(runtime.dispatcher.ledger.snapshot()["attempts"])
        with sqlite3.connect(case.ledger.path) as connection:
            holds_before = connection.execute(
                "SELECT COUNT(*) FROM attempt_quota_reservations").fetchone()[0]
        with pytest.raises(DeferredWithoutIntent):
            runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        assert call.deferred is not None
        assert not runtime.dispatcher._failed.is_set()
        assert len(runtime.dispatcher.ledger.snapshot()["attempts"]) == attempts_before == 1
        with sqlite3.connect(case.ledger.path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM attempt_quota_reservations").fetchone()[0] == holds_before == 1
        assert transport.calls == []

        with sqlite3.connect(case.ledger.path) as connection:
            if limit == "global_capacity":
                connection.execute("UPDATE meta SET global_concurrency=10 WHERE id=1")
            else:
                connection.execute("UPDATE quota_scopes SET requests=100 WHERE scope_id=?",
                                   (call.grant.binding.quota_scope_ids[0],))
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        assert permit is call.permit and call.fresh
        assert not runtime.dispatcher._failed.is_set()
        assert len(runtime.dispatcher.ledger.snapshot()["attempts"]) == 2
        assert sum(row["run_id"] == runtime.run.run_id
                   for row in runtime.dispatcher.ledger.snapshot()["attempts"]) == 1
        runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
        call.response = transport.post(url, body=body, headers=headers)
        assert len(transport.calls) == 1
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)


def test_job_managed_run_cannot_reserve_or_send_via_legacy_b2_path(job_case):
    case, receipt, runtime, transport = job_case
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        _claim, call, body, headers, url = _prepared(case, receipt, runtime)
        call.job_claim = None
        with pytest.raises(CoordinationError, match="job_dispatch_claim_required"):
            runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        assert call.permit is None
        assert transport.calls == []
        assert runtime.dispatcher.ledger.snapshot()["attempts"] == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)
