"""Offline dispatch, crash and resource regressions for Phase A."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from test_ai_first_orchestration import SyntheticAdapter, SyntheticRenderer, make_pdf, make_config, run_job
from averon_import.services.ai_first.orchestration import CheckpointStore
from averon_import.services.ai_first.dispatch import RENDER_CAPACITY, SYNC_RENDER_CAPACITY
from averon_import.services.ai_first.qwen_vision_adapter import QwenVisionQuotaRegistry
from test_ai_first_qwen_vision_adapter import (
    FakeTransport, SYNTHETIC_KEY, _adapter, _make_pdf, _make_orchestrator,
    _orchestration_config, _response,
)


def test_limiter_cannot_age_out_while_dispatch_checkpoint_is_delayed(tmp_path, monkeypatch):
    """Two old permits must not join two new permits in one transport window."""
    pdf = make_pdf(tmp_path / "delayed-dispatch.pdf", 4)
    adapter = SyntheticAdapter()
    original = CheckpointStore.write
    delayed = 0

    async def delayed_write(store, record):
        nonlocal delayed
        attempt = record.attempts[-1] if record.attempts else None
        if (record.state == "running" and (attempt.request_started or getattr(attempt, "dispatch_intent_at", None)) and delayed < 2):
            delayed += 1
            await asyncio.sleep(0.3)
        await original(store, record)

    monkeypatch.setattr(CheckpointStore, "write", delayed_write)
    report = run_job(tmp_path, pdf, adapter, config=make_config(
        concurrency=4, requests_per_minute=2, tokens_per_minute=40,
    ), rate_window_s=0.2)
    assert report.status == "completed"
    starts = sorted(adapter.started_at)
    assert len(starts) == 4
    assert max(sum(start <= stamp < start + 0.2 for stamp in starts) for start in starts) <= 2


@pytest.mark.parametrize("concurrency", [2, 4, 8, 10])
def test_http_requests_really_overlap_at_each_parallelism(tmp_path, concurrency):
    # Every POST must enter the barrier before any can return. A serial
    # implementation times out; max_active alone is not the evidence.
    transport = FakeTransport(request_barrier=threading.Barrier(concurrency), delay_s=0.05)
    pdf = _make_pdf(tmp_path / "overlap.pdf", concurrency)
    adapter = _adapter(transport)
    runner = _make_orchestrator(pdf, adapter, _orchestration_config(concurrency=concurrency), tmp_path / "cp")
    start = time.monotonic()
    report = asyncio.run(runner.run(pdf))
    elapsed = time.monotonic() - start
    assert report.status == "completed"
    assert transport.max_active == concurrency
    assert len(transport.calls) == concurrency
    assert max(transport.started_at) - min(transport.started_at) < elapsed - 0.04
    assert report.max_inflight_image_count <= concurrency
    assert len({p.extraction.extraction_fingerprint for p in report.pages}) == concurrency
    print(json.dumps({"concurrency": concurrency, "max_http_active": transport.max_active,
                      "elapsed_s": round(elapsed, 4),
                      "start_span_s": round(max(transport.started_at) - min(transport.started_at), 4)}))


@pytest.mark.parametrize("rpm,tpm", [(2, 100000), (1000, 768)])
def test_shared_quota_cannot_expire_during_client_preparation(tmp_path, monkeypatch, rpm, tpm):
    from averon_import.services.ocr.qwen_vision_client import QwenVisionClient
    registry = QwenVisionQuotaRegistry(window_s=0.2)
    transport = FakeTransport()
    original = QwenVisionClient.extract
    lock = threading.Lock()
    calls = 0

    def delayed_extract(client, *args, **kwargs):
        nonlocal calls
        with lock:
            calls += 1
            delay = calls <= 2
        if delay:
            time.sleep(0.3)
        return original(client, *args, **kwargs)

    monkeypatch.setattr(QwenVisionClient, "extract", delayed_extract)
    jobs = []
    for index in range(2):
        pdf = _make_pdf(tmp_path / f"shared-{index}.pdf", 2)
        adapter = _adapter(transport, registry=registry, rpm=rpm, tpm=tpm)
        runner = _make_orchestrator(pdf, adapter, _orchestration_config(rpm=rpm, tpm=tpm), tmp_path / f"cp-{index}")
        jobs.append((pdf, runner))

    async def run():
        return await asyncio.gather(*(runner.run(pdf) for pdf, runner in jobs))

    reports = asyncio.run(run())
    assert all(r.status == "completed" for r in reports)
    assert len(transport.calls) == 4
    stamps = sorted(transport.started_at)
    assert max(sum(start <= stamp < start + 0.2 for stamp in stamps) for start in stamps) <= 2


def test_global_page_capacity_bounds_two_ten_page_documents(tmp_path):
    transport = FakeTransport(request_barrier=threading.Barrier(10), delay_s=0.02)
    registry = QwenVisionQuotaRegistry()
    jobs = []
    for index in range(2):
        pdf = _make_pdf(tmp_path / f"capacity-{index}.pdf", 10)
        runner = _make_orchestrator(pdf, _adapter(transport, registry=registry),
                                    _orchestration_config(concurrency=10), tmp_path / f"cp-{index}")
        jobs.append((pdf, runner))

    async def run():
        return await asyncio.gather(*(runner.run(pdf) for pdf, runner in jobs))

    reports = asyncio.run(run())
    assert all(r.status == "completed" for r in reports)
    assert transport.max_active == 10
    assert len(transport.calls) == 20
    assert all(r.max_inflight_image_count <= 10 for r in reports)


def test_cancellation_while_waiting_shared_quota_has_no_dispatch_intent(tmp_path):
    registry = QwenVisionQuotaRegistry(window_s=0.05)
    transport = FakeTransport()
    pdf = _make_pdf(tmp_path / "cancel-quota.pdf", 1)
    runner = _make_orchestrator(pdf, _adapter(transport, registry=registry, rpm=1),
                                _orchestration_config(rpm=1, concurrency=1), tmp_path / "cp")

    async def run():
        permit = await registry.acquire(SYNTHETIC_KEY, 1, 100000, 384, None)
        cancel = asyncio.Event()
        task = asyncio.create_task(runner.run(pdf, cancel_event=cancel))
        # Wait until preparation has reserved the document budget.
        deadline = time.monotonic() + 5
        while not list((tmp_path / "cp").glob("page-*.json")):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # longer than the rolling window: permit stays pinned
        cancel.set()
        result = await task
        permit.finish(request_started=False)
        return result

    report = asyncio.run(run())
    assert transport.calls == []
    assert report.reserved_unknown_cost_rub == 0
    attempt = report.pages[0].attempts[-1]
    assert attempt.request_started is False
    assert attempt.dispatch_intent_at is None and attempt.dispatched_at is None


def test_request_cap_is_atomic_for_ten_simultaneous_pages(tmp_path):
    pdf = _make_pdf(tmp_path / "request-cap.pdf", 10)
    transport = FakeTransport(request_barrier=threading.Barrier(3))
    config = _orchestration_config(concurrency=10).model_copy(update={"max_requests": 3})
    runner = _make_orchestrator(pdf, _adapter(transport), config, tmp_path / "cp")
    report = asyncio.run(runner.run(pdf))
    assert len(transport.calls) == 3
    assert sum(p.state == "completed" for p in report.pages) == 3
    assert sum(a.request_started for p in report.pages for a in p.attempts) == 3


def test_unknown_cost_reservations_bound_parallel_budget_and_resume(tmp_path):
    def timeout(*_args):
        raise TimeoutError

    transport = FakeTransport(timeout)
    pdf = _make_pdf(tmp_path / "budget.pdf", 10)
    config = _orchestration_config(concurrency=10, budget=Decimal("0.19"))
    adapter = _adapter(transport)
    first = asyncio.run(_make_orchestrator(pdf, adapter, config, tmp_path / "cp").run(pdf))
    assert len(transport.calls) <= 2
    assert first.reserved_unknown_cost_rub <= config.max_budget_rub
    calls = len(transport.calls)
    resumed = asyncio.run(_make_orchestrator(pdf, adapter, config, tmp_path / "cp").run(pdf))
    assert len(transport.calls) == calls
    assert resumed.reserved_unknown_cost_rub == first.reserved_unknown_cost_rub


def test_render_failure_does_not_send_and_keeps_other_pages(tmp_path):
    from test_ai_first_orchestration import SyntheticRenderer

    class FailingRenderer(SyntheticRenderer):
        def render(self, path, number, dpi, config):
            if number == 2:
                raise RuntimeError("synthetic native render error")
            return super().render(path, number, dpi, config)

    pdf = make_pdf(tmp_path / "render-failure.pdf", 10)
    adapter = SyntheticAdapter(delay=lambda n: (11 - n) * 0.003)
    report = run_job(tmp_path, pdf, adapter, config=make_config(concurrency=10), renderer=FailingRenderer())
    assert sorted(adapter.calls) == [1, *range(3, 11)]
    assert report.pages[1].attempts[-1].request_started is False
    assert report.pages[1].attempts[-1].safe_error_code == "page_render_failed"
    assert sum(p.state == "completed" for p in report.pages) == 9
    assert adapter.finished != sorted(adapter.finished)
    assert [p.page_number for p in report.pages] == list(range(1, 11))


@pytest.mark.parametrize("phase", ["prepared", "intent", "transport"])
def test_killed_process_preserves_success_and_never_reposts_unknown(tmp_path, phase):
    pdf = _make_pdf(tmp_path / "crash.pdf", 2)
    checkpoint, ready = tmp_path / "cp", tmp_path / "ready"
    script = Path(__file__).with_name("ai_first_phase_a_crash_worker.py")
    process = subprocess.Popen([sys.executable, str(script), str(pdf), str(checkpoint), str(ready), phase],
                               cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(f"crash worker exited: {process.communicate()[1].decode(errors='replace')}")
            assert time.monotonic() < deadline
            time.sleep(0.01)
        successful_bytes = (checkpoint / "page-000001.json").read_bytes()
        prepared = json.loads((checkpoint / "page-000002.json").read_text(encoding="utf-8"))
        assert prepared["attempts"][-1]["request_started"] is False
        assert bool(prepared["attempts"][-1].get("dispatch_intent_at")) == (phase != "prepared")
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    transport = FakeTransport()
    adapter = _adapter(transport)
    config = _orchestration_config(concurrency=1)
    resumed = asyncio.run(_make_orchestrator(pdf, adapter, config, checkpoint).run(pdf))
    assert (checkpoint / "page-000001.json").read_bytes() == successful_bytes
    assert resumed.pages[0].state == "completed"
    if phase == "prepared":
        assert transport.started_pages == [2]
        assert resumed.pages[1].state == "completed"
    else:
        assert transport.calls == []
        assert resumed.pages[1].state == "unknown_outcome"
        assert resumed.reserved_unknown_cost_rub == Decimal("0.0896")
        assert resumed.pages[1].attempts[-1].request_started is False


def test_real_renderer_is_isolated_and_submissions_are_bounded(tmp_path, monkeypatch):
    from averon_import.services.ai_first import pdf_render_worker as worker
    from averon_import.services.ai_first.orchestration import FitZPageRenderer
    pdf = _make_pdf(tmp_path / "isolated.pdf", 10)
    transport = FakeTransport(request_barrier=threading.Barrier(10))
    runner = _make_orchestrator(pdf, _adapter(transport), _orchestration_config(concurrency=10), tmp_path / "cp")
    pool = worker._get_pool()
    pid = pool.submit(os.getpid).result(timeout=10)
    assert pid != os.getpid()
    real_submit = pool.submit
    active = peak = 0
    lock = threading.Lock()

    def submit(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        future = real_submit(*args, **kwargs)

        def done(_future):
            nonlocal active
            with lock:
                active -= 1

        future.add_done_callback(done)
        return future

    monkeypatch.setattr(pool, "submit", submit)
    # A parent-process PyMuPDF render would fail; spawned worker state is isolated.
    monkeypatch.setattr(FitZPageRenderer, "render", lambda *_args: pytest.fail("render ran in app process"))
    report = asyncio.run(runner.run(pdf))
    assert report.status == "completed"
    assert peak == 1 and active == 0
    assert transport.max_active == 10


def test_quota_and_capacity_are_shared_by_documents_in_different_event_loops(tmp_path):
    transport = FakeTransport(request_barrier=threading.Barrier(2), delay_s=0.01)
    registry = QwenVisionQuotaRegistry(window_s=0.15)
    jobs = []
    for index in range(2):
        pdf = _make_pdf(tmp_path / f"loops-{index}.pdf", 2)
        runner = _make_orchestrator(pdf, _adapter(transport, registry=registry, rpm=2),
                                    _orchestration_config(concurrency=2, rpm=2), tmp_path / f"cp-{index}")
        jobs.append((pdf, runner))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, runner.run(pdf)) for pdf, runner in jobs]
        reports = [future.result(timeout=15) for future in futures]
    assert all(r.status == "completed" for r in reports)
    stamps = sorted(transport.started_at)
    assert len(stamps) == 4
    assert max(sum(start <= stamp < start + 0.15 for stamp in stamps) for start in stamps) == 2


def test_duplicate_run_on_the_same_store_cannot_unlock_its_owner(tmp_path):
    from averon_import.services.ai_first.orchestration import OrchestrationError
    pdf = _make_pdf(tmp_path / "same-store.pdf", 1)
    transport = FakeTransport(delay_s=0.1)
    runner = _make_orchestrator(pdf, _adapter(transport), _orchestration_config(), tmp_path / "cp")

    async def run():
        task = asyncio.create_task(runner.run(pdf))
        while not transport.started_event.is_set():
            await asyncio.sleep(0.001)
        with pytest.raises(OrchestrationError, match="checkpoint_in_use"):
            await runner.run(pdf)
        other = CheckpointStore(runner.checkpoint.directory, runner.checkpoint.identity)
        with pytest.raises(OrchestrationError, match="checkpoint_in_use"):
            other.open()
        return await task

    assert asyncio.run(run()).status == "completed"
    assert len(transport.calls) == 1


def test_native_renderer_death_fails_closed_and_explicit_retry_uses_fresh_worker(tmp_path):
    from concurrent.futures.process import BrokenProcessPool
    from averon_import.services.ai_first import pdf_render_worker as worker
    pdf = _make_pdf(tmp_path / "dead-renderer.pdf", 1)
    transport = FakeTransport()
    adapter, config = _adapter(transport), _orchestration_config()
    runner = _make_orchestrator(pdf, adapter, config, tmp_path / "cp")
    original_render = runner.renderer.render_async

    async def kill_then_render(*args):
        with pytest.raises(BrokenProcessPool):
            worker._get_pool().submit(os._exit, 73).result(timeout=10)
        return await original_render(*args)

    runner.renderer.render_async = kill_then_render
    report = asyncio.run(runner.run(pdf))
    assert report.pages[0].state == "failed"
    assert report.pages[0].attempts[-1].request_started is False
    assert transport.calls == []
    # No automatic re-render/re-POST of this failed page.
    again = asyncio.run(_make_orchestrator(pdf, adapter, config, tmp_path / "cp").run(pdf))
    assert again.pages[0].state == "failed" and transport.calls == []
    retried = asyncio.run(_make_orchestrator(pdf, adapter, config, tmp_path / "cp").run(pdf, retry_pages={1}))
    assert retried.status == "completed" and len(transport.calls) == 1


def test_preparation_does_not_hold_false_dispatch_after_source_changes_during_intent(tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path / "changed-at-intent.pdf", 1)
    transport = FakeTransport()
    original = CheckpointStore.write

    async def write(store, record):
        await original(store, record)
        if record.state == "running" and record.attempts[-1].dispatch_intent_at:
            with pdf.open("ab") as stream:
                stream.write(b"synthetic source change")

    monkeypatch.setattr(CheckpointStore, "write", write)
    report = asyncio.run(_make_orchestrator(pdf, _adapter(transport), _orchestration_config(), tmp_path / "cp").run(pdf))
    assert transport.calls == []
    assert report.pages[0].attempts[-1].request_started is False
    assert report.reserved_unknown_cost_rub == 0


def test_legacy_unknown_without_cost_reservation_blocks_new_paid_attempts(tmp_path):
    from averon_import.services.ai_first.orchestration import PageAttempt, PageRunRecord, inspect_pdf
    pdf = make_pdf(tmp_path / "legacy-budget.pdf", 2)
    adapter = SyntheticAdapter()
    config = make_config(concurrency=2, max_budget_rub=Decimal("0.01"))
    identity = inspect_pdf(pdf, adapter, config, run_id="synthetic-run")
    store = CheckpointStore(tmp_path / "cp", identity)
    store.open()
    stamp = "2026-01-01T00:00:00+00:00"
    record = PageRunRecord(page_number=1, state="unknown_outcome", updated_at=stamp, attempts=[
        PageAttempt(state="unknown_outcome", started_at=stamp, finished_at=stamp,
                    request_started=True, safe_error_code="legacy_timeout")])
    asyncio.run(store.write(record))
    store.close()
    report = run_job(tmp_path, pdf, adapter, config=config, checkpoint=store.directory)
    assert report.status == "budget_exhausted"
    assert adapter.calls == []
    assert report.reserved_unknown_cost_rub == config.max_budget_rub


@pytest.mark.parametrize("operation", ["inspect", "regions"])
def test_async_pdf_worker_wait_is_responsive_and_cancellation_drains_slot(tmp_path, monkeypatch, operation):
    from concurrent.futures import Future
    from averon_import.services.ai_first import pdf_render_worker as worker
    from averon_import.services.ai_first.orchestration import inspect_pdf_async

    pdf = _make_pdf(tmp_path / f"async-{operation}.pdf", 1)
    adapter = _adapter(FakeTransport())
    config = _orchestration_config().model_copy(update={"page_regions": {1: (0.1, 0.1, 0.8, 0.8)}})
    runner = _make_orchestrator(pdf, adapter, config, tmp_path / "cp")
    started, release = threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def delayed_submit(call, *args):
        future = executor.submit(lambda: (started.set(), release.wait(timeout=5), call(*args))[2])
        future.add_done_callback(lambda _done: RENDER_CAPACITY.release())
        return future

    monkeypatch.setattr(worker, "_submit", delayed_submit)

    async def run():
        operation_task = asyncio.create_task(
            inspect_pdf_async(pdf, adapter, config, run_id="async-synthetic")
            if operation == "inspect" else runner._region_pdf_points(pdf)
        )
        assert await asyncio.to_thread(started.wait, 3)
        ticks = 0
        for _ in range(6):
            await asyncio.sleep(0.005)
            ticks += 1
        assert ticks == 6
        operation_task.cancel()
        await asyncio.sleep(0.02)
        assert not operation_task.done()
        assert not RENDER_CAPACITY._slots.acquire(blocking=False)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation_task
        assert RENDER_CAPACITY._slots.acquire(blocking=False)
        RENDER_CAPACITY.release()

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_sync_inspect_pdf_refuses_to_block_an_event_loop(tmp_path):
    from averon_import.services.ai_first.orchestration import OrchestrationError, inspect_pdf
    pdf = _make_pdf(tmp_path / "sync-inspect.pdf", 1)
    adapter, config = _adapter(FakeTransport()), _orchestration_config()

    async def run():
        with pytest.raises(OrchestrationError, match="async_inspect_required"):
            inspect_pdf(pdf, adapter, config)

    asyncio.run(run())


def test_legacy_sync_renderers_run_off_loop_and_serialize_across_documents(tmp_path):
    started, release = threading.Event(), threading.Event()
    state_lock = threading.Lock()
    active = peak = 0

    class BlockingRenderer(SyntheticRenderer):
        def render(self, path, number, dpi, config):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("synthetic renderer wait expired")
            try:
                return super().render(path, number, dpi, config)
            finally:
                with state_lock:
                    active -= 1

    renderer = BlockingRenderer()
    jobs = []
    adapters = []
    for index in range(2):
        pdf = _make_pdf(tmp_path / f"legacy-render-{index}.pdf", 1)
        adapter = SyntheticAdapter()
        adapters.append(adapter)
        runner = _make_orchestrator(pdf, adapter, _orchestration_config(concurrency=1),
                                    tmp_path / f"cp-{index}")
        runner.renderer = renderer
        jobs.append((pdf, runner))

    async def run():
        tasks = [asyncio.create_task(runner.run(pdf)) for pdf, runner in jobs]
        assert await asyncio.to_thread(started.wait, 3)
        ticks = 0
        for _ in range(8):
            await asyncio.sleep(0.005)
            ticks += 1
        assert ticks == 8
        assert peak == 1
        release.set()
        reports = await asyncio.gather(*tasks)
        return reports

    try:
        reports = asyncio.run(run())
    finally:
        release.set()
    assert all(report.status == "completed" for report in reports)
    assert all(len(adapter.calls) == 1 for adapter in adapters)
    assert peak == 1 and active == 0
    assert SYNC_RENDER_CAPACITY._slots.acquire(blocking=False)
    SYNC_RENDER_CAPACITY.release()


def test_cancelled_sync_renderer_drains_thread_and_releases_capacity(tmp_path):
    started, release = threading.Event(), threading.Event()
    active = 0

    class BlockingRenderer(SyntheticRenderer):
        def render(self, path, number, dpi, config):
            nonlocal active
            active += 1
            started.set()
            if not release.wait(timeout=5):
                active -= 1
                raise RuntimeError("synthetic renderer wait expired")
            try:
                return super().render(path, number, dpi, config)
            finally:
                active -= 1

    pdf = _make_pdf(tmp_path / "cancel-sync-render.pdf", 1)
    adapter = SyntheticAdapter()
    runner = _make_orchestrator(pdf, adapter, _orchestration_config(concurrency=1), tmp_path / "cp")
    runner.renderer = BlockingRenderer()

    async def run():
        task = asyncio.create_task(runner.run(pdf))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not SYNC_RENDER_CAPACITY._slots.acquire(blocking=False)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert SYNC_RENDER_CAPACITY._slots.acquire(blocking=False)
        SYNC_RENDER_CAPACITY.release()
        assert adapter.calls == []
        report = await runner.run(pdf)
        assert report.status == "completed"
        assert adapter.calls == [1]

    try:
        asyncio.run(run())
    finally:
        release.set()
