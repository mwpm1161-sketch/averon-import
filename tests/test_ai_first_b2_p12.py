"""P1.2 history-only settlement tests; every provider response is fake."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4, uuid5, UUID

import pytest

from averon_import.services.ai_first import coordination as coordination_module, dispatch_policy
from averon_import.services.ai_first.coordination_contracts import (
    Attempt, Binding, CoordinationError, Outcome, Run, fingerprint, rub_units,
)
from averon_import.services.ai_first.durable_dispatch import (
    DurableDispatcher, HistoricalSettlementContext, credential_tag, price_units,
    target_digest, usage_units,
)
from averon_import.services.ai_first.durable_qwen_adapter import configuration_digest
from averon_import.services.ai_first.orchestration import OrchestrationConfig, TokenPricing
from averon_import.services.ocr.qwen_vision_client import QwenVisionConfig
from test_ai_first_durable_dispatch import (
    FakeTransport, receive, request, runtime, setup_runtime, worker,
)


@pytest.fixture(autouse=True)
def p12_offline(monkeypatch):
    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setattr(dispatch_policy, "_required", False)
    from averon_import.services.ocr.qwen_vision_client import _HttpsTransport
    def denied(*_args, **_kwargs):
        raise AssertionError("real provider/network forbidden")
    monkeypatch.setattr(_HttpsTransport, "post", denied)


def _crash_after_evidence(value, *, no_usage=False):
    value.dispatcher.close()
    value.client = None
    value.adapter = None
    mode = "after_evidence_no_usage" if no_usage else "after_evidence"
    with worker(value.payload, mode) as process:
        signal = receive(process)
        assert signal["window"] == mode and signal["posts"] == 1
        process.kill()
        process.wait(timeout=10)
    attempt_id = DurableDispatcher.attempt_id(value.run.run_id, 1)
    evidence_path = Path(value.payload["evidence"]) / f"{attempt_id}.json"
    assert evidence_path.is_file()
    return attempt_id, evidence_path


def _new_owner(value):
    options = {"lease_ms": value.payload["lease_ms"]} if "lease_ms" in value.payload else {}
    owner = DurableDispatcher(
        value.payload["ledger"], installation_id=value.payload["installation_id"],
        evidence_root=value.payload["evidence"], enabled=True, **options,
    )
    value.dispatcher = owner
    assert owner._clients == {}
    return owner


def _rotate_and_revise(value, owner):
    ledger = owner.ledger
    wire = QwenVisionConfig(
        "https://provider.invalid/v1", "gpt://synthetic-project/qwen-test",
        "replacement-synthetic-credential", "synthetic-project",
    )
    binding = Binding(
        "binding", 2, "generation_2", credential_tag(wire, "generation_2"),
        "billing", ("quota",), target_digest(wire),
    )
    ledger.put_binding(owner._owner, uuid4().hex, binding)
    config = value.config.model_copy(update={
        "pricing": TokenPricing(profile_id="synthetic-b2", input_rub_per_1k=Decimal("7"),
                                output_rub_per_1k=Decimal("3")),
        "requests_per_minute": 1,
        "tokens_per_minute": 50,
        "max_budget_rub": Decimal("10"),
    })
    run = Run(
        uuid4().hex, value.run.tenant_id, value.run.document_id, value.run.generation + 1,
        value.run.source_digest, configuration_digest(config, binding),
        rub_units(config.max_budget_rub), min(config.max_requests, 100),
    )
    ledger.revise_billing(owner._owner, uuid4().hex, "billing", 1, rub_units("0.1"))
    ledger.revise_quota(owner._owner, uuid4().hex, "quota", 1,
                        requests=1, tokens=50, concurrency=1, window_ms=60000)
    ledger.register_run(owner._owner, uuid4().hex, run)
    return binding, config, run


def _snapshot_files(root):
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "current-checkpoint/page-000001.json": b'{"generation":2,"state":"completed","text":"NEW"}',
        "human_review.json": b'{"decision":"human confirmed","raw":"NEW"}',
        "ocr_raw.json": b'{"observation":"NEW OCR"}',
        "editor_draft.json": b'{"unsaved":"NEW EDITOR STATE"}',
        "export.xlsx": b"NEW XLSX BYTES\x00\x01",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {name: (root / name).read_bytes() for name in files}


def test_p12_after_evidence_crash_settles_after_generation_credential_policy_and_price_changes(runtime, tmp_path, monkeypatch):
    attempt_id, evidence_path = _crash_after_evidence(runtime)
    evidence_before = evidence_path.read_bytes()
    owner = _new_owner(runtime)
    binding, current_config, current_run = _rotate_and_revise(runtime, owner)
    # Neither the old PDF nor its old credential/client is available to finance recovery.
    Path(runtime.payload["pdf"]).unlink()
    protected = tmp_path / "user-state"
    before = _snapshot_files(protected)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    assert current_config.pricing != runtime.config.pricing
    assert current_run.generation == runtime.run.generation + 1
    assert binding.revision == 2
    assert owner.ledger.snapshot()["attempts"][0]["state"] == "unknown"
    assert owner.ledger.snapshot()["billing"]["billing"]["held_units"] == rub_units("0.2")

    def forbidden_post(*_args, **_kwargs):
        pytest.fail("historical settlement must not issue HTTP")
    monkeypatch.setattr(FakeTransport, "post", forbidden_post)
    receipt = owner.settle_saved_evidence(attempt_id, context)

    snapshot = owner.ledger.snapshot()
    assert receipt == {"attempt_id": attempt_id, "state": "terminal"}
    assert snapshot["attempts"][0]["state"] == "terminal"
    assert snapshot["attempts"][0]["actual_units"] == rub_units("0.14")
    billing = snapshot["billing"]["billing"]
    assert billing["spent_units"] == rub_units("0.14") and billing["held_units"] == 0
    assert billing["ceiling_units"] == rub_units("0.1") and billing["blocked"] is True
    with owner.ledger._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert evidence_path.read_bytes() == evidence_before
    assert {name: (protected / name).read_bytes() for name in before} == before

    # New admission is still blocked under the tighter current policy.
    new_attempt = Attempt(
        uuid4().hex, uuid4().hex, current_run.run_id, 1, 1, 1, binding, 2, (("quota", 2),),
        "a" * 64, 1, 1, price_units(current_config.pricing, 1, 1),
    )
    with pytest.raises(CoordinationError, match="needs_reconciliation"):
        owner.ledger.reserve(owner._owner, new_attempt)


def test_p12_missing_or_wrong_original_pricing_context_keeps_full_hold(runtime):
    attempt_id, _evidence_path = _crash_after_evidence(runtime)
    owner = _new_owner(runtime)
    with pytest.raises(CoordinationError, match="historical_context_required"):
        owner.settle_saved_evidence(attempt_id, None)
    wrong_config = runtime.config.model_copy(update={
        "pricing": TokenPricing(profile_id="synthetic-b2", input_rub_per_1k=Decimal("9"),
                                output_rub_per_1k=Decimal("1")),
    })
    wrong = HistoricalSettlementContext.capture(runtime.run, wrong_config)
    with pytest.raises(CoordinationError, match="historical_context_required"):
        owner.settle_saved_evidence(attempt_id, wrong)
    snapshot = owner.ledger.snapshot()
    assert snapshot["attempts"][0]["state"] == "unknown"
    assert snapshot["billing"]["billing"]["spent_units"] == 0
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    assert snapshot["billing"]["billing"]["blocked"] is True


def test_p12_concurrent_and_repeated_recovery_has_one_financial_receipt(runtime):
    attempt_id, _evidence_path = _crash_after_evidence(runtime)
    owner = _new_owner(runtime)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(
            lambda _index: owner.settle_saved_evidence(attempt_id, context), range(2),
        ))
    third = owner.settle_saved_evidence(attempt_id, context)
    assert first == second == third == {"attempt_id": attempt_id, "state": "terminal"}
    snapshot = owner.ledger.snapshot()
    assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
    assert snapshot["billing"]["billing"]["held_units"] == 0
    with owner.ledger._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations WHERE kind='outcome'").fetchone()[0] == 1


def test_p12_missing_usage_keeps_full_reservation_and_v1_terminal_cannot_be_refined(runtime):
    attempt_id, _evidence_path = _crash_after_evidence(runtime, no_usage=True)
    owner = _new_owner(runtime)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    receipt = owner.settle_saved_evidence(attempt_id, context)
    assert receipt == {"attempt_id": attempt_id, "state": "terminal"}
    snapshot = owner.ledger.snapshot()
    assert snapshot["attempts"][0]["actual_units"] is None
    assert snapshot["billing"]["billing"]["spent_units"] == 0
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    terminal = Outcome(
        uuid5(UUID(attempt_id), "settlement").hex, attempt_id,
        owner.evidence.read(attempt_id)["attempt"]["request_digest"], fingerprint({"different": "evidence"}),
        "terminal", rub_units("0.14"),
    )
    before = owner.ledger.snapshot()
    with pytest.raises(CoordinationError, match="operation_conflict"):
        owner.ledger.record_outcome(owner._owner, terminal)
    assert owner.ledger.snapshot() == before


@pytest.mark.parametrize("tamper", ["attempt_digest", "original_epoch", "pricing_hash"])
def test_p12_rejects_rehashed_but_ledger_inconsistent_evidence(runtime, tamper):
    attempt_id, evidence_path = _crash_after_evidence(runtime)
    envelope = json.loads(evidence_path.read_bytes())
    payload = envelope["payload"]
    if tamper == "attempt_digest":
        payload["attempt"]["request_digest"] = "f" * 64
    elif tamper == "original_epoch":
        payload["owner_epoch"] += 1
    else:
        payload["proof"]["pricing_sha256"] = "f" * 64
    envelope["sha256"] = fingerprint(payload)
    evidence_path.write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    owner = _new_owner(runtime)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    with pytest.raises(CoordinationError):
        owner.settle_saved_evidence(attempt_id, context)
    snapshot = owner.ledger.snapshot()
    assert snapshot["attempts"][0]["state"] == "unknown"
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    assert snapshot["billing"]["billing"]["spent_units"] == 0


def test_p12_sql_write_failure_rolls_back_then_exact_replay_converges(runtime, monkeypatch):
    attempt_id, _evidence_path = _crash_after_evidence(runtime)
    owner = _new_owner(runtime)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    original_connect = coordination_module.sqlite3.connect

    class FailOutcomeCommit(sqlite3.Connection):
        def commit(self):
            if (self.in_transaction and self.execute(
                "SELECT COUNT(*) FROM operations WHERE kind='outcome'",
            ).fetchone()[0]):
                raise sqlite3.OperationalError("synthetic outcome commit failure")
            return super().commit()

    def failed_connect(*args, **kwargs):
        kwargs["factory"] = FailOutcomeCommit
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(coordination_module.sqlite3, "connect", failed_connect)
        with pytest.raises(CoordinationError, match="ledger_unavailable"):
            owner.settle_saved_evidence(attempt_id, context)
    snapshot = owner.ledger.snapshot()
    assert snapshot["attempts"][0]["state"] == "unknown"
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    assert snapshot["billing"]["billing"]["spent_units"] == 0
    assert owner.settle_saved_evidence(attempt_id, context) == {"attempt_id": attempt_id, "state": "terminal"}


def test_p12_lost_commit_receipt_replays_without_a_second_expense(runtime, monkeypatch):
    attempt_id, _evidence_path = _crash_after_evidence(runtime)
    owner = _new_owner(runtime)
    context = HistoricalSettlementContext.capture(runtime.run, runtime.config)
    original = owner.ledger.record_outcome
    def commit_then_lose_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise CoordinationError("synthetic_lost_commit_receipt")
    with monkeypatch.context() as patch:
        patch.setattr(owner.ledger, "record_outcome", commit_then_lose_receipt)
        with pytest.raises(CoordinationError, match="synthetic_lost_commit_receipt"):
            owner.settle_saved_evidence(attempt_id, context)
    assert owner.settle_saved_evidence(attempt_id, context) == {"attempt_id": attempt_id, "state": "terminal"}
    snapshot = owner.ledger.snapshot()
    assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
    assert snapshot["billing"]["billing"]["held_units"] == 0


def test_p12_does_not_replace_proven_not_sent_with_saved_result(tmp_path):
    value = setup_runtime(tmp_path, pages=2)
    try:
        # A synthetic successful page supplies only typed result material; the
        # page-1 intent below is proven not sent and gets a deliberately forged
        # artifact to ensure v1 terminal history cannot be rewritten.
        asyncio.run(value.adapter.extract(request(value, 2)))
        from averon_import.services.ai_first.durable_dispatch import CALLER
        from averon_import.services.ai_first.orchestration import AdapterPageResult
        token = CALLER.set(value.adapter._caller)
        image = request(value, 1).image
        body = value.client._request_body(image.data, 1, value.adapter._user_context)
        call = value.dispatcher.call(value.client, 1, hashlib.sha256(body).hexdigest(), value.adapter.estimate(image))
        headers = {"Authorization": "Api-Key synthetic-b2-credential", "OpenAI-Project": "synthetic-project"}
        url = value.client.config.base_url + "/chat/completions"
        try:
            permit = value.dispatcher.prepare(value.client, call, url, body, headers)
            value.dispatcher.finish(call, None, None)
        finally:
            CALLER.reset(token)
        attempt = call.attempt
        with value.dispatcher.ledger._connection() as connection:
            assert connection.execute("SELECT state FROM attempts WHERE attempt_id=?",
                                      (attempt.attempt_id,)).fetchone()[0] == "not_sent"

        saved = value.dispatcher.evidence.read(DurableDispatcher.attempt_id(value.run.run_id, 2))
        result = AdapterPageResult.model_validate(saved["result"])
        result = result.model_copy(update={"extraction": result.extraction.model_copy(update={"page_number": 1})})
        proof = {
            "attempt": attempt.attempt_id, "state": "terminal",
            "usage": result.usage.model_dump(mode="json") if result.usage is not None else None,
            "status": result.http_status, "response_sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "result_sha256": fingerprint(result.model_dump(mode="json")),
            "pricing_sha256": fingerprint(value.config.pricing.model_dump(mode="json")),
        }
        outcome = Outcome(
            uuid5(UUID(attempt.attempt_id), "settlement").hex, attempt.attempt_id,
            attempt.request_digest, fingerprint(proof), "terminal", usage_units(value.config.pricing, result.usage),
        )
        value.dispatcher.evidence.write(attempt, permit.epoch, result, outcome, proof)
        context = HistoricalSettlementContext.capture(value.run, value.config)
        with pytest.raises(CoordinationError, match="historical_attempt_mismatch"):
            value.dispatcher.settle_saved_evidence(attempt.attempt_id, context)
        assert len(value.transport.calls) == 1  # page-2 fake POST only; recovery did not send page 1.
        with value.dispatcher.ledger._connection() as connection:
            assert connection.execute("SELECT state FROM attempts WHERE attempt_id=?",
                                      (attempt.attempt_id,)).fetchone()[0] == "not_sent"
    finally:
        value.dispatcher.close()
