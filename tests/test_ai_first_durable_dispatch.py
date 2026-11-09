"""Offline B2 boundary tests: real ledger, actual client delegation, subprocesses."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
import hashlib
import ipaddress
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import fitz
from PIL import Image
import pytest

from averon_import.services.ai_first import coordination as core, dispatch_policy
from averon_import.services.ai_first.coordination import CoordinationLedger
from averon_import.services.ai_first.coordination_contracts import BillingScope, Binding, Bootstrap, CoordinationError, QuotaScope, Run, rub_units
from averon_import.services.ai_first.durable_dispatch import CALLER, DispatchBinding, DurableDispatcher, credential_tag, price_units, target_digest, units_rub
from averon_import.services.ai_first.durable_qwen_adapter import DurableQwenVisionAdapter, configuration_digest
from averon_import.services.ai_first.orchestration import AdapterRequestError, CheckpointStore, FitZPageRenderer, OrchestrationConfig, PageExtractionRequest, PageImage, PageOrchestrator, TokenPricing, inspect_pdf
from averon_import.services.ai_first.private_acl import create_private_directory
from averon_import.services.ai_first.qwen_vision_adapter import QwenVisionQuotaRegistry
from averon_import.services.ocr.qwen_vision_client import QwenVisionClient, QwenVisionConfig, QwenVisionError, VisionLimits, _HttpsTransport
from test_ai_first_qwen_vision_adapter import FakeTransport, _response

WORKER = Path(__file__).with_name("ai_first_durable_dispatch_worker.py")
PROOF = "e" * 64


def image(page=1):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    return PageImage(page, buffer.getvalue(), "image/png", 32, 32, 72)


def open_runtime(payload, transport=None):
    """Also used by real independent workers. Never bootstrap an existing run."""
    config = OrchestrationConfig.model_validate_json(json.dumps(payload["config"]))
    binding = Binding(**{**payload["binding"], "quota_scope_ids": tuple(payload["binding"]["quota_scope_ids"])})
    run = Run(**payload["run"])
    wire = QwenVisionConfig("https://provider.invalid/v1", "gpt://synthetic-project/qwen-test",
                            "synthetic-b2-credential", "synthetic-project",
                            limits=VisionLimits(max_output_tokens=100, max_pages=100, max_requests=100, dpi=72))
    dispatcher = DurableDispatcher(payload["ledger"], installation_id=payload["installation_id"],
                                   evidence_root=payload["evidence"], enabled=True,
                                   **({"lease_ms": payload["lease_ms"]} if "lease_ms" in payload else {}),
                                   **({"schema_version": payload["schema_version"]}
                                      if "schema_version" in payload else {}),
                                   **({"maintenance_root": payload["maintenance_root"]}
                                      if "maintenance_root" in payload else {}))
    try:
        transport = transport or FakeTransport()
        client = QwenVisionClient(wire, transport=transport, dispatch_boundary=dispatcher)
        grant = DispatchBinding(run, binding, 1, tuple((q, 1) for q in binding.quota_scope_ids), config.pricing)
        adapter = DurableQwenVisionAdapter(client, dispatcher=dispatcher, dispatch_binding=grant,
                    quota_registry=QwenVisionQuotaRegistry(), requests_per_minute=config.requests_per_minute,
                    tokens_per_minute=config.tokens_per_minute, input_token_upper_bound=100,
                    pricing_profile_id="synthetic-b2")
        dispatcher.register(client)
        return SimpleNamespace(dispatcher=dispatcher, adapter=adapter, client=client, transport=transport,
                               config=config, run=run, payload=payload, pdf=Path(payload["pdf"]))
    except BaseException:
        dispatcher.close()
        raise


def setup_runtime(tmp_path, *, pages=1, concurrency=10, rpm=100, tpm=100000, budget="50", account_budget="100", transport=None, page_regions=None, input_rate="1", cached_rate=None, lease_ms=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf = tmp_path / "synthetic.pdf"
    document = fitz.open()
    for number in range(pages):
        document.new_page(width=100, height=100).insert_text((10, 30), f"SYNTHETIC {number + 1}")
    document.save(pdf)
    document.close()
    pricing = TokenPricing(profile_id="synthetic-b2", input_rub_per_1k=Decimal(input_rate), output_rub_per_1k=Decimal("1"),
                           cached_input_rub_per_1k=Decimal(cached_rate) if cached_rate is not None else None)
    config = OrchestrationConfig(concurrency=concurrency, dpi=72, max_requests=100,
               requests_per_minute=rpm, tokens_per_minute=tpm, max_budget_rub=Decimal(budget), pricing=pricing,
               page_regions=page_regions or {})
    wire = QwenVisionConfig("https://provider.invalid/v1", "gpt://synthetic-project/qwen-test",
                           "synthetic-b2-credential", "synthetic-project")
    binding = Binding("binding", 1, "generation_1", credential_tag(wire, "generation_1"),
                      "billing", ("quota",), target_digest(wire))
    spec = Bootstrap(uuid4().hex, uuid4().hex, PROOF,
           (BillingScope("billing", 1, rub_units(account_budget), 0, 0, True, PROOF),),
           (QuotaScope("quota", 1, rpm, tpm, concurrency, 60000, 0, 0, 0, True, PROOF),), (binding,), global_concurrency=10)
    CoordinationLedger.bootstrap(tmp_path / "ledger", spec)
    create_private_directory(tmp_path / "evidence")
    run = Run(uuid4().hex, "tenant", "document", 1, hashlib.sha256(pdf.read_bytes()).hexdigest(),
              configuration_digest(config, binding), rub_units(budget), 100)
    payload = {"ledger": str(tmp_path / "ledger"), "installation_id": spec.installation_id,
               "evidence": str(tmp_path / "evidence"), "pdf": str(pdf), "config": config.model_dump(mode="json"),
               "binding": asdict(binding), "run": asdict(run)}
    if lease_ms is not None:
        payload["lease_ms"] = lease_ms
    return open_runtime(payload, transport)


def request(runtime, page=1):
    return PageExtractionRequest("pdf-sha256:" + runtime.run.source_digest, page, image(page), runtime.run.run_id)


def run_orchestrator(runtime, *, cancel_event=None):
    identity = inspect_pdf(runtime.pdf, runtime.adapter, runtime.config, run_id=runtime.run.run_id)
    checkpoint = CheckpointStore(runtime.pdf.parent / "checkpoint", identity)
    orchestrator = PageOrchestrator(runtime.adapter, FitZPageRenderer(), runtime.config, checkpoint,
                                    durable_dispatch=runtime.adapter)
    return asyncio.run(orchestrator.run(runtime.pdf, cancel_event=cancel_event))


@pytest.fixture(autouse=True)
def offline_durable_mode(monkeypatch):
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


@pytest.fixture
def runtime(tmp_path):
    value = setup_runtime(tmp_path)
    try:
        yield value
    finally:
        value.dispatcher.close()


@contextmanager
def prepared(runtime, page=1):
    token = CALLER.set(runtime.adapter._caller)
    call = None
    try:
        item = image(page)
        body = runtime.client._request_body(item.data, page)
        call = runtime.dispatcher.call(runtime.client, page, hashlib.sha256(body).hexdigest(), runtime.adapter.estimate(item))
        headers = {"Authorization": "Api-Key synthetic-b2-credential", "OpenAI-Project": "synthetic-project"}
        yield call, body, headers, runtime.client.config.base_url + "/chat/completions"
    finally:
        if call is not None:
            runtime.dispatcher.finish(call, None, None)
        CALLER.reset(token)


def test_missing_ledger_never_bootstraps(tmp_path):
    create_private_directory(tmp_path / "evidence")
    with pytest.raises(CoordinationError):
        DurableDispatcher(tmp_path / "missing", installation_id=uuid4().hex, evidence_root=tmp_path / "evidence", enabled=True)
    assert not (tmp_path / "missing").exists()
    with pytest.raises(CoordinationError, match="durable_mode_required"):
        DurableDispatcher(tmp_path / "missing", installation_id=uuid4().hex, evidence_root=tmp_path / "evidence")


def test_injected_transport_cannot_bypass_missing_authorization(runtime):
    with pytest.raises(QwenVisionError, match="durable_authorization_required"):
        QwenVisionClient(runtime.client.config, transport=runtime.transport).extract(image().data, 1)
    with pytest.raises(QwenVisionError, match="dispatch_caller_mismatch"):
        runtime.client.extract(image().data, 1)
    assert runtime.transport.calls == []


@pytest.mark.parametrize("pages", [2, 4, 10])
def test_actual_orchestrator_boundary_and_single_financial_authority(tmp_path, monkeypatch, pages):
    from averon_import.services.ai_first import orchestration
    def forbidden(*args, **kwargs):
        raise AssertionError("durable path used a second local authority")
    monkeypatch.setattr(orchestration._BudgetLedger, "__init__", forbidden)
    monkeypatch.setattr(orchestration._RateGate, "__init__", forbidden)
    transport = FakeTransport(request_barrier=threading.Barrier(pages))
    runtime = setup_runtime(tmp_path, pages=pages, concurrency=pages, transport=transport)
    original = transport.post
    def checked_post(url, **kwargs):
        rows = runtime.dispatcher.ledger.snapshot()["attempts"]
        assert rows and any(row["state"] == "intent" for row in rows)
        return original(url, **kwargs)
    transport.post = checked_post
    try:
        report = run_orchestrator(runtime)
        assert len(report.draft.pages) == pages
        assert all(record.state == "completed" for record in report.pages)
        assert sorted(transport.started_pages) == list(range(1, pages + 1))
        assert transport.max_active == pages
        assert report.max_parallel_requests <= pages
        snapshot = runtime.dispatcher.ledger.snapshot()
        assert snapshot["billing"]["billing"]["spent_units"] == pages * rub_units("0.14")
        assert all(row["state"] == "terminal" for row in snapshot["attempts"])
        assert len(list((tmp_path / "evidence").glob("*.json"))) == pages
        assert not list((tmp_path / "evidence").glob("*.pending"))
        assert runtime.dispatcher._calls == 0
    finally:
        runtime.dispatcher.close()


def test_simultaneous_duplicate_operation_and_attempt_send_once(runtime):
    async def race():
        return await asyncio.gather(*(runtime.adapter.extract(request(runtime)) for _ in range(4)), return_exceptions=True)
    results = asyncio.run(race())
    assert len(runtime.transport.calls) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert len(runtime.dispatcher.ledger.snapshot()["attempts"]) == 1
    assert runtime.dispatcher._calls == 0


def test_one_permit_cannot_be_consumed_twice(runtime):
    with prepared(runtime) as (call, body, headers, url):
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
        with pytest.raises(CoordinationError, match="dispatch_permit_consumed"):
            runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
        with pytest.raises(CoordinationError, match="dispatch_already_reserved"):
            runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
    assert runtime.transport.calls == []


@pytest.mark.parametrize("change", ["body", "model", "project", "credential", "endpoint", "caller", "epoch", "generation"])
def test_mismatch_at_real_client_boundary_never_calls_custom_transport(runtime, change):
    with prepared(runtime) as (call, body, headers, url):
        if change in ("body", "model", "project", "credential", "endpoint", "caller"):
            if change == "body":
                call.body_digest = "0" * 64
            elif change == "caller":
                CALLER.set(object())
            else:
                field, value = {"model": ("model", "gpt://other/model"), "project": ("folder_id", "other"),
                                "credential": ("api_key", "other-credential"), "endpoint": ("base_url", "https://other.invalid/v1")}[change]
                runtime.client.config = replace(runtime.client.config, **{field: value})
            with pytest.raises(QwenVisionError):
                runtime.client.extract(image().data, 1, dispatch_call=call)
        else:
            permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
            if change == "epoch":
                permit.epoch += 1
            else:
                run = replace(runtime.run, run_id=uuid4().hex, generation=2)
                runtime.dispatcher.ledger.register_run(runtime.dispatcher._owner, uuid4().hex, run)
            with pytest.raises(CoordinationError):
                runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
    assert runtime.transport.calls == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_error_never_implies_free_or_retry(runtime, status):
    runtime.transport.handler = lambda page, count, kwargs: _response(page, status=status)
    with pytest.raises(AdapterRequestError):
        asyncio.run(runtime.adapter.extract(request(runtime)))
    assert len(runtime.transport.calls) == 1
    snapshot = runtime.dispatcher.ledger.snapshot()
    assert snapshot["billing"]["billing"]["spent_units"] == 0
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    assert snapshot["attempts"][0]["state"] == ("unknown" if status >= 500 else "terminal")


def test_transport_timeout_is_unknown_and_retains_cost(runtime):
    def timed_out(*args):
        raise TimeoutError
    runtime.transport.handler = timed_out
    with pytest.raises(AdapterRequestError):
        asyncio.run(runtime.adapter.extract(request(runtime)))
    assert len(runtime.transport.calls) == 1
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "unknown"


def test_cancel_before_boundary_is_proven_not_sent(runtime, monkeypatch):
    original = runtime.dispatcher.ledger.validate_dispatch
    def cancel_after_validation(*args, **kwargs):
        original(*args, **kwargs)
        current_cancel.set()
    monkeypatch.setattr(runtime.dispatcher.ledger, "validate_dispatch", cancel_after_validation)
    with prepared(runtime) as (call, body, headers, url):
        current_cancel = threading.Event()
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        with pytest.raises(CoordinationError, match="cancelled_before_dispatch"):
            runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, current_cancel)
    assert runtime.transport.calls == []
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "not_sent"


def test_cancel_after_delegation_drains_and_retains_unknown(runtime):
    def wait_for_cancel(page, count, kwargs):
        assert kwargs["cancel"].wait(5)
        raise QwenVisionError("cancelled")
    runtime.transport.handler = wait_for_cancel
    async def cancel_in_flight():
        task = asyncio.create_task(runtime.adapter.extract(request(runtime)))
        while not runtime.transport.started_event.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        return await asyncio.gather(task, return_exceptions=True)
    result = asyncio.run(cancel_in_flight())[0]
    assert isinstance(result, AdapterRequestError) and result.outcome_unknown
    assert runtime.transport.active == 0 and runtime.dispatcher._calls == 0
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "unknown"


def test_lost_real_sqlite_commit_receipt_never_authorizes_post(runtime, monkeypatch):
    original = core.sqlite3.connect
    class LostReceipt(sqlite3.Connection):
        def commit(self):
            super().commit()
            if self.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                raise sqlite3.OperationalError("injected lost receipt")
    def connect(*args, **kwargs):
        kwargs["factory"] = LostReceipt
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(core.sqlite3, "connect", connect)
        with pytest.raises(AdapterRequestError):
            asyncio.run(runtime.adapter.extract(request(runtime)))
    assert runtime.transport.calls == []
    runtime.dispatcher.close()
    restarted = open_runtime(runtime.payload)
    try:
        assert restarted.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "unknown"
        with pytest.raises((AdapterRequestError, CoordinationError)):
            asyncio.run(restarted.adapter.extract(request(restarted)))
        assert restarted.transport.calls == []
    finally:
        restarted.dispatcher.close()


def test_legacy_ai_auth_fallback_is_excluded_before_urlopen(runtime, monkeypatch):
    from averon_import.ai.provider import OpenAICompatibleProvider
    from averon_import.ai.config import ProviderSettings
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: calls.append(args))
    settings = ProviderSettings(key="synthetic", label="synthetic", base_url="https://provider.invalid/v1",
                                model="synthetic", api_key="synthetic", auth_schemes=("Api-Key", "Bearer"))
    provider = OpenAICompatibleProvider(settings, timeout_seconds=1, temperature=0, max_tokens=100)
    with pytest.raises(CoordinationError, match="durable_authorization_required"):
        provider.complete([{"role": "user", "content": "synthetic"}])
    assert calls == []


@contextmanager
def worker(payload, mode):
    process = subprocess.Popen([sys.executable, str(WORKER), mode], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               cwd=WORKER.parents[1])
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def receive(process):
    def next_json():
        for line in process.stdout:
            if line.startswith("{"):
                return json.loads(line)
        return None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(next_json)
        try:
            result = future.result(timeout=30)
        except TimeoutError:
            process.kill()
            raise
    if result is None:
        pytest.fail(process.stderr.read())
    return result


def test_two_processes_only_one_real_dispatcher_and_financial_authority(runtime):
    payload = runtime.payload
    runtime.dispatcher.close()
    with worker(payload, "hold") as first:
        owner = receive(first)
        assert owner["window"] == "owner"
        with worker(payload, "hold") as second:
            assert receive(second) == {"error": "dispatcher_busy"}
        first.kill()
        first.wait(timeout=10)
    replacement = open_runtime(payload)
    try:
        assert replacement.dispatcher._owner.epoch > owner["epoch"]
        result = asyncio.run(replacement.adapter.extract(request(replacement)))
        assert result.extraction.page_number == 1
        assert len(replacement.transport.calls) == 1
        assert replacement.dispatcher.ledger.snapshot()["billing"]["billing"]["spent_units"] == rub_units("0.14")
    finally:
        replacement.dispatcher.close()


@pytest.mark.parametrize("window,posts", [("before_post", 0), ("during_http", 1)])
def test_real_process_death_never_reissues_paid_post(runtime, window, posts):
    payload = runtime.payload
    runtime.dispatcher.close()
    with worker(payload, window) as process:
        signal = receive(process)
        assert signal["window"] == window and signal["posts"] == posts
        process.kill()
        process.wait(timeout=10)
    replacement = open_runtime(payload)
    try:
        snapshot = replacement.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["state"] == "unknown"
        assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
        report = run_orchestrator(replacement)
        assert report.pages[0].state == "unknown_outcome"
        assert replacement.transport.calls == []
        with pytest.raises((CoordinationError, AdapterRequestError)):
            asyncio.run(replacement.adapter.extract(request(replacement)))
        assert replacement.transport.calls == []
    finally:
        replacement.dispatcher.close()


def test_real_death_after_result_fsync_recovers_settlement_and_page_without_post(runtime):
    payload = runtime.payload
    runtime.dispatcher.close()
    with worker(payload, "after_evidence") as process:
        assert receive(process)["window"] == "after_evidence"
        process.kill()
        process.wait(timeout=10)
    evidence_file = next(Path(payload["evidence"]).glob("*.json"))
    evidence_bytes = evidence_file.read_bytes()
    replacement = open_runtime(payload)
    try:
        report = run_orchestrator(replacement)
        assert report.pages[0].state == "completed"
        assert replacement.transport.calls == []
        snapshot = replacement.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["state"] == "terminal"
        assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
        assert snapshot["billing"]["billing"]["held_units"] == 0
        # B2 never clears B1's recovery stop. That administrative action is B6.
        assert snapshot["billing"]["billing"]["blocked"] is True
        assert evidence_file.read_bytes() == evidence_bytes
    finally:
        replacement.dispatcher.close()


@pytest.mark.parametrize("limit", ["rpm", "tpm", "budget"])
@pytest.mark.parametrize("pages", [2, 4, 10])
def test_real_quota_and_budget_admission_for_fake_bursts(tmp_path, pages, limit):
    runtime = setup_runtime(tmp_path, pages=pages,
        rpm=1 if limit == "rpm" else 100,
        tpm=200 if limit == "tpm" else 100000,
        account_budget="0.2" if limit == "budget" else "100")
    try:
        async def burst():
            return await asyncio.gather(*(runtime.adapter.extract(request(runtime, page)) for page in range(1, pages + 1)),
                                        return_exceptions=True)
        asyncio.run(burst())
        assert len(runtime.transport.calls) <= 1
        snapshot = runtime.dispatcher.ledger.snapshot()
        assert len(snapshot["attempts"]) == 1
        billing = snapshot["billing"]["billing"]
        assert billing["spent_units"] + billing["held_units"] <= rub_units("0.2")
        assert runtime.dispatcher._calls == 0
    finally:
        runtime.dispatcher.close()


def test_corrupt_immutable_result_evidence_blocks_resume_without_post(runtime):
    asyncio.run(runtime.adapter.extract(request(runtime)))
    path = next(Path(runtime.payload["evidence"]).glob("*.json"))
    content = json.loads(path.read_bytes())
    content["payload"]["result"]["extraction"]["page_number"] = 2
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(CoordinationError, match="result_evidence_invalid"):
        run_orchestrator(runtime)
    assert len(runtime.transport.calls) == 1


def test_slow_reserve_does_not_block_event_loop(runtime, monkeypatch):
    original = runtime.dispatcher.ledger.reserve
    def delayed(*args):
        time.sleep(0.15)
        return original(*args)
    monkeypatch.setattr(runtime.dispatcher.ledger, "reserve", delayed)
    async def measure():
        task = asyncio.create_task(runtime.adapter.extract(request(runtime)))
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.005)
        await task
        return ticks
    assert asyncio.run(measure()) >= 20


def test_default_mode_is_unchanged_and_durable_mode_is_sticky(runtime, monkeypatch):
    assert dispatch_policy.durable_required()
    monkeypatch.delenv("AVERON_AI_DURABLE_DISPATCH")
    assert dispatch_policy.durable_required()
    with pytest.raises(QwenVisionError, match="durable_authorization_required"):
        QwenVisionClient(runtime.client.config, transport=runtime.transport).extract(image().data, 1)
    assert runtime.transport.calls == []


def test_human_review_raw_observations_and_exports_are_unchanged(tmp_path):
    protected = tmp_path / "existing-private-workspace"
    protected.mkdir()
    files = {"human_review.json": b'{"decision":"confirmed","synthetic":true}',
             "ocr_raw.json": b'{"observation":"SYNTHETIC ORIGINAL"}',
             "editor_draft.json": b'{"unsaved":"SYNTHETIC ORIGINAL"}'}
    for name, data in files.items():
        (protected / name).write_bytes(data)
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active["A1"] = "SYNTHETIC ORIGINAL EXPORT"
    workbook.save(protected / "export.xlsx")
    before = {file.name: file.read_bytes() for file in protected.iterdir()}
    runtime = setup_runtime(tmp_path / "b2", pages=2)
    try:
        report = run_orchestrator(runtime)
        assert all(page.state == "completed" for page in report.pages)
        assert {file.name: file.read_bytes() for file in protected.iterdir()} == before
    finally:
        runtime.dispatcher.close()


def test_real_sqlite_busy_process_never_calls_transport(runtime):
    with worker(runtime.payload, "busy") as process:
        assert receive(process) == {"window": "busy"}
        with pytest.raises(AdapterRequestError):
            asyncio.run(runtime.adapter.extract(request(runtime)))
    assert runtime.transport.calls == []
    assert runtime.dispatcher.ledger.snapshot()["attempts"] == []


def test_actual_sqlite_readonly_failure_has_no_local_fallback(runtime, monkeypatch):
    original = core.sqlite3.connect
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.execute("PRAGMA query_only=ON")
        return connection
    with monkeypatch.context() as patch:
        patch.setattr(core.sqlite3, "connect", connect)
        with pytest.raises(AdapterRequestError):
            asyncio.run(runtime.adapter.extract(request(runtime)))
    assert runtime.transport.calls == []
    assert runtime.dispatcher.ledger.snapshot()["attempts"] == []


@pytest.mark.parametrize("field", ["attempt_id", "claim_seq", "binding", "quota_revisions"])
def test_forged_permit_metadata_is_rejected_before_transport(runtime, field):
    with prepared(runtime) as (call, body, headers, url):
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        replacement = {"attempt_id": uuid4().hex, "claim_seq": 2,
                       "binding": replace(permit.attempt.binding, target_digest="f" * 64),
                       "quota_revisions": (("quota", 2),)}[field]
        permit.attempt = replace(permit.attempt, **{field: replacement})
        with pytest.raises(CoordinationError, match="dispatch_request_mismatch"):
            runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
    assert runtime.transport.calls == []


def test_borrowed_permit_is_rejected_from_another_thread(runtime):
    with prepared(runtime) as (call, body, headers, url):
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        def borrow():
            token = CALLER.set(runtime.adapter._caller)
            try:
                with pytest.raises(CoordinationError, match="dispatch_caller_mismatch"):
                    runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
            finally:
                CALLER.reset(token)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(borrow).result(timeout=5)
    assert runtime.transport.calls == []


def test_policy_revision_after_reservation_is_fenced_at_boundary(runtime):
    with prepared(runtime) as (call, body, headers, url):
        permit = runtime.dispatcher.prepare(runtime.client, call, url, body, headers)
        runtime.dispatcher.ledger.revise_quota(runtime.dispatcher._owner, uuid4().hex, "quota", 1,
                        requests=100, tokens=100000, concurrency=10, window_ms=60000)
        with pytest.raises(CoordinationError, match="policy_revision_mismatch"):
            runtime.dispatcher.consume(permit, runtime.client, call, url, body, headers, None)
    assert runtime.transport.calls == []


def test_cancel_during_slow_reservation_drains_commit_and_proves_no_post(runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = runtime.dispatcher.ledger.reserve
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(runtime.dispatcher.ledger, "reserve", delayed)
    async def cancel_commit():
        task = asyncio.create_task(runtime.adapter.extract(request(runtime)))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        return await asyncio.gather(task, return_exceptions=True)
    asyncio.run(cancel_commit())
    assert runtime.transport.calls == []
    assert runtime.dispatcher._calls == 0
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "not_sent"


def test_cancel_after_result_fsync_preserves_success_and_settlement(runtime, monkeypatch):
    saved = threading.Event()
    release = threading.Event()
    original = runtime.dispatcher.evidence.write
    def delayed(*args):
        original(*args)
        saved.set()
        assert release.wait(5)
    monkeypatch.setattr(runtime.dispatcher.evidence, "write", delayed)
    async def cancel_settlement():
        task = asyncio.create_task(runtime.adapter.extract(request(runtime)))
        while not saved.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        return await task
    result = asyncio.run(cancel_settlement())
    assert result.extraction.page_number == 1
    assert len(runtime.transport.calls) == 1
    assert runtime.dispatcher._calls == 0
    assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "terminal"


def test_yandex_ocr_injected_path_is_excluded_in_durable_process(monkeypatch):
    from averon_import.services.ocr.yandex_vision import YandexVisionProvider
    provider = object.__new__(YandexVisionProvider)
    calls = []
    provider._http = SimpleNamespace(request=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(CoordinationError, match="durable_authorization_required"):
        provider._request_with_retry("POST", "https://vision.invalid/ocr", body=b"synthetic",
                    api_key="synthetic", folder_id="synthetic", timeout=1, cancel=None, warnings=[])
    assert calls == []


def test_schema_error_does_not_trigger_a_second_paid_post(runtime):
    runtime.transport.handler = lambda page, count, kwargs: _response(page, content="not valid JSON")
    for _ in range(2):
        with pytest.raises(AdapterRequestError):
            asyncio.run(runtime.adapter.extract(request(runtime)))
    assert len(runtime.transport.calls) == 1
    assert runtime.dispatcher.ledger.snapshot()["billing"]["billing"]["spent_units"] == rub_units("0.14")


def test_async_close_waits_for_inflight_http_and_settlement(runtime):
    entered = threading.Event()
    release = threading.Event()
    def delayed(page, count, kwargs):
        entered.set()
        assert release.wait(5)
        return _response(page)
    runtime.transport.handler = delayed
    async def close_in_flight():
        request_task = asyncio.create_task(runtime.adapter.extract(request(runtime)))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        close_task = asyncio.create_task(runtime.dispatcher.aclose())
        await asyncio.sleep(0.05)
        assert not close_task.done()
        with pytest.raises(CoordinationError, match="dispatcher_busy"):
            CoordinationLedger(runtime.payload["ledger"], installation_id=runtime.payload["installation_id"]).dispatcher()
        release.set()
        await request_task
        await close_task
    asyncio.run(close_in_flight())
    assert runtime.dispatcher._calls == 0
    with CoordinationLedger(runtime.payload["ledger"], installation_id=runtime.payload["installation_id"]).dispatcher():
        pass


def test_cancelled_async_open_closes_the_new_owner_instead_of_leaking_it(runtime, monkeypatch):
    runtime.dispatcher.close()
    entered, release = threading.Event(), threading.Event()
    original = DurableDispatcher.__init__
    def delayed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(DurableDispatcher, "__init__", delayed)
    async def cancel_open():
        task = asyncio.create_task(DurableDispatcher.open(runtime.payload["ledger"],
            installation_id=runtime.payload["installation_id"], evidence_root=runtime.payload["evidence"], enabled=True))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel_open())
    with CoordinationLedger(runtime.payload["ledger"], installation_id=runtime.payload["installation_id"]).dispatcher():
        pass


def test_result_fsync_failure_retains_liability_and_closes_resources(runtime, monkeypatch):
    original = os.fsync
    def failed_sync(descriptor):
        raise OSError("synthetic fsync failure")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed_sync)
        with pytest.raises(AdapterRequestError, match="durable_settlement_failed"):
            asyncio.run(runtime.adapter.extract(request(runtime)))
    snapshot = runtime.dispatcher.ledger.snapshot()
    assert snapshot["attempts"][0]["state"] == "intent"
    assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
    assert len(runtime.transport.calls) == 1 and runtime.dispatcher._calls == 0
    # Windows refuses unlinking open files; closing the failed writer is real.
    for path in Path(runtime.payload["evidence"]).glob("*.pending"):
        path.unlink()


def test_mutating_pricing_after_binding_is_rejected_before_post(runtime):
    runtime.dispatcher._clients[runtime.client][1].pricing.input_rub_per_1k = Decimal("2")
    with pytest.raises(AdapterRequestError):
        asyncio.run(runtime.adapter.extract(request(runtime)))
    assert runtime.transport.calls == []


def test_superseded_generation_cannot_return_success_but_keeps_cost_and_evidence(runtime):
    def supersede(page, count, kwargs):
        newer = replace(runtime.run, run_id=uuid4().hex, generation=2)
        runtime.dispatcher.ledger.register_run(runtime.dispatcher._owner, uuid4().hex, newer)
        return _response(page)
    runtime.transport.handler = supersede
    with pytest.raises(AdapterRequestError, match="durable_settlement_failed"):
        asyncio.run(runtime.adapter.extract(request(runtime)))
    assert len(runtime.transport.calls) == 1
    snapshot = runtime.dispatcher.ledger.snapshot()
    assert snapshot["attempts"][0]["state"] == "terminal"
    assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
    assert len(list(Path(runtime.payload["evidence"]).glob("*.json"))) == 1


def test_unknown_page_does_not_lose_other_successful_page(tmp_path):
    def handler(page, count, kwargs):
        if page == 1:
            raise TimeoutError
        return _response(page)
    runtime = setup_runtime(tmp_path, pages=2, concurrency=2,
                 transport=FakeTransport(handler=handler, request_barrier=threading.Barrier(2)))
    try:
        report = run_orchestrator(runtime)
        assert [page.state for page in report.pages] == ["unknown_outcome", "completed"]
        billing = runtime.dispatcher.ledger.snapshot()["billing"]["billing"]
        assert billing["held_units"] == rub_units("0.2") and billing["spent_units"] == rub_units("0.14")
        assert len(runtime.transport.calls) == 2
    finally:
        runtime.dispatcher.close()


def test_checkpoint_failure_recovers_success_without_reposting(runtime, monkeypatch):
    from averon_import.services.ai_first.orchestration import OrchestrationError
    original = CheckpointStore.write
    async def fail_completed(store, record):
        if record.state == "completed":
            raise OrchestrationError("synthetic_checkpoint_failure")
        return await original(store, record)
    with monkeypatch.context() as patch:
        patch.setattr(CheckpointStore, "write", fail_completed)
        with pytest.raises(OrchestrationError):
            run_orchestrator(runtime)
    assert len(runtime.transport.calls) == 1
    runtime.dispatcher.close()
    restarted = open_runtime(runtime.payload)
    try:
        assert run_orchestrator(restarted).pages[0].state == "completed"
        assert restarted.transport.calls == []
        assert restarted.dispatcher.ledger.snapshot()["billing"]["billing"]["spent_units"] == rub_units("0.14")
    finally:
        restarted.dispatcher.close()


def test_region_result_recovery_is_idempotent(tmp_path):
    runtime = setup_runtime(tmp_path, page_regions={1: (0.0, 0.0, 0.5, 0.5)})
    try:
        first = run_orchestrator(runtime)
        second = run_orchestrator(runtime)
        assert first.pages[0].extraction == second.pages[0].extraction
        assert first.pages[0].state == second.pages[0].state == "completed"
        assert len(runtime.transport.calls) == 1
    finally:
        runtime.dispatcher.close()


@pytest.mark.parametrize("field", ["source_document_id", "image"])
def test_document_image_identity_mismatch_is_denied_before_intent(runtime, field):
    item = request(runtime)
    setattr(item, field, "pdf-sha256:" + "f" * 64 if field == "source_document_id" else image(2))
    with pytest.raises(AdapterRequestError, match="durable_run_mismatch"):
        asyncio.run(runtime.adapter.extract(item))
    assert runtime.transport.calls == []
    assert runtime.dispatcher.ledger.snapshot()["attempts"] == []


def test_exact_nano_rub_cost_is_independent_of_decimal_context_and_tail(tmp_path):
    runtime = setup_runtime(tmp_path, input_rate="1." + "0" * 100 + "1")
    try:
        with localcontext() as context:
            context.prec = 3
            assert price_units(runtime.config.pricing, 100, 100) == 200000001
            assert units_rub(140000001) == Decimal("0.140000001")
            report = run_orchestrator(runtime)
        snapshot = runtime.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["reserve_units"] == 200000001
        assert snapshot["billing"]["billing"]["spent_units"] == 140000001
        assert report.pages[0].attempts[-1].reserved_cost_rub == Decimal("0.200000001")
        assert report.pages[0].attempts[-1].actual_cost_rub == Decimal("0.140000001")
        assert report.actual_cost_rub == Decimal("0.140000001")
    finally:
        runtime.dispatcher.close()


@pytest.mark.parametrize("rate", ["1e-5000", "1e5000"])
def test_excessive_pricing_representation_is_rejected_without_money_or_float(rate):
    pricing = TokenPricing(input_rub_per_1k=Decimal(rate), output_rub_per_1k=Decimal("1"))
    with pytest.raises(CoordinationError, match="invalid_durable_pricing"):
        price_units(pricing, 1, 1)


def test_unregistered_caller_cannot_impersonate_existing_run_metadata(runtime):
    other = FakeTransport()
    client = QwenVisionClient(runtime.client.config, transport=other, dispatch_boundary=runtime.dispatcher)
    grant = replace(runtime.adapter.dispatch_binding, run=replace(runtime.run, document_id="another_document"))
    adapter = DurableQwenVisionAdapter(client, dispatcher=runtime.dispatcher, dispatch_binding=grant,
              quota_registry=QwenVisionQuotaRegistry(), requests_per_minute=100, tokens_per_minute=100000,
              input_token_upper_bound=100, pricing_profile_id="synthetic-b2")
    with pytest.raises(CoordinationError, match="durable_run_not_registered"):
        asyncio.run(adapter.extract(request(runtime)))
    with pytest.raises(CoordinationError):
        runtime.dispatcher.register(client)
    assert other.calls == []
    assert runtime.dispatcher.ledger.snapshot()["attempts"] == []
    asyncio.run(runtime.adapter.extract(request(runtime)))
    assert len(runtime.transport.calls) == 1


def test_reservation_covers_configured_cache_tier_even_when_more_expensive(tmp_path):
    runtime = setup_runtime(tmp_path, cached_rate="2")
    try:
        asyncio.run(runtime.adapter.extract(request(runtime)))
        snapshot = runtime.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["reserve_units"] == rub_units("0.3")
        assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.15")
        assert len(runtime.transport.calls) == 1
    finally:
        runtime.dispatcher.close()


def test_missing_price_sensitive_cached_usage_keeps_reservation(tmp_path):
    def no_cached_usage(page, count, kwargs):
        response = _response(page)
        value = json.loads(response.body)
        value["usage"].pop("prompt_tokens_details")
        return replace(response, body=json.dumps(value).encode())
    runtime = setup_runtime(tmp_path, cached_rate="0.5", transport=FakeTransport(handler=no_cached_usage))
    try:
        result = asyncio.run(runtime.adapter.extract(request(runtime)))
        assert result.extraction.page_number == 1
        billing = runtime.dispatcher.ledger.snapshot()["billing"]["billing"]
        assert billing["spent_units"] == 0 and billing["held_units"] == rub_units("0.2")
        assert len(runtime.transport.calls) == 1
    finally:
        runtime.dispatcher.close()


def test_real_heartbeat_keeps_owner_valid_beyond_initial_http_lease(tmp_path):
    runtime = setup_runtime(tmp_path, lease_ms=1200, transport=FakeTransport(delay_s=2.5))
    try:
        with runtime.dispatcher.ledger._connection() as connection:
            initial_deadline = connection.execute("SELECT lease_until_ms FROM meta").fetchone()[0]
        result = asyncio.run(runtime.adapter.extract(request(runtime)))
        assert runtime.dispatcher.ledger.clock().milliseconds > initial_deadline
        assert result.extraction.page_number == 1 and len(runtime.transport.calls) == 1
        assert not runtime.dispatcher._failed.is_set()
        assert runtime.dispatcher.ledger.snapshot()["attempts"][0]["state"] == "terminal"
    finally:
        runtime.dispatcher.close()
