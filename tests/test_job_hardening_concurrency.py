from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.models import (
    HistorySafeMatchBasis,
    Offer,
    ProductIntent,
    ProjectSourcingResult,
    SourcingResult,
    SourcingSourceMode,
)
from averon_import.services.sourcing.providers.base import SourcingProviderError
from averon_import.services.sourcing.providers.one_c_history import (
    HistoryLookupResult,
    HistoryMatchOutcome,
)
from averon_import.services.sourcing.run_history import SourcingRunHistory
from averon_import.services.sourcing.service import SourcingService
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.jobs import JobCoordinator, SOURCING


def _wait_status(service: JobCoordinator, job_id: str, status: str, timeout: float = 2.0):
    sleeper = threading.Event()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        current = service.get(job_id)
        if current.status == status:
            return current
        sleeper.wait(0.005)
    raise AssertionError(f"job did not reach {status}")


def _intent(source_row_id: str) -> ProductIntent:
    return ProductIntent(
        source_row_id=source_row_id,
        source_text=f"synthetic item {source_row_id}",
        normalized_name=f"synthetic item {source_row_id}",
        quantity="1",
        unit="шт.",
    )


def test_sourcing_cache_concurrent_instances_do_not_lose_updates(tmp_path):
    path = tmp_path / "sourcing-cache.json"
    count = 12
    barrier = threading.Barrier(count)
    errors = []

    def write(index: int):
        cache = SourcingCache(path)
        try:
            barrier.wait(timeout=2)
            cache.set(f"search-{index}", SourcingResult(intent=_intent(f"r{index}")))
        except Exception as exc:  # surfaced from worker threads below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert {key for key in payload if key.startswith("search-")} == {
        f"search-{index}" for index in range(count)
    }
    assert list(path.parent.glob(".sourcing-cache.json.*.tmp")) == []


def test_sourcing_run_history_concurrent_prune_keeps_distinct_immutable_records(tmp_path):
    root = tmp_path / "runs"
    history = SourcingRunHistory(root, retention_limit=40)
    count = 24
    barrier = threading.Barrier(count)
    errors = []

    def write(index: int):
        record = {
            "run_id": f"{index:032x}",
            "created_at": f"2026-09-{(index % 28) + 1:02d}T00:00:00+00:00",
            "status": "completed",
        }
        try:
            barrier.wait(timeout=2)
            history._persist(record)
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(write, index) for index in range(count)]
        for future in futures:
            future.result(timeout=5)
    assert errors == []
    assert len(history.list_records()) == count
    with pytest.raises(FileExistsError):
        history._persist({"run_id": f"{0:032x}", "created_at": "later"})
    assert len(history.list_records()) == count
    assert list(root.glob(".*.tmp")) == []


class _Provider:
    key = "fake"
    label = "Fake"

    def __init__(self, failures: dict[int, Exception] | None = None):
        self.failures = failures or {}
        self.calls = 0

    def stats(self):
        return {"reachable": True, "catalog_version": "v1"}

    def search(self, intent, *, limit=20):
        self.calls += 1
        failure = self.failures.get(self.calls)
        if failure is not None:
            raise failure
        return []


def _rows(count: int):
    return [
        {
            "id": f"row-{index}",
            "row_type": "item",
            "name": f"test item {index}",
            "quantity": "1",
            "unit": "шт.",
        }
        for index in range(count)
    ]


def test_provider_wide_failure_opens_project_local_circuit():
    provider = _Provider({1: SourcingProviderError("offline", category="network")})
    service = SourcingService({"fake": provider}, default_provider="fake")
    result = service.search_project(_rows(4), ai_rerank=False)
    assert provider.calls == 1
    assert result.results[0].provider_error_category == "network"
    assert [row.provider_call_suppressed for row in result.results[1:]] == [True, True, True]
    assert all(
        any(notice.code == "PROJECT_PROVIDER_CIRCUIT_OPEN" for notice in row.notices)
        for row in result.results[1:]
    )
    assert result.positions_without_offers == 4
    assert result.confirmed_total is None


def test_row_local_invalid_request_does_not_open_project_circuit():
    provider = _Provider({1: SourcingProviderError("bad item", category="invalid_request")})
    result = SourcingService({"fake": provider}, default_provider="fake").search_project(
        _rows(2), ai_rerank=False
    )
    assert provider.calls == 2
    assert result.results[0].provider_error_category == "invalid_request"
    assert not result.results[1].provider_call_suppressed


def test_unknown_provider_failure_defaults_to_project_wide():
    provider = _Provider({1: RuntimeError("unclassified provider failure")})
    result = SourcingService({"fake": provider}, default_provider="fake").search_project(
        _rows(3), ai_rerank=False
    )
    assert provider.calls == 1
    assert result.results[0].provider_error_category == "provider_error"
    assert [item.provider_call_suppressed for item in result.results[1:]] == [True, True]


class _History:
    def __init__(self, safe_offer: Offer):
        self.safe_offer = safe_offer

    def lookup(self, intent, *, source_intent=None, limit=20):
        if intent.source_row_id == "row-1":
            return HistoryLookupResult(
                HistoryMatchOutcome.SAFE_MATCH,
                True,
                selected_offer=self.safe_offer,
                candidates=(self.safe_offer,),
                reason_code="exact_article",
                safe_basis=HistorySafeMatchBasis.EXACT_ARTICLE,
                catalog_version="history-v1",
            )
        return HistoryLookupResult(
            HistoryMatchOutcome.NO_MATCH,
            True,
            reason_code="no_match",
            catalog_version="history-v1",
        )


def test_one_c_then_provider_keeps_later_safe_history_match_after_live_circuit_opens():
    historical = Offer(
        offer_id="history-event",
        provider="one_c_history",
        title="synthetic historical purchase",
        price=Decimal("777.00"),
        currency="RUB",
        data_provenance={"purchase_date": "2026-08-01", "selected_event_id": "event-1"},
    )
    provider = _Provider({1: SourcingProviderError("offline", category="upstream_error")})
    service = SourcingService(
        {"fake": provider},
        default_provider="fake",
        one_c_history_provider=_History(historical),
    )
    result = service.search_project_routed(
        _rows(3),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        provider_key="fake",
        ai_rerank=False,
    )
    assert provider.calls == 1
    assert result.results[1].route.final_source_kind == "historical_purchase"
    assert result.results[1].route.fallback_called is False
    assert result.results[2].route.fallback_status == "suppressed"
    assert result.results[2].route.fallback_called is False
    assert result.results[2].provider_call_suppressed is True
    assert not any(
        notice.code == "ONE_C_FALLBACK_USED" for notice in result.results[2].notices
    )
    assert any(
        notice.code == "ONE_C_FALLBACK_SUPPRESSED" for notice in result.results[2].notices
    )
    assert result.results[2].offers == []
    assert result.positions_fallback_called == 1
    assert result.positions_history_matched == 1
    assert result.confirmed_total is None
    assert result.confirmed_totals == {}


def test_one_c_fallback_health_failure_is_not_counted_as_live_call():
    class UnreachableProvider(_Provider):
        def stats(self):
            return {"reachable": False, "catalog_version": "unavailable"}

    class EmptyHistory:
        def lookup(self, intent, *, source_intent=None, limit=20):
            return HistoryLookupResult(
                HistoryMatchOutcome.NO_MATCH,
                True,
                reason_code="no_match",
                catalog_version="history-v1",
            )

    provider = UnreachableProvider()
    service = SourcingService(
        {"fake": provider},
        default_provider="fake",
        one_c_history_provider=EmptyHistory(),
    )
    result = service.search_project_routed(
        _rows(2),
        source_mode=SourcingSourceMode.ONE_C_THEN_PROVIDER,
        provider_key="fake",
        ai_rerank=False,
    )

    assert provider.calls == 0
    assert result.positions_fallback_called == 0
    assert all(item.route.fallback_called is False for item in result.results)
    assert all(item.route.fallback_status == "suppressed" for item in result.results)
    assert all(item.provider_call_suppressed is True for item in result.results)
    assert all(
        any(notice.code == "ONE_C_FALLBACK_SUPPRESSED" for notice in item.notices)
        for item in result.results
    )
    assert all(
        not any(notice.code == "ONE_C_FALLBACK_USED" for notice in item.notices)
        for item in result.results
    )
    assert all(item.offers == [] for item in result.results)


def test_sourcing_runtime_rebuild_does_not_rebind_accepted_job(monkeypatch):
    from averon_import import main

    class FakeProvider:
        key = "fake"
        label = "Fake"

    class FakeService:
        def __init__(self, identity):
            self.identity = identity
            self.calls = 0

        def provider(self, _key=None):
            return FakeProvider()

        def search_project(self, rows, **_kwargs):
            self.calls += 1
            return ProjectSourcingResult(
                positions_total=len(rows),
                positions_processed=len(rows),
                source_mode=SourcingSourceMode.PROVIDER_ONLY,
                provider_key=self.identity,
                provider_label=self.identity,
            )

    jobs = JobCoordinator()
    runtime_a = FakeService("A")
    runtime_b = FakeService("B")
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(main, "job_service", jobs)
    monkeypatch.setattr(main, "sourcing_service", runtime_a)
    try:
        blocker = jobs.submit(
            lambda _progress: (started.set(), release.wait(2))[1], lane=SOURCING
        )
        assert started.wait(1)
        accepted = main._submit_sourcing_project_job(
            _rows(1), provider_key="fake", limit=20
        )
        monkeypatch.setattr(main, "sourcing_service", runtime_b)
        release.set()
        _wait_status(jobs, accepted["id"], "completed")
        assert jobs.get(accepted["id"]).public()["result"]["provider_key"] == "A"
        assert runtime_a.calls == 1

        next_job = main._submit_sourcing_project_job(
            _rows(1), provider_key="fake", limit=20
        )
        _wait_status(jobs, next_job["id"], "completed")
        assert jobs.get(next_job["id"]).public()["result"]["provider_key"] == "B"
        assert runtime_b.calls == 1
        assert jobs.get(blocker.id).status == "completed"
    finally:
        release.set()
        jobs.executor.shutdown(wait=True)


def test_job_read_is_owner_bound_and_public_payload_omits_owner():
    from fastapi import HTTPException

    from averon_import import main
    from averon_import.services.auth import CurrentUser, Role

    jobs = JobCoordinator()
    job = jobs.submit(lambda _progress: True, lane=SOURCING, owner_id="account-a")
    try:
        owner = CurrentUser(username="a", role=Role.USER, user_id="account-a")
        other = CurrentUser(username="b", role=Role.USER, user_id="account-b")
        monkeypatch_job_service = main.job_service
        main.job_service = jobs
        try:
            assert main.get_job(job.id, owner)["id"] == job.id
            with pytest.raises(HTTPException) as denied:
                main.get_job(job.id, other)
            assert denied.value.status_code == 404
            assert "owner_id" not in job.public()
        finally:
            main.job_service = monkeypatch_job_service
    finally:
        jobs.executor.shutdown(wait=True)


def test_ai_shared_status_and_provider_calls_are_serialized():
    class SlowProvider:
        model = "test"
        configured = True

        def __init__(self):
            self.guard = threading.Lock()
            self.active = 0
            self.maximum = 0
            self.first_entered = threading.Event()
            self.release = threading.Event()

        def complete(self, messages, **_kwargs):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            self.first_entered.set()
            self.release.wait(1)
            with self.guard:
                self.active -= 1
            return json.dumps({"normalized_name": "safe item"})

    class AiService:
        def __init__(self, provider):
            self.provider = provider

        def ensure_provider(self, _key):
            return self.provider

        def public_config(self):
            return {"providers": {"yandex": {"configured": True}}}

    provider = SlowProvider()
    ai = SourcingAIService(AiService(provider))
    rows = [
        {"id": "r1", "name": "safe item", "quantity": "1"},
        {"id": "r2", "name": "safe item", "quantity": "1"},
    ]
    futures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures.append(pool.submit(ai.understand_with_audit, rows[0], _intent("r1")))
        assert provider.first_entered.wait(1)
        futures.append(pool.submit(ai.understand_with_audit, rows[1], _intent("r2")))
        provider.release.set()
        [future.result(timeout=2) for future in futures]
    assert provider.maximum == 1
