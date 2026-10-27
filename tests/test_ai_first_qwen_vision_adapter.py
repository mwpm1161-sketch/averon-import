from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import logging
import re
import threading
import time
from types import SimpleNamespace

import fitz
import pytest

from averon_import.services.ai_first.assembly import assemble_document_draft
from averon_import.services.ai_first.orchestration import (
    CheckpointStore, FitZPageRenderer, OrchestrationConfig, OrchestrationError, PageOrchestrator,
    TokenPricing, _fingerprint, inspect_pdf,
)
from averon_import.services.ai_first.qwen_vision_adapter import (
    QwenVisionAdapter, QwenVisionQuotaRegistry,
)
from averon_import.services.ai_first.review import (
    AIReviewError, assert_review_ready_for_export, new_review_state,
)
from averon_import.services.ocr.qwen_vision_client import (
    QwenVisionClient, QwenVisionConfig, VisionHttpResponse, VisionLimits, _HttpsTransport,
)
from averon_import.services.secrets import MemorySecretStore, YANDEX_AI_API_KEY
from averon_import.services.ocr import qwen_vision_client as qwen_vision_client_module


SYNTHETIC_KEY = "synthetic-qwen-secret-never-log"
SYNTHETIC_TEXT = "SYNTHETIC OCR CONTENT FOR TESTS ONLY"


def _cell(column: int, text: str, **changes):
    value = {
        "column_index": column,
        "column_span": 1,
        "row_span": 1,
        "raw_text": text,
        "alternatives": [],
        "handwritten_replacements": [],
        "unreadable": False,
        "struck_through": False,
        "handwritten": False,
        "bbox_candidate": None,
    }
    value.update(changes)
    return value


def _row(cells, **changes):
    value = {
        "kind": "header",
        "logical_row_index": 0,
        "continuation_of": None,
        "continues_previous_page": False,
        "struck_through": False,
        "handwritten": False,
        "handwritten_replacements": [],
        "bbox_candidate": None,
        "cells": cells,
    }
    value.update(changes)
    return value


def _vision_page(number: int, *, headers=None, rows=None, tables=None, unreadable=None, marks=None):
    if tables is None:
        if headers is None:
            headers = ["Поз.", "Наименование", "Ед. изм.", "Количество"]
        if rows is None:
            rows = [_row([
                _cell(0, "1"), _cell(1, SYNTHETIC_TEXT), _cell(2, "шт"), _cell(3, "2"),
            ])]
        tables = [{
            "headers": headers,
            "rows": rows,
            "continues_previous_page": False,
            "bbox_candidate": None,
        }]
    return {
        "page": number,
        "tables": tables,
        "unreadable_fragments": unreadable or [],
        "handwritten_replacements": marks or [],
    }


def _response(
    page_number: int, *, payload=None, status=200, finish_reason="stop", content=None,
    include_error_usage=False,
):
    if content is None:
        content = json.dumps(payload or _vision_page(page_number), ensure_ascii=False)
    outer = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "total_tokens": 140,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }
    body = (
        json.dumps({"usage": outer["usage"]}, ensure_ascii=False).encode("utf-8")
        if status >= 400 and include_error_usage
        else b"" if status >= 400
        else json.dumps(outer, ensure_ascii=False).encode("utf-8")
    )
    return VisionHttpResponse(status, body, {})


class FakeTransport:
    def __init__(self, handler=None, *, delay_s=0.0, request_barrier=None):
        self.handler = handler
        self.delay_s = delay_s
        self.request_barrier = request_barrier
        self.calls = []
        self.started_pages = []
        self.started_at = []
        self.active = 0
        self.max_active = 0
        self._counts = {}
        self._lock = threading.Lock()
        self.started_event = threading.Event()

    def post(self, url, **kwargs):
        payload = json.loads(kwargs["body"])
        user_message = next(message for message in payload["messages"] if message["role"] == "user")
        text = user_message["content"][0]["text"]
        page_number = int(re.search(r"(\d+)$", text).group(1))
        with self._lock:
            self._counts[page_number] = self._counts.get(page_number, 0) + 1
            call_number = self._counts[page_number]
            self.calls.append({"url": url, "payload": payload})
            self.started_pages.append(page_number)
            self.started_at.append(time.monotonic())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started_event.set()
        try:
            if self.request_barrier is not None:
                self.request_barrier.wait(timeout=3)
            if self.delay_s:
                time.sleep(self.delay_s)
            if self.handler is None:
                return _response(page_number)
            return self.handler(page_number, call_number, kwargs)
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture(autouse=True)
def no_live_transport(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("AI-first adapter tests must not use the live HTTPS transport")
    monkeypatch.setattr(_HttpsTransport, "post", denied)


def _limits(**changes):
    defaults = {
        "max_image_bytes": 2_000_000,
        "max_image_pixels": 2_000_000,
        "max_request_bytes": 3_000_000,
        "max_response_bytes": 1_000_000,
        "max_output_tokens": 128,
        "request_timeout_s": 2.0,
        "total_timeout_s": 4.0,
        "dpi": 72,
        "max_pages": 100,
        "max_requests": 100,
    }
    defaults.update(changes)
    return replace(VisionLimits(), **defaults)


def _adapter(
    transport, *, registry=None, rpm=1000, tpm=100000, input_bound=256,
    api_key=SYNTHETIC_KEY, limits=None, model="gpt://synthetic-folder/qwen3.6-35b-a3b",
    base_url="https://ai.synthetic.invalid/v1", folder_id="synthetic-folder", user_context=None,
):
    config = QwenVisionConfig(
        base_url,
        model,
        api_key,
        folder_id=folder_id,
        limits=limits or _limits(),
    )
    return QwenVisionAdapter(
        QwenVisionClient(config, transport=transport),
        quota_registry=registry or QwenVisionQuotaRegistry(),
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        input_token_upper_bound=input_bound,
        pricing_profile_id="synthetic-tariff-v1",
        user_context=user_context,
    )


def _orchestration_config(*, rpm=1000, tpm=100000, budget=Decimal("100"), concurrency=3):
    return OrchestrationConfig(
        concurrency=concurrency,
        dpi=72,
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
        max_budget_rub=budget,
        pricing=TokenPricing(
            profile_id="synthetic-tariff-v1",
            input_rub_per_1k=Decimal("0.20"),
            cached_input_rub_per_1k=Decimal("0.05"),
            output_rub_per_1k=Decimal("0.30"),
        ),
        max_image_bytes=2_000_000,
        max_image_pixels=2_000_000,
    )


def _make_pdf(path, pages=1):
    with fitz.open() as document:
        for page_number in range(1, pages + 1):
            page = document.new_page(width=72, height=72)
            page.insert_text((5, 20), f"synthetic page {page_number}")
        document.save(path)
    return path


def _make_orchestrator(pdf, adapter, config, checkpoint_dir, *, progress=None, rate_window_s=0.002):
    identity = inspect_pdf(pdf, adapter, config, run_id="qwen-synthetic-run")
    return PageOrchestrator(
        adapter,
        FitZPageRenderer(),
        config,
        CheckpointStore(checkpoint_dir, identity),
        progress=progress,
        rate_window_s=rate_window_s,
    )


def test_thirty_page_pdf_runs_offline_at_parallelism_three_and_keeps_order(tmp_path):
    pdf = _make_pdf(tmp_path / "synthetic-30.pdf", 30)
    transport = FakeTransport(request_barrier=threading.Barrier(3))
    adapter = _adapter(transport)
    config = _orchestration_config()
    report = asyncio.run(_make_orchestrator(pdf, adapter, config, tmp_path / "checkpoint").run(pdf))

    assert report.status == "completed"
    assert report.expected_page_count == 30
    assert [page.page_number for page in report.pages] == list(range(1, 31))
    assert [page.page_number for page in report.draft.pages] == list(range(1, 31))
    assert report.max_parallel_requests == transport.max_active == 3
    assert len(transport.calls) == 30
    assert report.draft.export_authorized is False


def test_client_request_cap_counts_resume_history_and_never_sends_past_limit(tmp_path):
    pdf = _make_pdf(tmp_path / "request-cap.pdf", 4)
    transport = FakeTransport()
    adapter = _adapter(transport, limits=_limits(max_requests=2))
    checkpoint = tmp_path / "request-cap-checkpoint"
    config = _orchestration_config()

    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    resumed_adapter = _adapter(transport, limits=_limits(max_requests=2))
    resumed = asyncio.run(_make_orchestrator(pdf, resumed_adapter, config, checkpoint).run(pdf))

    assert first.status == resumed.status == "completed_with_errors"
    assert len(transport.calls) == 2
    assert sum(page.state == "completed" for page in resumed.pages) == 2
    assert all(
        not page.attempts or page.attempts[-1].request_started is False
        for page in resumed.pages if page.state != "completed"
    )


def test_client_page_cap_refuses_oversized_document_before_http(tmp_path):
    pdf = _make_pdf(tmp_path / "page-cap.pdf", 3)
    transport = FakeTransport()
    adapter = _adapter(transport, limits=_limits(max_pages=2))
    orchestrator = _make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "page-cap-checkpoint",
    )

    with pytest.raises(OrchestrationError, match="adapter_page_limit_exceeded"):
        asyncio.run(orchestrator.run(pdf))
    assert transport.calls == []


def test_visionpage_mapping_preserves_all_tables_unknown_columns_and_untrusted_roles(tmp_path):
    source = _vision_page(1, tables=[
        {
            "headers": ["Поз.", "Неизвестный столбец", "Наименование"],
            "rows": [
                _row([
                    _cell(0, str(index)), _cell(1, f"NX-{index:02d}"),
                    _cell(2, f"{SYNTHETIC_TEXT} {index}"),
                ], kind="header", logical_row_index=index - 1)
                for index in range(1, 4)
            ],
            "continues_previous_page": True,
            "bbox_candidate": None,
        },
        {
            "headers": ["Примечание"],
            "rows": [],
            "continues_previous_page": False,
            "bbox_candidate": None,
        },
    ])
    transport = FakeTransport(lambda _page, _call, _kw: _response(1, payload=source))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "mapping.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "mapping-checkpoint",
    ).run(pdf))
    page = report.draft.pages[0]

    assert len(page.tables) == 2
    assert page.tables[0].columns[1].raw_header == "Неизвестный столбец"
    row = page.tables[0].rows[0]
    assert [cell.raw_value for cell in row.raw_cells] == ["1", "NX-01", f"{SYNTHETIC_TEXT} 1"]
    assert len(page.tables[0].rows) == 3
    assert [row.model_kind for row in page.tables[0].rows] == ["header"] * 3
    assert all(row.server_role_candidate is None and row.role_verified is False for row in page.tables[0].rows)
    assert page.tables[0].continues_previous_page is True
    assert page.raw_model_payload == source
    assert "requests_per_minute" not in adapter.descriptor.parameters
    assert "tokens_per_minute" not in adapter.descriptor.parameters
    assert adapter.descriptor.parameters["pricing_profile_id"] == "synthetic-tariff-v1"


def test_handwriting_strikethrough_and_unreadable_content_are_preserved_as_incomplete(tmp_path):
    replacement = {"text": "8", "bbox_candidate": None}
    source = _vision_page(1, rows=[_row([
        _cell(0, "1", struck_through=True, handwritten_replacements=[replacement]),
        _cell(1, SYNTHETIC_TEXT, handwritten=True),
        _cell(2, "шт"),
        _cell(3, "3", unreadable=True),
    ], struck_through=True, handwritten_replacements=[replacement])], unreadable=["unreadable synthetic fragment"])
    transport = FakeTransport(lambda _page, _call, _kw: _response(1, payload=source))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "uncertain.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "uncertain-checkpoint",
    ).run(pdf))

    extraction = report.pages[0].extraction
    assert report.pages[0].state == "incomplete"
    assert report.pages[0].attempts[0].state == "incomplete"
    assert extraction is not None and extraction.status == "incomplete"
    row = extraction.tables[0].rows[0]
    assert row.struck_through is True
    assert row.handwritten_replacements[0].text == "8"
    assert row.raw_cells[0].struck_through is True
    assert row.raw_cells[0].handwritten_replacements[0].text == "8"
    assert row.raw_cells[3].unreadable is True
    assert extraction.unreadable_fragments == source["unreadable_fragments"]
    assert report.draft.pages[0].status == "incomplete"
    with pytest.raises(AIReviewError, match="page_processing_incomplete"):
        assert_review_ready_for_export(report.draft, new_review_state(report.draft))


def test_empty_tables_and_unknown_headers_are_not_dropped(tmp_path):
    source = _vision_page(1, tables=[{
        "headers": ["Произвольный столбец", "Ещё поле"],
        "rows": [], "continues_previous_page": False, "bbox_candidate": None,
    }])
    transport = FakeTransport(lambda _page, _call, _kw: _response(1, payload=source))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "empty-table.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "empty-table-checkpoint",
    ).run(pdf))
    assert report.status == "completed"
    assert report.pages[0].state == "completed"
    assert report.draft.pages[0].tables[0].columns[0].raw_header == "Произвольный столбец"
    assert report.draft.pages[0].tables[0].rows == []

    empty = _vision_page(1, tables=[])
    empty_transport = FakeTransport(lambda _page, _call, _kw: _response(1, payload=empty))
    empty_adapter = _adapter(empty_transport)
    empty_pdf = _make_pdf(tmp_path / "empty-page.pdf")
    empty_report = asyncio.run(_make_orchestrator(
        empty_pdf, empty_adapter, _orchestration_config(), tmp_path / "empty-page-checkpoint",
    ).run(empty_pdf))
    assert empty_report.draft.pages[0].status == "no_specification"
    assert empty_report.draft.pages[0].tables == []


def test_incomplete_response_is_retryable_only_when_explicitly_selected(tmp_path):
    transport = FakeTransport(lambda page, call, _kw: (
        _response(page, finish_reason="length", content='{"page":')
        if call == 1 else _response(page)
    ))
    adapter = _adapter(transport)
    config = _orchestration_config()
    pdf = _make_pdf(tmp_path / "incomplete-retry.pdf")
    checkpoint = tmp_path / "incomplete-retry-checkpoint"
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    second = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert first.pages[0].state == "incomplete"
    assert first.pages[0].attempts[0].usage.input_tokens == 100
    assert second.pages[0].state == "incomplete"
    assert len(transport.calls) == 1

    retried = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf, retry_pages={1}))
    assert retried.status == "completed"
    assert retried.pages[0].state == "completed"
    assert [attempt.state for attempt in retried.pages[0].attempts] == ["incomplete", "completed"]
    assert len(transport.calls) == 2
    assert retried.actual_input_tokens == 200


def test_transport_loss_after_send_is_unknown_and_not_retried_on_resume(tmp_path):
    transport = FakeTransport(lambda _page, _call, _kw: OSError(f"{SYNTHETIC_KEY} {SYNTHETIC_TEXT}"))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "lost-connection.pdf")
    checkpoint = tmp_path / "lost-connection-checkpoint"
    config = _orchestration_config()
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    resumed = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert first.pages[0].state == resumed.pages[0].state == "unknown_outcome"
    assert first.pages[0].attempts[0].request_started is True
    assert first.pages[0].attempts[0].usage is None
    assert resumed.actual_cost_rub is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status", "safe_code", "expected_state"),
    [
        (429, "http_429", "failed"),
        (500, "http_500", "unknown_outcome"),
        (502, "http_502", "unknown_outcome"),
        (503, "http_503", "unknown_outcome"),
        (504, "http_504", "unknown_outcome"),
    ],
)
def test_http_errors_are_not_automatically_retried_on_resume(
    tmp_path, status, safe_code, expected_state,
):
    transport = FakeTransport(lambda _page, _call, _kw: _response(
        1, status=status, include_error_usage=status == 500,
    ))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / f"http-{status}.pdf")
    checkpoint = tmp_path / f"http-{status}-checkpoint"
    config = _orchestration_config()
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    resumed = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert first.pages[0].state == resumed.pages[0].state == expected_state
    assert first.pages[0].attempts[0].safe_error_code == safe_code
    assert first.pages[0].attempts[0].request_started is True
    assert len(transport.calls) == 1
    if status == 500:
        attempt = resumed.pages[0].attempts[0]
        assert attempt.usage.input_tokens == 100
        assert attempt.usage.output_tokens == 40
        assert attempt.actual_cost_rub is not None
        assert resumed.actual_input_tokens == 100
    elif status >= 500:
        assert resumed.actual_cost_rub is None
        assert resumed.reserved_unknown_cost_rub > 0


def test_unknown_http_5xx_is_retried_only_after_explicit_page_selection(tmp_path):
    transport = FakeTransport(lambda page, call, _kw: (
        _response(page, status=503) if call == 1 else _response(page)
    ))
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "explicit-5xx-retry.pdf")
    checkpoint = tmp_path / "explicit-5xx-retry-checkpoint"
    config = _orchestration_config()

    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    resumed = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert first.pages[0].state == resumed.pages[0].state == "unknown_outcome"
    assert len(transport.calls) == 1
    assert resumed.reserved_unknown_cost_rub > 0
    assert resumed.actual_cost_rub is None

    retried = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(
        pdf, retry_pages={1},
    ))
    assert retried.pages[0].state == "completed"
    assert [attempt.state for attempt in retried.pages[0].attempts] == [
        "unknown_outcome", "completed",
    ]
    assert len(transport.calls) == 2
    assert retried.reserved_unknown_cost_rub > 0


def test_multiple_inflight_requests_are_cancelled_without_orphan_threads(tmp_path):
    transport = None

    def wait_for_cancel(_page, _call, kwargs):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cancel = kwargs.get("cancel")
            if cancel is not None and cancel.is_set():
                raise TimeoutError("synthetic cancellation")
            time.sleep(0.002)
        raise TimeoutError("synthetic timeout")

    transport = FakeTransport(wait_for_cancel)
    adapter = _adapter(transport)
    config = _orchestration_config(concurrency=3)
    pdf = _make_pdf(tmp_path / "cancel-three.pdf", 6)
    cancel_event = asyncio.Event()
    orchestrator = _make_orchestrator(
        pdf, adapter, config, tmp_path / "cancel-checkpoint",
    )

    async def cancel_after_three_posts():
        await asyncio.to_thread(transport.started_event.wait, 2.0)
        deadline = time.monotonic() + 2.0
        while transport.active < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        cancel_event.set()

    async def run():
        canceller = asyncio.create_task(cancel_after_three_posts())
        report = await orchestrator.run(pdf, cancel_event=cancel_event)
        await canceller
        return report

    report = asyncio.run(run())
    assert report.status == "cancelled"
    assert len(transport.calls) == 3
    assert report.max_parallel_requests == 3
    assert all(report.pages[index].state == "unknown_outcome" for index in range(3))
    assert transport.active == 0


def test_resume_keeps_usage_history_for_successful_pages_and_retry_attempts(tmp_path):
    def response_by_call(page, call, _kw):
        if page == 2 and call == 1:
            return _response(page, finish_reason="length", content='{"page":')
        return _response(page)

    transport = FakeTransport(response_by_call)
    adapter = _adapter(transport)
    config = _orchestration_config()
    pdf = _make_pdf(tmp_path / "usage-resume.pdf", 2)
    checkpoint = tmp_path / "usage-resume-checkpoint"
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert [page.state for page in first.pages] == ["completed", "incomplete"]
    retried = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf, retry_pages={2}))
    assert transport.started_pages.count(1) == 1
    assert transport.started_pages.count(2) == 2
    assert [attempt.state for attempt in retried.pages[1].attempts] == ["incomplete", "completed"]
    assert retried.pages[1].attempts[0].usage.input_tokens == 100
    assert retried.pages[1].attempts[1].usage.reasoning_tokens == 5
    assert retried.actual_input_tokens == 300
    assert retried.actual_output_tokens == 120


def test_provider_quota_is_shared_across_simultaneous_document_jobs(tmp_path):
    # Keep both rolling reservations live while PDF rendering and local
    # request preparation run, so this asserts the shared credential budget
    # rather than timing a tiny synthetic window against preprocessing.
    registry = QwenVisionQuotaRegistry(window_s=0.5)
    transport_a, transport_b = FakeTransport(), FakeTransport()
    adapter_a = _adapter(transport_a, registry=registry, rpm=2, tpm=416, input_bound=80)
    adapter_b = _adapter(transport_b, registry=registry, rpm=2, tpm=416, input_bound=80)
    config = _orchestration_config(rpm=2, tpm=416)
    pdf_a = _make_pdf(tmp_path / "quota-a.pdf", 2)
    pdf_b = _make_pdf(tmp_path / "quota-b.pdf", 2)
    orchestrator_a = _make_orchestrator(pdf_a, adapter_a, config, tmp_path / "quota-a-checkpoint")
    orchestrator_b = _make_orchestrator(pdf_b, adapter_b, config, tmp_path / "quota-b-checkpoint")

    async def run_both():
        return await asyncio.gather(orchestrator_a.run(pdf_a), orchestrator_b.run(pdf_b))

    reports = asyncio.run(run_both())
    timestamps = sorted(transport_a.started_at + transport_b.started_at)
    assert all(report.status == "completed" for report in reports)
    assert len(timestamps) == 4
    assert max(
        sum(start <= stamp < start + 0.45 for stamp in timestamps)
        for start in timestamps
    ) <= 2


def test_budget_exhaustion_happens_before_any_vision_request(tmp_path):
    transport = FakeTransport()
    adapter = _adapter(transport)
    config = _orchestration_config(budget=Decimal("0.001"))
    pdf = _make_pdf(tmp_path / "budget.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, config, tmp_path / "budget-checkpoint",
    ).run(pdf))
    assert report.status == "budget_exhausted"
    assert transport.calls == []


def test_missing_safe_input_estimate_refuses_before_http(tmp_path):
    transport = FakeTransport()
    adapter = _adapter(transport, input_bound=None)
    pdf = _make_pdf(tmp_path / "estimate-required.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "estimate-checkpoint",
    ).run(pdf))
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[0].safe_error_code == "request_estimate_invalid"
    assert transport.calls == []


def test_usage_mapping_keeps_valid_counts_when_optional_details_are_inconsistent():
    usage = QwenVisionAdapter._usage({
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 120},
        "completion_tokens_details": {"reasoning_tokens": 41},
    })
    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.cached_input_tokens is None
    assert usage.reasoning_tokens is None


def test_adding_optional_pricing_profile_preserves_legacy_resume_fingerprint():
    config = _orchestration_config()
    pricing = config.pricing.model_copy(update={"profile_id": None})
    config = config.model_copy(update={"pricing": pricing})
    legacy_pricing = pricing.model_dump(mode="json")
    legacy_pricing.pop("profile_id", None)
    assert config.processing_fingerprint() == _fingerprint({
        "dpi": config.dpi,
        "max_image_bytes": config.max_image_bytes,
        "max_image_pixels": config.max_image_pixels,
        "pricing": legacy_pricing,
    })


def test_checkpoint_descriptor_binds_full_model_uri_and_effective_endpoint(tmp_path):
    transport = FakeTransport()
    model_a = _adapter(
        transport, model="gpt://private-folder-a/shared-model",
        folder_id="private-folder-a",
    )
    model_b = _adapter(
        transport, model="gpt://private-folder-b/shared-model",
        folder_id="private-folder-b",
    )
    endpoint_b = _adapter(
        transport, model="gpt://private-folder-a/shared-model",
        folder_id="private-folder-a", base_url="https://other.synthetic.invalid/v1",
    )

    assert model_a.descriptor.model_version == model_b.descriptor.model_version == "shared-model"
    assert model_a.descriptor.fingerprint != model_b.descriptor.fingerprint
    assert model_a.descriptor.fingerprint != endpoint_b.descriptor.fingerprint
    public_descriptor = json.dumps(model_a.descriptor.model_dump())
    assert "private-folder-a" not in public_descriptor
    assert "ai.synthetic.invalid" not in public_descriptor
    assert model_a.descriptor.parameters["model_deployment_sha256"] != model_b.descriptor.parameters[
        "model_deployment_sha256"
    ]
    assert model_a.descriptor.parameters["effective_endpoint_sha256"] != endpoint_b.descriptor.parameters[
        "effective_endpoint_sha256"
    ]

    pdf = _make_pdf(tmp_path / "deployment-identity.pdf")
    config = _orchestration_config()
    checkpoint = tmp_path / "deployment-identity-checkpoint"
    first = asyncio.run(_make_orchestrator(pdf, model_a, config, checkpoint).run(pdf))
    assert first.status == "completed"
    for changed_adapter in (model_b, endpoint_b):
        with pytest.raises(OrchestrationError, match="checkpoint_identity_mismatch"):
            asyncio.run(_make_orchestrator(pdf, changed_adapter, config, checkpoint).run(pdf))
    assert len(transport.calls) == 1


def test_private_credential_scope_invalidates_checkpoint_without_public_identifier(tmp_path):
    pdf = _make_pdf(tmp_path / "credential-scope.pdf")
    config = _orchestration_config()
    first_adapter = _adapter(FakeTransport(), api_key=SYNTHETIC_KEY)
    same_adapter = _adapter(FakeTransport(), api_key=SYNTHETIC_KEY)
    rotated_adapter = _adapter(FakeTransport(), api_key="another-synthetic-key")
    first_identity = inspect_pdf(pdf, first_adapter, config, run_id="same-synthetic-run")
    same_identity = inspect_pdf(pdf, same_adapter, config, run_id="same-synthetic-run")
    rotated_identity = inspect_pdf(pdf, rotated_adapter, config, run_id="same-synthetic-run")

    assert first_adapter.descriptor.fingerprint == rotated_adapter.descriptor.fingerprint
    assert first_identity.fingerprint == same_identity.fingerprint
    assert first_identity.private_adapter_scope_fingerprint != rotated_identity.private_adapter_scope_fingerprint
    assert first_identity.fingerprint != rotated_identity.fingerprint
    serialized_identity = json.dumps(first_identity.model_dump())
    assert SYNTHETIC_KEY not in serialized_identity
    assert "synthetic-folder" not in serialized_identity
    assert first_identity.private_adapter_scope_fingerprint in serialized_identity


def test_changed_prompt_invalidates_checkpoint_even_when_prompt_version_is_unchanged(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "prompt-fingerprint.pdf")
    checkpoint = tmp_path / "prompt-fingerprint-checkpoint"
    config = _orchestration_config()
    transport = FakeTransport()
    original_adapter = _adapter(transport)
    original_prompt_version = original_adapter.descriptor.prompt_version
    first = asyncio.run(_make_orchestrator(
        pdf, original_adapter, config, checkpoint,
    ).run(pdf))
    assert first.status == "completed"

    monkeypatch.setattr(
        qwen_vision_client_module,
        "_PROMPT",
        qwen_vision_client_module._PROMPT + "\nSynthetic prompt revision for fingerprint test.",
    )
    changed_adapter = _adapter(transport)
    assert changed_adapter.descriptor.prompt_version == original_prompt_version
    assert changed_adapter.descriptor.fingerprint != original_adapter.descriptor.fingerprint
    with pytest.raises(OrchestrationError, match="checkpoint_identity_mismatch"):
        asyncio.run(_make_orchestrator(pdf, changed_adapter, config, checkpoint).run(pdf))
    assert len(transport.calls) == 1


def test_user_context_is_private_checkpoint_identity_and_exactly_resumed(tmp_path):
    context = "Synthetic hint: inspect wrapped names. Preserve every source value as read."
    changed_context = context + " Check the note column too."
    transport = FakeTransport()
    adapter = _adapter(transport, user_context=context)
    same_context_adapter = _adapter(FakeTransport(), user_context=context)
    changed_context_adapter = _adapter(FakeTransport(), user_context=changed_context)
    empty_adapter = _adapter(FakeTransport())
    empty_string_adapter = _adapter(FakeTransport(), user_context="")

    assert adapter.descriptor.fingerprint == empty_adapter.descriptor.fingerprint
    assert empty_adapter.private_checkpoint_scope_fingerprint == empty_string_adapter.private_checkpoint_scope_fingerprint
    assert adapter.descriptor.fingerprint == same_context_adapter.descriptor.fingerprint
    assert adapter.private_checkpoint_scope_fingerprint == same_context_adapter.private_checkpoint_scope_fingerprint
    assert adapter.private_checkpoint_scope_fingerprint != empty_adapter.private_checkpoint_scope_fingerprint
    assert adapter.private_checkpoint_scope_fingerprint != changed_context_adapter.private_checkpoint_scope_fingerprint
    assert context not in json.dumps(adapter.descriptor.model_dump())

    pdf = _make_pdf(tmp_path / "context-identity.pdf")
    checkpoint = tmp_path / "context-identity-checkpoint"
    config = _orchestration_config()
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert first.status == "completed"
    request_messages = transport.calls[0]["payload"]["messages"]
    assert request_messages[2]["content"][1]["text"].endswith(context)
    public = json.dumps({"descriptor": first.adapter.model_dump(), "report": first.model_dump(mode="json")})
    saved = "".join(path.read_text(encoding="utf-8") for path in checkpoint.glob("*.json"))
    assert context not in public and context not in saved

    resumed = asyncio.run(_make_orchestrator(
        pdf, same_context_adapter, config, checkpoint,
    ).run(pdf))
    assert resumed.status == "completed"
    assert len(transport.calls) == 1
    with pytest.raises(OrchestrationError, match="checkpoint_identity_mismatch"):
        asyncio.run(_make_orchestrator(
            pdf, changed_context_adapter, config, checkpoint,
        ).run(pdf))
    assert len(transport.calls) == 1


def test_user_context_has_no_silent_truncation_and_reserves_safe_tpm_bound(tmp_path):
    context = "я" * 1000
    accepted = _adapter(FakeTransport(), user_context=context)
    with pytest.raises(ValueError, match="user_context_too_long"):
        _adapter(FakeTransport(), user_context=context + "я")

    pdf = _make_pdf(tmp_path / "context-token-bound.pdf")
    image = FitZPageRenderer().render(pdf, 1, 72, _orchestration_config())
    extra_bound = accepted.client.additional_context_token_reservation(
        image.data, 1, context,
    )
    estimate = accepted.estimate(image)
    assert extra_bound > len(context.encode("utf-8"))
    assert estimate.reserved_input_tokens == accepted.input_token_upper_bound + extra_bound
    assert estimate.reserved_output_tokens == accepted.client.config.limits.max_output_tokens

    no_send = FakeTransport()
    bounded = _adapter(
        no_send, user_context=context, input_bound=10,
        tpm=10 + _limits().max_output_tokens,
    )
    report = asyncio.run(_make_orchestrator(
        pdf, bounded,
        _orchestration_config(tpm=10 + _limits().max_output_tokens),
        tmp_path / "context-token-bound-checkpoint",
    ).run(pdf))
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[0].safe_error_code == "request_estimate_invalid"
    assert no_send.calls == []


def test_input_and_output_reservation_must_fit_documented_context_window(tmp_path):
    pdf = _make_pdf(tmp_path / "context-window-boundary.pdf")
    config = _orchestration_config(tpm=300000)
    image = FitZPageRenderer().render(pdf, 1, 72, config)
    no_send = FakeTransport()
    adapter = _adapter(
        no_send,
        input_bound=262144,
        tpm=300000,
        limits=_limits(max_output_tokens=32768),
    )

    with pytest.raises(ValueError, match="model context window"):
        adapter.estimate(image)
    assert no_send.calls == []


def test_context_request_size_cap_still_blocks_http_before_dispatch(tmp_path):
    context = "Synthetic request-size test hint."
    measurement_adapter = _adapter(FakeTransport())
    pdf = _make_pdf(tmp_path / "context-request-size.pdf")
    image = FitZPageRenderer().render(pdf, 1, 72, _orchestration_config())
    baseline_bytes = measurement_adapter.client.measure_request_bytes(image.data, 1)
    no_send = FakeTransport()
    bounded = _adapter(
        no_send,
        user_context=context,
        limits=_limits(max_request_bytes=baseline_bytes + 8),
    )

    report = asyncio.run(_make_orchestrator(
        pdf, bounded, _orchestration_config(),
        tmp_path / "context-request-size-checkpoint",
    ).run(pdf))

    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[0].safe_error_code == "request_size_limit"
    assert no_send.calls == []


def test_secret_store_descriptor_checkpoint_and_logs_contain_no_credentials_or_http_body(tmp_path, caplog):
    store = MemorySecretStore()
    store.set(YANDEX_AI_API_KEY, SYNTHETIC_KEY)
    settings = SimpleNamespace(
        folder_id="synthetic-folder",
        llm_model="qwen3.6-35b-a3b",
        llm_base_url="https://ai.synthetic.invalid",
    )
    transport = FakeTransport(lambda page, _call, _kw: (
        _response(page) if page == 1 else OSError(f"{SYNTHETIC_KEY} {SYNTHETIC_TEXT}")
    ))
    registry = QwenVisionQuotaRegistry()
    adapter = QwenVisionAdapter.from_app_settings(
        settings,
        store,
        limits=_limits(),
        quota_registry=registry,
        requests_per_minute=1000,
        tokens_per_minute=100000,
        input_token_upper_bound=256,
        pricing_profile_id="synthetic-tariff-v1",
        environ={},
        transport=transport,
    )
    pdf = _make_pdf(tmp_path / "private-check.pdf", 2)
    caplog.set_level(logging.INFO)
    checkpoint = tmp_path / "private-checkpoint"
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), checkpoint,
    ).run(pdf))

    public = json.dumps({"adapter": report.adapter.model_dump(), "report": report.model_dump(mode="json")})
    saved = "".join(path.read_text(encoding="utf-8") for path in checkpoint.glob("*.json"))
    assert SYNTHETIC_KEY not in repr(adapter.descriptor)
    assert SYNTHETIC_KEY not in public and SYNTHETIC_KEY not in saved
    assert "synthetic-folder" not in public and "synthetic-folder" not in saved
    assert SYNTHETIC_TEXT in public and SYNTHETIC_TEXT in saved
    assert SYNTHETIC_TEXT not in caplog.text
    assert SYNTHETIC_KEY not in caplog.text
    assert "image_url" not in caplog.text and "base64" not in caplog.text.casefold()
    assert "data:image/png;base64," not in public and "data:image/png;base64," not in saved


def test_v2a_export_guard_stays_closed_for_unconfirmed_qwen_draft(tmp_path):
    transport = FakeTransport()
    adapter = _adapter(transport)
    pdf = _make_pdf(tmp_path / "export-guard.pdf")
    report = asyncio.run(_make_orchestrator(
        pdf, adapter, _orchestration_config(), tmp_path / "export-guard-checkpoint",
    ).run(pdf))
    assert report.draft.export_authorized is False
    with pytest.raises(AIReviewError, match="page_completeness_not_human_confirmed"):
        assert_review_ready_for_export(report.draft, new_review_state(report.draft))


def test_live_qwen_runtime_disables_reasoning_in_payload_and_fingerprints_it(monkeypatch, tmp_path):
    from averon_import.services.ai_first.local_workflow import build_live_qwen_runtime

    transport = FakeTransport()
    original_from_settings = QwenVisionAdapter.from_app_settings

    def with_fake_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return original_from_settings(*args, **kwargs)

    monkeypatch.setattr(
        QwenVisionAdapter, "from_app_settings", staticmethod(with_fake_transport),
    )
    env = {
        "AVERON_AI_FIRST_RPM_LIMIT": "1",
        "AVERON_AI_FIRST_TPM_LIMIT": "1000",
        "AVERON_AI_FIRST_INPUT_TOKEN_UPPER_BOUND": "512",
        "AVERON_AI_FIRST_MAX_REQUESTS": "1",
        "AVERON_AI_FIRST_MAX_BUDGET_RUB": "1",
        "AVERON_AI_FIRST_INPUT_RUB_PER_1K": "0.20",
        "AVERON_AI_FIRST_CACHED_INPUT_RUB_PER_1K": "0.05",
        "AVERON_AI_FIRST_OUTPUT_RUB_PER_1K": "0.30",
        "AVERON_AI_FIRST_MAX_OUTPUT_TOKENS": "256",
        "AVERON_AI_FIRST_DPI": "72",
        "AVERON_AI_FIRST_REQUEST_TIMEOUT_S": "120",
        "AVERON_YANDEX_AI_MODEL": "gpt://synthetic-folder/qwen3.6-35b-a3b",
        "AVERON_YANDEX_AI_BASE_URL": "https://ai.api.cloud.yandex.net/v1",
        "AVERON_YANDEX_AI_API_KEY": "synthetic-key",
    }
    settings = SimpleNamespace(
        folder_id="synthetic-folder",
        llm_model="qwen3.6-35b-a3b",
        llm_base_url="https://ai.api.cloud.yandex.net/v1",
    )
    adapter, _config = build_live_qwen_runtime(
        settings=settings,
        secret_store=MemorySecretStore(),
        quota_registry=QwenVisionQuotaRegistry(),
        user_context=None,
        page_count=1,
        environ=env,
    )

    assert adapter.client.config.limits.reasoning_effort == "none"
    assert adapter.descriptor.parameters["reasoning_effort"] == "none"

    pdf = _make_pdf(tmp_path / "reasoning-runtime.pdf")
    with fitz.open(pdf) as document:
        image = document[0].get_pixmap(dpi=72).tobytes("png")
    adapter.client.extract(image, 1)
    assert transport.calls[0]["payload"]["reasoning_effort"] == "none"

    baseline_limits = replace(adapter.client.config.limits, reasoning_effort=None)
    baseline_client = QwenVisionClient(
        replace(adapter.client.config, limits=baseline_limits), transport=transport,
    )
    baseline_adapter = QwenVisionAdapter(
        baseline_client,
        quota_registry=QwenVisionQuotaRegistry(),
        requests_per_minute=adapter.requests_per_minute,
        tokens_per_minute=adapter.tokens_per_minute,
        input_token_upper_bound=adapter.input_token_upper_bound,
        pricing_profile_id=adapter.pricing_profile_id,
    )
    assert baseline_adapter.descriptor.fingerprint != adapter.descriptor.fingerprint
