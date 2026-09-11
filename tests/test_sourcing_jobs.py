from __future__ import annotations

import time
from decimal import Decimal

import pytest

from averon_import.services.jobs import JobService
from averon_import.services.sourcing.cache import SourcingCache
from averon_import.services.sourcing.matching import MatchDecision
from averon_import.services.sourcing.models import Offer, ProductIntent
from averon_import.services.sourcing.product_understanding import SourcingAIService
from averon_import.services.sourcing.service import SourcingService


def offer() -> Offer:
    return Offer(
        offer_id="offer-1",
        provider="stub",
        source_item_id="item-1",
        title="Клапан DN50",
        price=Decimal("10"),
        currency="RUB",
        availability=True,
        attributes={"diameter": 50, "pressure": 16},
        data_provenance={"source": "fixture"},
    )


def intent(row_id: str) -> ProductIntent:
    return ProductIntent(
        source_row_id=row_id,
        source_text="Клапан DN50 PN16",
        normalized_name="Клапан",
        attributes={"diameter": 50, "pressure": 16},
        required_attributes={"diameter": 50, "pressure": 16},
        quantity="1",
        unit="шт.",
    )


class CountingProvider:
    key = "stub"
    label = "Тестовый поставщик"

    def __init__(self, *, fail_ids: set[str] | None = None):
        self.stats_calls = 0
        self.search_calls: list[str] = []
        self.fail_ids = fail_ids or set()

    def stats(self):
        self.stats_calls += 1
        return {"item_count": 1, "catalog_version": "catalog-1"}

    def search(self, requested, *, limit=20):
        self.search_calls.append(requested.source_row_id)
        if requested.source_row_id in self.fail_ids:
            raise ValueError("temporary provider failure")
        return [offer()][:limit]


def service_for(tmp_path, provider, ai=None):
    return SourcingService(
        {provider.key: provider},
        default_provider=provider.key,
        ai=ai or SourcingAIService(),
        cache=SourcingCache(tmp_path / "sourcing-cache.json"),
    )


def rows():
    return [
        {"id": "row-1", "row_type": "item", "name": "Клапан", "quantity": "1"},
        {"id": "row-2", "row_type": "item", "name": "Клапан", "quantity": "2"},
        {"id": "note", "row_type": "note", "name": "Не искать"},
    ]


def test_project_reports_real_progress_preserves_order_and_skips_rerank(tmp_path):
    provider = CountingProvider()
    ai = SourcingAIService()
    rank_calls = []
    original_rank = ai.rank_matches
    ai.rank_matches = lambda current_intent, matches: (
        rank_calls.append(current_intent.source_row_id),
        original_rank(current_intent, matches),
    )[1]
    service = service_for(tmp_path, provider, ai)
    progress = []

    result = service.search_project(rows(), progress=lambda current, total, message: progress.append((current, total, message)))

    assert result.positions_total == result.positions_processed == 2
    assert [item.intent.source_row_id for item in result.results] == ["row-1", "row-2"]
    assert progress[0][:2] == (0, 2)
    assert progress[-1][:2] == (2, 2)
    assert [event[0] for event in progress if event[2].startswith("Готово")] == [1, 2]
    assert "1 из 2" in next(event[2] for event in progress if event[0] == 0 and "Анализируем" in event[2])
    assert rank_calls == []
    assert provider.stats_calls == 1
    assert provider.search_calls == ["row-1", "row-2"]


def test_single_search_keeps_rerank_enabled(tmp_path):
    provider = CountingProvider()
    ai = SourcingAIService()
    rank_calls = []
    original_rank = ai.rank_matches

    def rank(current_intent, matches):
        rank_calls.append(current_intent.source_row_id)
        return original_rank(current_intent, matches)

    ai.rank_matches = rank
    service = service_for(tmp_path, provider, ai)
    result = service.search_intent(intent("single"))
    assert result.match_results[0].decision == MatchDecision.MATCH
    assert rank_calls == ["single"]


def test_project_resolves_provider_stats_once_per_run_and_reuses_both_caches(tmp_path):
    provider = CountingProvider()
    service = service_for(tmp_path, provider)
    first = service.search_project(rows())
    second = service.search_project(rows())

    assert provider.stats_calls == 2
    assert provider.search_calls == ["row-1", "row-2"]
    assert first.timings["provider_stats_calls"] == 1
    assert first.timings["understanding_cache_hits"] == 0
    assert second.timings["understanding_cache_hits"] == 2
    assert second.timings["search_cache_hits"] == 2


def test_project_continues_after_one_provider_error_without_fabricating_offer(tmp_path):
    provider = CountingProvider(fail_ids={"row-1"})
    result = service_for(tmp_path, provider).search_project(rows()[:2])

    assert result.positions_processed == 2
    assert result.positions_without_offers == 1
    assert result.results[0].offers == []
    assert result.results[0].recommended_offer is None
    assert "temporary provider failure" in result.results[0].warnings[0]
    assert result.results[1].offers


def test_demo_store_unreachable_fails_before_processing_rows(tmp_path):
    class OfflineDemoStore:
        key = "demo_store_http"
        label = "Averon Demo Store"
        search_calls = 0

        def stats(self):
            return {"reachable": False, "catalog_version": "unavailable", "error": "store offline"}

        def search(self, requested, *, limit=20):
            self.search_calls += 1
            raise AssertionError("search must not run after health precheck")

    provider = OfflineDemoStore()
    with pytest.raises(ValueError, match="store offline"):
        service_for(tmp_path, provider).search_project(rows()[:2])
    assert provider.search_calls == 0


def _wait_for(service: JobService, job_id: str):
    deadline = time.time() + 2
    while time.time() < deadline:
        current = service.get(job_id)
        if current.status in {"completed", "failed"}:
            return current
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_existing_job_service_exposes_completed_result_and_sanitized_failure():
    service = JobService(max_workers=1)
    try:
        completed = service.submit(lambda progress: {"positions_processed": 2})
        completed_job = _wait_for(service, completed.id)
        assert completed_job.public()["result"] == {"positions_processed": 2}
        failed = service.submit(lambda progress: (_ for _ in ()).throw(RuntimeError("authorization token leaked")))
        failed_job = _wait_for(service, failed.id)
        public = failed_job.public()
        assert public["status"] == "failed"
        assert "token" not in (public["error"] or "").casefold()
        assert "traceback" not in public
    finally:
        service.executor.shutdown(wait=True)


def test_project_sourcing_ui_polls_job_and_renders_completed_result():
    from pathlib import Path

    app_js = (Path(__file__).parents[1] / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const job = await api(url" in app_js
    assert "pollSourcingJob(job.id" in app_js
    assert "await api(`/api/jobs/${jobId}`)" in app_js
    assert "renderSourcingResult(job.result)" in app_js
