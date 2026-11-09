"""B3.3 atomic cancel/send gate tests using real schema-v2 SQLite."""
from __future__ import annotations

import hashlib
import socket
import sqlite3
import threading
from uuid import uuid4

import pytest

from averon_import.services.ai_first.coordination_contracts import CoordinationError
from averon_import.services.ai_first.durable_dispatch import CALLER
from averon_import.services.ai_first.durable_job_store import DurableJobStore
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

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
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
            with pytest.raises(CoordinationError, match="job_send_gate_receipt_uncertain"):
                runtime.dispatcher.consume(call.permit, runtime.client, call, url, body, headers, None)
        assert call.uncertain and not call.gate_committed
        assert transport.calls == []
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)

    assert _page(case, receipt.job_id)["state"] == "send_committed"
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "intent"
