from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import time

import fitz
import pytest
from pydantic import ValidationError

from averon_import.services.ai_first.assembly import page_from_payload
from averon_import.services.ai_first.orchestration import (
    AdapterDescriptor, AdapterPageResult, AdapterRequestError, CheckpointStore, FitZPageRenderer,
    OrchestrationConfig, OrchestrationError, PageAttempt, PageExtractionRequest,
    PageImage, PageOrchestrator, PageRunRecord, RequestEstimate, TokenPricing,
    TokenUsage, inspect_pdf,
)

SYNTHETIC_RAW = "SYNTHETIC_ONLY_RAW_FIELD"


def make_pdf(path: Path, page_count: int = 1, *, marker: str = "synthetic") -> Path:
    document = fitz.open()
    for index in range(page_count):
        document.new_page().insert_text((40, 40), f"{marker} page {index + 1}")
    document.save(path)
    document.close()
    return path


def make_config(**overrides) -> OrchestrationConfig:
    value = {
        "concurrency": 3, "dpi": 72,
        "requests_per_minute": 60, "tokens_per_minute": 120000,
        "max_budget_rub": Decimal("100"),
        "pricing": TokenPricing(
            input_rub_per_1k=Decimal("0.20"),
            cached_input_rub_per_1k=Decimal("0.05"),
            output_rub_per_1k=Decimal("0.30"),
        ),
        "max_image_bytes": 1_000_000, "max_image_pixels": 2_000_000,
    }
    value.update(overrides)
    return OrchestrationConfig(**value)


class SyntheticRenderer:
    def __init__(self, image_bytes: int = 4096):
        self.image_bytes = image_bytes

    def render(self, pdf_path, page_number, dpi, config):
        return PageImage(page_number, b"I" * self.image_bytes, "image/png", 64, 64, dpi)


class SyntheticAdapter:
    def __init__(
        self, *, model_version="synthetic-v1", prompt_version="prompt-v1",
        schema_version="schema-v1", delay=None, fail_pages=None,
        unknown_once=None, cancel_event=None, usage_missing=False,
    ):
        self._descriptor = AdapterDescriptor(
            provider_id="mock-vision", model_id="synthetic-model",
            model_version=model_version, prompt_version=prompt_version,
            schema_version=schema_version, parameters={"detail": "low"},
        )
        self.delay = delay or (lambda page: 0.001)
        self.fail_pages = set(fail_pages or ())
        self.unknown_once = set(unknown_once or ())
        self.cancel_event = cancel_event
        self.usage_missing = usage_missing
        self.calls = []
        self.finished = []
        self.started_at = []
        self.active = 0
        self.max_active = 0

    @property
    def descriptor(self):
        return self._descriptor

    def estimate(self, image: PageImage) -> RequestEstimate:
        return RequestEstimate(reserved_input_tokens=10, reserved_output_tokens=10)

    async def extract(self, request: PageExtractionRequest) -> AdapterPageResult:
        number = request.page_number
        self.calls.append(number)
        self.started_at.append(time.monotonic())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.cancel_event is not None:
                self.cancel_event.set()
                await asyncio.sleep(2)
            await asyncio.sleep(self.delay(number))
            if number in self.unknown_once:
                self.unknown_once.remove(number)
                raise AdapterRequestError("synthetic_timeout", outcome_unknown=True)
            if number in self.fail_pages:
                raise AdapterRequestError("synthetic_http_failure", outcome_unknown=False, usage=TokenUsage(input_tokens=10, output_tokens=8))
            payload = {
                "page": number, "tables": [], "unreadable_fragments": [],
                "handwritten_replacements": [], "future_vendor_field": SYNTHETIC_RAW,
            }
            extraction = page_from_payload(payload, source_document_id=request.source_document_id)
            return AdapterPageResult(
                extraction=extraction,
                usage=None if self.usage_missing else TokenUsage(input_tokens=10, output_tokens=8),
            )
        finally:
            self.active -= 1
            self.finished.append(number)


def run_job(
    tmp_path: Path, pdf: Path, adapter, *, config=None, checkpoint=None,
    run_id="synthetic-run", progress=None, renderer=None, rate_window_s=0.01, **kwargs,
):
    config = config or make_config()
    identity = inspect_pdf(pdf, adapter, config, run_id=run_id)
    store = CheckpointStore(checkpoint or tmp_path / "private-checkpoint", identity)
    orchestrator = PageOrchestrator(
        adapter, renderer or SyntheticRenderer(), config, store,
        progress=progress, rate_window_s=rate_window_s,
    )
    return asyncio.run(orchestrator.run(pdf, **kwargs))


def test_30_pages_concurrency_three_and_deterministic_source_order(tmp_path):
    pdf = make_pdf(tmp_path / "synthetic-30.pdf", 30)
    adapter = SyntheticAdapter(delay=lambda page: (page % 5) * 0.001)
    report = run_job(tmp_path, pdf, adapter)
    assert report.status == "completed"
    assert report.source_integrity_verified is True
    assert report.source_integrity_error_code is None
    assert report.expected_page_count == 30
    assert report.adapter.model_version == "synthetic-v1"
    assert report.config.concurrency == 3 and report.config.dpi == 72
    assert [page.page_number for page in report.pages] == list(range(1, 31))
    assert report.max_parallel_requests == adapter.max_active == 3
    assert adapter.finished != list(range(1, 31))
    assert report.draft.expected_page_numbers == list(range(1, 31))
    assert report.draft.export_authorized is False


def test_50_pages_keep_rendered_images_bounded_by_worker_count(tmp_path):
    pdf = make_pdf(tmp_path / "synthetic-50.pdf", 50)
    adapter = SyntheticAdapter(delay=lambda page: 0.0005)
    report = run_job(tmp_path, pdf, adapter)
    assert report.status == "completed"
    assert len(adapter.calls) == 50
    assert report.max_inflight_image_count <= 3
    assert report.max_inflight_image_bytes <= 3 * 4096
    assert report.max_parallel_requests <= 3


def test_one_page_failure_preserves_successes_and_full_page_manifest(tmp_path):
    pdf = make_pdf(tmp_path / "synthetic-errors.pdf", 5)
    report = run_job(tmp_path, pdf, SyntheticAdapter(fail_pages={3}))
    assert report.status == "completed_with_errors"
    assert report.pages[2].state == "failed"
    assert report.pages[0].state == report.pages[1].state == "completed"
    assert len(report.draft.pages) == 5
    assert report.draft.pages[2].status == "failed"
    assert report.pages[2].attempts[-1].usage.input_tokens == 10
    assert report.pages[2].attempts[-1].actual_cost_rub == Decimal("0.0044")
    assert report.actual_input_tokens == 50
    assert report.actual_output_tokens == 40
    assert report.pages[0].extraction.raw_model_payload["future_vendor_field"] == SYNTHETIC_RAW


def test_source_change_stops_later_pages_and_preserves_completed_results(tmp_path):
    pdf = make_pdf(tmp_path / "source-changes-mid-run.pdf", 3)
    adapter = SyntheticAdapter()

    def mutate_after_first_page(event):
        if event.page_number == 1 and event.state == "completed":
            with pdf.open("ab") as stream:
                stream.write(b"synthetic source mutation")

    report = run_job(
        tmp_path, pdf, adapter, config=make_config(concurrency=1),
        progress=mutate_after_first_page,
    )

    assert report.status == "completed_with_errors"
    assert report.source_integrity_verified is False
    assert adapter.calls == [1]
    assert report.pages[0].state == "failed"
    assert report.pages[0].extraction is not None
    assert report.pages[0].attempts[-1].usage == TokenUsage(input_tokens=10, output_tokens=8)
    assert report.pages[0].attempts[-1].safe_error_code == "source_changed_during_run"
    assert report.pages[1].state == "failed"
    assert report.pages[1].attempts[-1].safe_error_code == "source_changed_during_run"
    assert report.pages[2].state == "pending"
    assert all(page.status == "failed" for page in report.draft.pages)


def test_final_content_hash_rejects_equal_size_mtime_mutation_after_dispatch(tmp_path):
    pdf = make_pdf(tmp_path / "inflight-source-mutation.pdf", 3)
    adapter = SyntheticAdapter(delay=lambda page: 0.01 if page == 1 else 0.3)
    original_stat = pdf.stat()
    identity = inspect_pdf(pdf, adapter, make_config(), run_id="synthetic-run")
    checkpoint = tmp_path / "inflight-source-checkpoint"
    mutated = False

    async def mutate_after_all_requests_started(event):
        nonlocal mutated
        if event.page_number != 1 or event.state != "completed" or mutated:
            return
        deadline = time.monotonic() + 2
        while len(adapter.calls) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        if len(adapter.calls) == 3:
            content = bytearray(pdf.read_bytes())
            content[-1] ^= 1
            pdf.write_bytes(content)
            os.utime(pdf, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            mutated = True

    report = run_job(
        tmp_path, pdf, adapter, checkpoint=checkpoint,
        progress=mutate_after_all_requests_started,
    )

    assert mutated
    assert adapter.calls == [1, 2, 3]
    assert report.status == "completed_with_errors"
    assert report.source_integrity_verified is False
    assert report.source_integrity_error_code == "source_integrity_mismatch"
    assert all(page.state == "failed" and page.extraction is not None for page in report.pages)
    assert all(page.attempts[-1].usage is not None for page in report.pages)
    assert all(page.status == "failed" for page in report.draft.pages)
    assert report.actual_input_tokens == 30

    recovered_store = CheckpointStore(checkpoint, identity)
    recovered = recovered_store.open()
    recovered_store.close()
    assert [recovered[number].state for number in range(1, 4)] == ["failed"] * 3
    assert all(recovered[number].extraction is not None for number in range(1, 4))


def test_source_change_during_render_prevents_dispatch(tmp_path):
    pdf = make_pdf(tmp_path / "source-changes-during-render.pdf", 2)

    class MutatingRenderer(SyntheticRenderer):
        def render(self, pdf_path, page_number, dpi, config):
            image = super().render(pdf_path, page_number, dpi, config)
            with Path(pdf_path).open("ab") as stream:
                stream.write(b"synthetic render-time mutation")
            return image

    adapter = SyntheticAdapter()
    report = run_job(
        tmp_path, pdf, adapter, config=make_config(concurrency=1),
        renderer=MutatingRenderer(),
    )

    assert report.status == "completed_with_errors"
    assert adapter.calls == []
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[-1].safe_error_code == "source_changed_during_run"
    assert report.pages[1].state == "pending"


def test_source_change_after_rate_gate_prevents_dispatch(tmp_path, monkeypatch):
    from averon_import.services.ai_first import orchestration

    pdf = make_pdf(tmp_path / "source-changes-after-rate-gate.pdf")
    adapter = SyntheticAdapter()
    original_acquire = orchestration._RateGate.acquire
    mutated = False

    async def acquire_then_mutate(gate, *args, **kwargs):
        nonlocal mutated
        acquired = await original_acquire(gate, *args, **kwargs)
        if acquired and not mutated:
            with pdf.open("ab") as stream:
                stream.write(b"synthetic rate-gate mutation")
            mutated = True
        return acquired

    monkeypatch.setattr(orchestration._RateGate, "acquire", acquire_then_mutate)
    report = run_job(tmp_path, pdf, adapter)

    assert report.status == "completed_with_errors"
    assert mutated
    assert adapter.calls == []
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[-1].safe_error_code == "source_changed_during_run"


def test_resume_skips_all_completed_pages_without_repeating_requests(tmp_path):
    pdf = make_pdf(tmp_path / "synthetic-resume.pdf", 4)
    adapter = SyntheticAdapter()
    checkpoint = tmp_path / "resume-checkpoint"
    first = run_job(tmp_path, pdf, adapter, checkpoint=checkpoint)
    original_calls = list(adapter.calls)
    second = run_job(tmp_path, pdf, adapter, checkpoint=checkpoint)
    assert first.status == second.status == "completed"
    assert adapter.calls == original_calls
    assert [page.state for page in second.pages] == ["completed"] * 4


def test_resume_rejects_a_different_pdf_before_any_request(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 2, marker="source-A")
    replacement = make_pdf(tmp_path / "replacement.pdf", 2, marker="source-B")
    adapter, config = SyntheticAdapter(), make_config()
    identity = inspect_pdf(source, adapter, config, run_id="same-run")
    store = CheckpointStore(tmp_path / "identity-checkpoint", identity)
    store.open()
    store.close()
    orchestrator = PageOrchestrator(adapter, SyntheticRenderer(), config, store)
    with pytest.raises(OrchestrationError, match="source_or_configuration_identity_mismatch"):
        asyncio.run(orchestrator.run(replacement))
    assert adapter.calls == []


@pytest.mark.parametrize("changed", ["model", "prompt", "schema", "dpi"])
def test_resume_rejects_model_prompt_schema_or_processing_change(tmp_path, changed):
    pdf = make_pdf(tmp_path / f"fingerprint-{changed}.pdf")
    config, first_adapter = make_config(), SyntheticAdapter()
    store = CheckpointStore(
        tmp_path / f"fingerprint-{changed}",
        inspect_pdf(pdf, first_adapter, config, run_id="same-run"),
    )
    store.open()
    store.close()
    options = {
        "model_version": "synthetic-v1",
        "prompt_version": "prompt-v1",
        "schema_version": "schema-v1",
    }
    if changed in ("model", "prompt", "schema"):
        key = {"model": "model_version", "prompt": "prompt_version", "schema": "schema_version"}[changed]
        options[key] = "changed"
    adapter = SyntheticAdapter(**options)
    second_config = make_config(dpi=144) if changed == "dpi" else config
    identity = inspect_pdf(pdf, adapter, second_config, run_id="same-run")
    with pytest.raises(OrchestrationError, match="checkpoint_identity_mismatch"):
        CheckpointStore(store.directory, identity).open()


def test_unknown_timeout_is_not_automatically_retried_after_resume(tmp_path):
    pdf = make_pdf(tmp_path / "unknown-timeout.pdf", 2)
    adapter, checkpoint = SyntheticAdapter(unknown_once={1}), tmp_path / "unknown-checkpoint"
    first = run_job(tmp_path, pdf, adapter, checkpoint=checkpoint)
    calls_after_first = list(adapter.calls)
    second = run_job(tmp_path, pdf, adapter, checkpoint=checkpoint)
    assert first.pages[0].state == second.pages[0].state == "unknown_outcome"
    assert adapter.calls == calls_after_first


def test_explicit_retry_pages_is_required_to_retry_unknown_outcome(tmp_path):
    pdf = make_pdf(tmp_path / "explicit-retry.pdf")
    adapter, checkpoint = SyntheticAdapter(unknown_once={1}), tmp_path / "explicit-retry-checkpoint"
    assert run_job(tmp_path, pdf, adapter, checkpoint=checkpoint).pages[0].state == "unknown_outcome"
    second = run_job(tmp_path, pdf, adapter, checkpoint=checkpoint, retry_pages={1})
    assert second.pages[0].state == "completed"
    assert adapter.calls == [1, 1]


def test_rpm_and_tpm_gate_request_starts(tmp_path):
    pdf = make_pdf(tmp_path / "rate-limit.pdf", 3)
    adapter = SyntheticAdapter()
    config = make_config(requests_per_minute=1, tokens_per_minute=20)
    report = run_job(tmp_path, pdf, adapter, config=config, rate_window_s=0.05)
    assert report.status == "completed"
    assert len(adapter.started_at) == 3
    assert all(right - left >= 0.04 for left, right in zip(adapter.started_at, adapter.started_at[1:]))


@pytest.mark.parametrize(("rpm", "tpm"), [(1, 100), (60, 20)])
def test_rate_limits_are_restored_across_resume(tmp_path, rpm, tpm):
    pdf = make_pdf(tmp_path / f"rate-resume-{rpm}-{tpm}.pdf", 2)
    adapter = SyntheticAdapter()
    checkpoint = tmp_path / f"rate-resume-checkpoint-{rpm}-{tpm}"
    cancel_event = asyncio.Event()

    def cancel_after_first_page(event):
        if event.page_number == 1 and event.state == "completed":
            cancel_event.set()

    config = make_config(
        concurrency=1, requests_per_minute=rpm, tokens_per_minute=tpm,
    )
    first = run_job(
        tmp_path, pdf, adapter, config=config, checkpoint=checkpoint,
        progress=cancel_after_first_page, cancel_event=cancel_event, rate_window_s=0.12,
    )
    assert first.status == "cancelled"
    assert first.pages[0].attempts[-1].request_started is True
    assert first.pages[0].attempts[-1].dispatched_at is not None
    assert adapter.calls == [1]

    resumed = run_job(
        tmp_path, pdf, adapter, config=config, checkpoint=checkpoint, rate_window_s=0.12,
    )
    assert resumed.status == "completed"
    assert adapter.calls == [1, 2]
    assert adapter.started_at[1] - adapter.started_at[0] >= 0.10


def test_unknown_outcome_reservation_is_restored_before_explicit_retry(tmp_path):
    pdf = make_pdf(tmp_path / "unknown-rate-resume.pdf", 2)
    adapter = SyntheticAdapter(unknown_once={1})
    checkpoint = tmp_path / "unknown-rate-resume-checkpoint"
    cancel_event = asyncio.Event()

    def cancel_after_unknown(event):
        if event.page_number == 1 and event.state == "unknown_outcome":
            cancel_event.set()

    config = make_config(concurrency=1, requests_per_minute=1, tokens_per_minute=20)
    first = run_job(
        tmp_path, pdf, adapter, config=config, checkpoint=checkpoint,
        progress=cancel_after_unknown, cancel_event=cancel_event, rate_window_s=0.12,
    )
    assert first.status == "cancelled"
    assert first.pages[0].state == "unknown_outcome"
    assert first.pages[0].attempts[-1].request_started is True
    assert adapter.calls == [1]

    resumed = run_job(
        tmp_path, pdf, adapter, config=config, checkpoint=checkpoint,
        retry_pages={1}, rate_window_s=0.12,
    )
    assert resumed.status == "completed"
    assert adapter.calls == [1, 1, 2]
    assert adapter.started_at[1] - adapter.started_at[0] >= 0.10


def test_budget_exhaustion_stops_new_requests_and_keeps_page_manifest(tmp_path):
    pdf = make_pdf(tmp_path / "budget.pdf", 10)
    adapter = SyntheticAdapter()
    config = make_config(max_budget_rub=Decimal("0.0051"))
    report = run_job(tmp_path, pdf, adapter, config=config, rate_window_s=0.05)
    assert report.status == "budget_exhausted"
    assert len(adapter.calls) == 1
    assert len(report.pages) == 10
    assert any(page.state == "pending" for page in report.pages)
    assert len(report.draft.pages) == 10


def test_user_cancellation_marks_dispatched_request_unknown(tmp_path):
    pdf = make_pdf(tmp_path / "cancel.pdf", 5)
    cancel_event = asyncio.Event()
    adapter = SyntheticAdapter(cancel_event=cancel_event)
    report = run_job(tmp_path, pdf, adapter, config=make_config(concurrency=1), cancel_event=cancel_event)
    assert report.status == "cancelled"
    assert report.pages[0].state == "unknown_outcome"
    assert report.pages[0].attempts[-1].safe_error_code == "cancelled_after_dispatch"
    assert len(adapter.calls) == 1


def test_checkpoint_running_attempt_becomes_unknown_after_restart(tmp_path):
    pdf = make_pdf(tmp_path / "restart-running.pdf")
    adapter, config = SyntheticAdapter(), make_config()
    identity = inspect_pdf(pdf, adapter, config, run_id="restart-run")
    store = CheckpointStore(tmp_path / "restart-checkpoint", identity)
    store.open()
    record = PageRunRecord(
        page_number=1, state="running", updated_at="2026-01-01T00:00:00+00:00",
        attempts=[PageAttempt(
            state="running", started_at="2026-01-01T00:00:00+00:00",
            request_started=True, reserved_input_tokens=10, reserved_output_tokens=10,
            reserved_cost_rub=Decimal("0.005"),
        )],
    )
    asyncio.run(store.write(record))
    store.close()
    reopened = CheckpointStore(store.directory, identity)
    recovered = reopened.open()
    reopened.close()
    assert recovered[1].state == "unknown_outcome"
    assert recovered[1].attempts[-1].safe_error_code == "process_restarted_during_request"


def test_retry_write_exception_releases_checkpoint_lock_before_workers(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "retry-write-failure.pdf")
    adapter, config = SyntheticAdapter(), make_config()
    identity = inspect_pdf(pdf, adapter, config, run_id="retry-lock-run")
    checkpoint_dir = tmp_path / "retry-write-checkpoint"
    seed = CheckpointStore(checkpoint_dir, identity)
    seed.open()
    asyncio.run(seed.write(PageRunRecord(
        page_number=1, state="failed", updated_at="2026-01-01T00:00:01+00:00",
        attempts=[PageAttempt(
            state="failed", started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00", safe_error_code="synthetic_failure",
        )],
    )))
    seed.close()

    checkpoint = CheckpointStore(checkpoint_dir, identity)

    async def fail_write(_record):
        raise OSError("synthetic checkpoint failure")

    monkeypatch.setattr(checkpoint, "write", fail_write)
    orchestrator = PageOrchestrator(adapter, SyntheticRenderer(), config, checkpoint)
    with pytest.raises(OSError, match="synthetic checkpoint failure"):
        asyncio.run(orchestrator.run(pdf, retry_pages={1}))
    assert adapter.calls == []

    reopened = CheckpointStore(checkpoint_dir, identity)
    recovered = reopened.open()
    reopened.close()
    assert recovered[1].state == "failed"


def test_atomic_checkpoint_replace_keeps_old_file_and_cleans_temp(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "atomic.pdf")
    adapter, config = SyntheticAdapter(), make_config()
    identity = inspect_pdf(pdf, adapter, config, run_id="atomic-run")
    store = CheckpointStore(tmp_path / "atomic-checkpoint", identity)
    store.open()
    target = store.directory / "atomic.json"
    target.write_text('{"old":true}', encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OrchestrationError, match="checkpoint_write_failed"):
        store._atomic_json(target, {"new": True})
    store.close()
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert list(store.directory.glob(".atomic.json.*.tmp")) == []


def test_logs_do_not_contain_raw_transcription_or_image_bytes(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "safe-logs.pdf")
    adapter = SyntheticAdapter()
    with caplog.at_level(logging.INFO, logger="averon_import.services.ai_first.orchestration"):
        report = run_job(tmp_path, pdf, adapter)
    assert report.pages[0].extraction.raw_model_payload["future_vendor_field"] == SYNTHETIC_RAW
    assert SYNTHETIC_RAW not in caplog.text
    assert "IIIIIIII" not in caplog.text


def test_v2a_draft_preserves_unknown_fields_without_export_authorization(tmp_path):
    pdf = make_pdf(tmp_path / "draft.pdf", 2)
    report = run_job(tmp_path, pdf, SyntheticAdapter())
    assert report.draft.expected_page_numbers == [1, 2]
    assert report.draft.extraction_is_verified is False
    assert report.draft.export_authorized is False
    assert [page.raw_model_payload["future_vendor_field"] for page in report.draft.pages] == [SYNTHETIC_RAW] * 2


def test_invalid_concurrency_and_token_rate_limits_are_rejected():
    with pytest.raises(ValidationError):
        make_config(concurrency=9)
    with pytest.raises(ValidationError):
        make_config(tokens_per_minute=0)


def test_default_configuration_has_three_workers_and_finite_budget_controls():
    config = make_config()
    assert config.concurrency == 3
    assert config.requests_per_minute > 0 and config.tokens_per_minute > 0
    assert config.max_budget_rub == Decimal("100")


@pytest.mark.parametrize("parameters", [
    {"transport": {"nested": [{"api_key": "synthetic-secret"}]}},
    {"transport": [{"authorization": "synthetic-secret"}]},
    {"endpoint": "https://user:synthetic-secret@example.test/v1"},
    {"endpoint": "https://example.test/v1?access_token=synthetic-secret"},
    {"header": "Bearer syntheticcredentialvalue"},
    {"opaque_value": "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"},
])
def test_adapter_descriptor_rejects_nested_or_credential_like_values(parameters):
    with pytest.raises(ValidationError):
        AdapterDescriptor(
            provider_id="mock-vision", model_id="synthetic-model",
            model_version="synthetic-v1", prompt_version="prompt-v1",
            schema_version="schema-v1", parameters=parameters,
        )


def test_usage_and_cost_remain_unknown_when_adapter_has_no_usage(tmp_path):
    pdf = make_pdf(tmp_path / "missing-usage.pdf")
    report = run_job(tmp_path, pdf, SyntheticAdapter(usage_missing=True))
    assert report.status == "completed"
    assert report.pages[0].attempts[-1].usage is None
    assert report.actual_input_tokens is None
    assert report.actual_output_tokens is None
    assert report.actual_reasoning_tokens is None
    assert report.actual_cost_rub is None
    assert report.reserved_unknown_cost_rub > 0


def test_cancel_before_dispatch_sends_no_request_and_releases_reservation(tmp_path):
    pdf = make_pdf(tmp_path / "cancel-before-dispatch.pdf")
    cancel_event = asyncio.Event()

    async def cancel_on_running(event):
        if event.state == "running":
            cancel_event.set()

    adapter = SyntheticAdapter()
    report = run_job(
        tmp_path, pdf, adapter, config=make_config(concurrency=1),
        progress=cancel_on_running, cancel_event=cancel_event,
    )
    assert report.status == "cancelled"
    assert adapter.calls == []
    assert report.pages[0].state == "pending"
    assert report.pages[0].attempts[-1].request_started is False
    assert report.actual_cost_rub is None
    assert report.reserved_unknown_cost_rub == 0


def test_real_renderer_produces_a_bounded_png_for_one_requested_page(tmp_path):
    pdf = make_pdf(tmp_path / "renderer.pdf", 2)
    image = FitZPageRenderer().render(pdf, 2, 72, make_config())
    assert image.page_number == 2
    assert image.content_type == "image/png"
    assert image.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.width * image.height <= make_config().max_image_pixels
    with pytest.raises(OrchestrationError, match="image_pixel_limit"):
        FitZPageRenderer().render(pdf, 1, 72, make_config(max_image_pixels=1))


def test_oversized_page_is_rejected_before_pixmap_allocation(monkeypatch):
    from types import SimpleNamespace

    from averon_import.services.ai_first import orchestration

    get_pixmap_calls = 0

    class OversizedPage:
        rect = SimpleNamespace(width=1_000_000, height=1_000_000)

        def get_pixmap(self, **_kwargs):
            nonlocal get_pixmap_calls
            get_pixmap_calls += 1
            raise AssertionError("pixmap allocation must not occur")

    class SyntheticDocument:
        page_count = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def load_page(self, _page_number):
            return OversizedPage()

    monkeypatch.setattr(orchestration.fitz, "open", lambda _path: SyntheticDocument())
    with pytest.raises(OrchestrationError, match="image_pixel_limit"):
        FitZPageRenderer().render("synthetic-oversized.pdf", 1, 150, make_config())
    assert get_pixmap_calls == 0


def test_cancel_after_partial_completion_preserves_and_resumes_successful_pages(tmp_path):
    pdf = make_pdf(tmp_path / "partial-cancel.pdf", 6)
    checkpoint = tmp_path / "partial-cancel-checkpoint"
    cancel_event = asyncio.Event()
    completed = 0

    def cancel_after_three(event):
        nonlocal completed
        if event.state == "completed":
            completed += 1
            if completed == 3:
                cancel_event.set()

    adapter = SyntheticAdapter()
    config = make_config(concurrency=1)
    first = run_job(
        tmp_path, pdf, adapter, config=config, checkpoint=checkpoint,
        progress=cancel_after_three, cancel_event=cancel_event,
    )
    calls_after_cancel = list(adapter.calls)
    second = run_job(tmp_path, pdf, adapter, config=config, checkpoint=checkpoint)

    assert first.status == "cancelled"
    assert [page.state for page in first.pages[:3]] == ["completed"] * 3
    assert all(page.state == "pending" for page in first.pages[3:])
    assert second.status == "completed"
    assert adapter.calls == calls_after_cancel + [4, 5, 6]

def test_progress_callback_failure_does_not_abort_page_processing(tmp_path, caplog):
    pdf = make_pdf(tmp_path / "progress-error.pdf", 3)
    adapter = SyntheticAdapter()

    def broken_callback(event):
        raise RuntimeError("SYNTHETIC_CALLBACK_DETAIL")

    with caplog.at_level(logging.WARNING, logger="averon_import.services.ai_first.orchestration"):
        report = run_job(tmp_path, pdf, adapter, progress=broken_callback)
    assert report.status == "completed"
    assert len(adapter.calls) == 3
    assert "SYNTHETIC_CALLBACK_DETAIL" not in caplog.text


def test_invalid_adapter_page_result_isolated_to_its_page(tmp_path):
    class MalformedAdapter(SyntheticAdapter):
        async def extract(self, request):
            self.calls.append(request.page_number)
            if request.page_number == 2:
                return object()
            return await super().extract(request)

    pdf = make_pdf(tmp_path / "adapter-result-error.pdf", 3)
    adapter = MalformedAdapter()
    report = run_job(tmp_path, pdf, adapter)
    assert report.status == "completed_with_errors"
    assert [page.state for page in report.pages] == ["completed", "failed", "completed"]
    assert report.pages[1].attempts[-1].safe_error_code == "adapter_response_invalid"
