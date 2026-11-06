"""P1.1 SQLite/checkpoint ordering; real commit boundaries and no paid provider."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from averon_import.services.ai_first.coordination_contracts import CoordinationError, rub_units
from averon_import.services.ai_first.orchestration import (
    CheckpointStore, OrchestrationError, PageAttempt, PageRunRecord, _utc_now, inspect_pdf,
)
from test_ai_first_durable_dispatch import (
    offline_durable_mode, runtime, open_runtime, receive, request, run_orchestrator, setup_runtime, worker,
)


def raw_checkpoint(payload):
    return Path(payload["pdf"]).parent / "checkpoint" / "page-000001.json"


def attempt_rows(payload):
    with sqlite3.connect(Path(payload["ledger"]) / "coordination.sqlite3") as connection:
        return connection.execute("SELECT attempt_id,state FROM attempts").fetchall()


def test_p11_crash_before_sqlite_commit_has_no_checkpoint_intent(runtime):
    payload = runtime.payload
    runtime.dispatcher.close()
    with worker(payload, "before_intent_commit") as process:
        assert receive(process)["window"] == "before_intent_commit"
        record = json.loads(raw_checkpoint(payload).read_bytes())
        assert record["state"] == "running"
        assert record["attempts"][-1]["dispatch_intent_at"] is None
        assert record["attempts"][-1]["request_started"] is False
        assert attempt_rows(payload) == []  # real uncommitted writer is invisible
        process.kill()
        process.wait(timeout=10)
    replacement = open_runtime(payload)
    try:
        assert replacement.dispatcher.ledger.snapshot()["attempts"] == []
        report = run_orchestrator(replacement)
        page = report.pages[0]
        assert page.state == "completed" and len(replacement.transport.calls) == 1
        assert [item.state for item in page.attempts] == ["failed", "completed"]
        assert page.attempts[0].safe_error_code == "durable_pre_intent_interrupted"
        assert page.attempts[0].dispatch_intent_at is None
        assert report.reserved_unknown_cost_rub == 0
        assert len(attempt_rows(payload)) == 1
        assert run_orchestrator(replacement).pages[0].attempts == page.attempts
        assert len(replacement.transport.calls) == 1
    finally:
        replacement.dispatcher.close()


@pytest.mark.parametrize("missing_checkpoint", [False, True])
def test_p11_crash_after_sqlite_commit_before_checkpoint_never_retries(runtime, monkeypatch, missing_checkpoint):
    payload = runtime.payload
    runtime.dispatcher.close()
    with worker(payload, "after_intent_commit") as process:
        signal = receive(process)
        assert signal["window"] == "after_intent_commit" and signal["posts"] == 0
        record = json.loads(raw_checkpoint(payload).read_bytes())
        assert record["attempts"][-1]["dispatch_intent_at"] is None
        assert record["attempts"][-1]["request_started"] is False
        assert len(attempt_rows(payload)) == 1 and attempt_rows(payload)[0][1] == "intent"
        process.kill()
        process.wait(timeout=10)
    if missing_checkpoint:
        raw_checkpoint(payload).unlink()
    replacement = open_runtime(payload)
    try:
        def no_reauthorization(*args, **kwargs):
            pytest.fail("recovered committed intent must not reach prepare()")
        monkeypatch.setattr(replacement.dispatcher, "prepare", no_reauthorization)
        report = run_orchestrator(replacement)
        assert report.pages[0].state == "unknown_outcome"
        assert len(report.pages[0].attempts) == 1
        assert report.pages[0].attempts[0].safe_error_code == "durable_intent_recovered"
        assert replacement.transport.calls == []
        snapshot = replacement.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["state"] == "unknown"
        assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
        assert snapshot["billing"]["billing"]["blocked"] is True
        assert replacement.dispatcher._calls == 0
    finally:
        replacement.dispatcher.close()


def test_p11_preparation_history_survives_later_evidence_crash(runtime):
    payload = runtime.payload
    runtime.dispatcher.close()
    for window in ("before_intent_commit", "after_evidence"):
        with worker(payload, window) as process:
            assert receive(process)["window"] == window
            process.kill()
            process.wait(timeout=10)
    replacement = open_runtime(payload)
    try:
        report = run_orchestrator(replacement)
        assert report.pages[0].state == "completed"
        assert [item.state for item in report.pages[0].attempts] == ["failed", "completed"]
        assert report.pages[0].attempts[0].safe_error_code == "durable_pre_intent_interrupted"
        assert replacement.transport.calls == []
        snapshot = replacement.dispatcher.ledger.snapshot()
        assert len(snapshot["attempts"]) == 1
        assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
        assert snapshot["billing"]["billing"]["blocked"] is True
    finally:
        replacement.dispatcher.close()


def write_running(runtime, *, marker=False, state="running"):
    identity = inspect_pdf(runtime.pdf, runtime.adapter, runtime.config, run_id=runtime.run.run_id)
    checkpoint = CheckpointStore(runtime.pdf.parent / "checkpoint", identity)
    checkpoint.open()
    now = _utc_now()
    attempt = PageAttempt(state=state, started_at=now,
        dispatch_intent_at=now if marker else None, finished_at=None if state == "running" else now,
        safe_error_code=None if state == "running" else "historical_ambiguity")
    asyncio.run(checkpoint.write(PageRunRecord(page_number=1, state=state, attempts=[attempt], updated_at=now)))
    checkpoint.close()
    return checkpoint


@pytest.mark.parametrize("marker,state", [(True, "running"), (False, "unknown_outcome")])
def test_p11_missing_history_does_not_clear_historical_ambiguity(runtime, marker, state):
    write_running(runtime, marker=marker, state=state)
    before = raw_checkpoint(runtime.payload).read_bytes()
    with pytest.raises(CoordinationError, match="checkpoint_ledger_history_mismatch"):
        run_orchestrator(runtime)
    assert raw_checkpoint(runtime.payload).read_bytes() == before
    assert runtime.transport.calls == [] and attempt_rows(runtime.payload) == []


def test_p11_checkpoint_read_waits_for_authority_before_classification(runtime, monkeypatch):
    checkpoint = write_running(runtime)
    before = raw_checkpoint(runtime.payload).read_bytes()
    records = checkpoint.open(recover_running=False)
    assert records[1].state == "running" and raw_checkpoint(runtime.payload).read_bytes() == before
    checkpoint.close()
    def unavailable(*args):
        raise CoordinationError("ledger_busy")
    monkeypatch.setattr(runtime.dispatcher.ledger, "checkpoint_attempts", unavailable)
    with pytest.raises(CoordinationError, match="ledger_busy"):
        run_orchestrator(runtime)
    assert raw_checkpoint(runtime.payload).read_bytes() == before
    assert runtime.transport.calls == []


def test_p11_checkpoint_failure_after_intent_is_proven_not_sent(runtime, monkeypatch):
    original = CheckpointStore.write
    seen = []
    async def fail_marker(store, record):
        await original(store, record)
        if record.state == "running" and record.attempts[-1].dispatch_intent_at is not None:
            assert attempt_rows(runtime.payload)[0][1] == "intent"
            seen.append(True)
            raise OrchestrationError("synthetic_checkpoint_failure")
    monkeypatch.setattr(CheckpointStore, "write", fail_marker)
    report = run_orchestrator(runtime)
    assert seen == [True] and report.pages[0].state == "failed"
    assert runtime.transport.calls == []
    billing = runtime.dispatcher.ledger.snapshot()["billing"]["billing"]
    assert billing["held_units"] == billing["spent_units"] == 0
    assert attempt_rows(runtime.payload)[0][1] == "not_sent"


def test_p11_cancel_during_checkpoint_callback_drains_not_sent(runtime, monkeypatch):
    original = CheckpointStore.write
    cancelled = asyncio.Event()
    async def cancel_after_marker(store, record):
        await original(store, record)
        if record.state == "running" and record.attempts[-1].dispatch_intent_at is not None:
            assert attempt_rows(runtime.payload)[0][1] == "intent"
            cancelled.set()
    monkeypatch.setattr(CheckpointStore, "write", cancel_after_marker)
    report = run_orchestrator(runtime, cancel_event=cancelled)
    assert report.pages[0].state == "failed" and runtime.transport.calls == []
    assert attempt_rows(runtime.payload)[0][1] == "not_sent"
    assert runtime.dispatcher._calls == 0


def test_p11_checkpoint_callback_wait_is_outside_http_deadline(runtime):
    runtime.client.config = replace(runtime.client.config,
        limits=replace(runtime.client.config.limits, request_timeout_s=0.05))
    item = request(runtime)
    async def slow_checkpoint():
        assert attempt_rows(runtime.payload)[0][1] == "intent"
        await asyncio.sleep(0.15)
    item.before_dispatch = slow_checkpoint
    result = asyncio.run(runtime.adapter.extract(item))
    assert result.extraction.page_number == 1 and len(runtime.transport.calls) == 1
    assert attempt_rows(runtime.payload)[0][1] == "terminal"


def test_p11_checkpoint_history_read_is_owner_and_generation_fenced(runtime):
    ledger, owner = runtime.dispatcher.ledger, runtime.dispatcher._owner
    newer = replace(runtime.run, run_id=uuid4().hex, generation=2)
    ledger.register_run(owner, uuid4().hex, newer)
    with pytest.raises(CoordinationError, match="run_fenced"):
        ledger.checkpoint_attempts(owner, runtime.run)
    owner.close()
    with pytest.raises(CoordinationError, match="owner_fenced"):
        ledger.checkpoint_attempts(owner, newer)


def test_p11_known_budget_denial_has_no_phantom_intent_or_unknown(tmp_path):
    value = setup_runtime(tmp_path, account_budget="0.1")
    try:
        report = run_orchestrator(value)
        assert report.pages[0].state == "failed"
        attempt = report.pages[0].attempts[-1]
        assert attempt.dispatch_intent_at is None and attempt.request_started is False
        assert report.reserved_unknown_cost_rub == 0
        assert value.transport.calls == [] and attempt_rows(value.payload) == []
        assert value.dispatcher._failed.is_set()  # no fallback or silent retry
    finally:
        value.dispatcher.close()


def test_p11_failed_commit_before_persistence_can_be_proved_absent(runtime, monkeypatch):
    from averon_import.services.ai_first import coordination as core
    connect = core.sqlite3.connect
    class BeforeCommitFailure(sqlite3.Connection):
        def commit(self):
            if self.in_transaction and self.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                raise sqlite3.OperationalError("synthetic pre-commit failure")
            return super().commit()
    def failed_connect(*args, **kwargs):
        kwargs["factory"] = BeforeCommitFailure
        return connect(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(core.sqlite3, "connect", failed_connect)
        report = run_orchestrator(runtime)
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[-1].dispatch_intent_at is None
    assert report.pages[0].attempts[-1].request_started is False
    assert report.reserved_unknown_cost_rub == 0
    assert runtime.transport.calls == [] and attempt_rows(runtime.payload) == []
