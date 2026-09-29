from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from averon_import.services.one_c_history.activity import (
    OneCHistoryActivityConflict,
    OneCHistoryActivityRegistry,
)
from averon_import.services.sourcing.models import SourcingSourceMode


def test_activity_registry_allows_shared_readers_and_fails_fast_for_writer_and_new_reader():
    registry = OneCHistoryActivityRegistry()
    first = registry.acquire_sourcing()
    second = registry.acquire_sourcing()
    assert registry.status() == {
        "active_sourcing_count": 2,
        "update_in_progress": False,
        "replacement_allowed": False,
    }
    with pytest.raises(OneCHistoryActivityConflict) as in_use:
        registry.try_begin_update()
    assert in_use.value.code == "ONE_C_HISTORY_IN_USE"
    assert in_use.value.active_sourcing_count == 2
    assert set(registry.status()) == {"active_sourcing_count", "update_in_progress", "replacement_allowed"}

    first.release()
    second.release()
    writer = registry.try_begin_update()
    with pytest.raises(OneCHistoryActivityConflict) as updating:
        registry.acquire_sourcing()
    assert updating.value.code == "ONE_C_HISTORY_UPDATING"
    with pytest.raises(OneCHistoryActivityConflict) as competing:
        registry.try_begin_update()
    assert competing.value.code == "ONE_C_HISTORY_UPDATE_IN_PROGRESS"
    writer.release()
    assert registry.status()["replacement_allowed"] is True


def test_threaded_sourcing_readers_hold_shared_leases_at_the_same_time():
    registry = OneCHistoryActivityRegistry()
    start = Barrier(3)
    acquired = Barrier(3)
    release = Event()

    def reader():
        start.wait(timeout=5)
        lease = registry.acquire_sourcing()
        try:
            acquired.wait(timeout=5)
            assert release.wait(timeout=5)
        finally:
            lease.release()

    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = [pool.submit(reader) for _ in range(2)]
        start.wait(timeout=5)
        acquired.wait(timeout=5)
        assert registry.status()["active_sourcing_count"] == 2
        with pytest.raises(OneCHistoryActivityConflict) as in_use:
            registry.try_begin_update()
        assert in_use.value.code == "ONE_C_HISTORY_IN_USE"
        release.set()
        for future in readers:
            future.result(timeout=5)

    assert registry.status()["active_sourcing_count"] == 0
    assert registry.status()["replacement_allowed"] is True


def test_threaded_reader_and_writer_attempts_fail_fast_while_update_lease_is_held():
    registry = OneCHistoryActivityRegistry()
    writer = registry.try_begin_update()
    start = Barrier(3)

    def attempt(operation):
        start.wait(timeout=5)
        try:
            lease = operation()
        except OneCHistoryActivityConflict as exc:
            return exc.code
        else:
            lease.release()
            return "unexpectedly-admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(attempt, registry.acquire_sourcing)
        second_writer = pool.submit(attempt, registry.try_begin_update)
        start.wait(timeout=5)
        assert {reader.result(timeout=5), second_writer.result(timeout=5)} == {
            "ONE_C_HISTORY_UPDATING",
            "ONE_C_HISTORY_UPDATE_IN_PROGRESS",
        }

    writer.release()
    reader = registry.acquire_sourcing()
    reader.release()
    next_writer = registry.try_begin_update()
    next_writer.release()
    assert registry.status() == {
        "active_sourcing_count": 0,
        "update_in_progress": False,
        "replacement_allowed": True,
    }


def _main_for_test(monkeypatch, tmp_path):
    monkeypatch.setenv("AVERON_DATA_DIR", str(tmp_path / "data"))
    from averon_import import main

    return main


def test_single_row_leases_release_on_success_and_error_and_provider_only_ignores_writer(monkeypatch, tmp_path):
    main = _main_for_test(monkeypatch, tmp_path)
    registry = OneCHistoryActivityRegistry()
    monkeypatch.setattr(main, "one_c_history_activity", registry)

    class FakeSourcingService:
        fail = False

        def search_row_routed(self, *_args, **_kwargs):
            if self.fail:
                raise ValueError("test failure")
            return {"ok": True}

    service = FakeSourcingService()
    monkeypatch.setattr(main, "sourcing_service", service)
    request = lambda mode: SimpleNamespace(source_mode=mode, row={}, provider=None, limit=20)

    assert main.sourcing_search(request(SourcingSourceMode.ONE_C_ONLY)) == {"ok": True}
    assert registry.status()["active_sourcing_count"] == 0
    service.fail = True
    with pytest.raises(HTTPException) as failed:
        main.sourcing_search(request(SourcingSourceMode.ONE_C_THEN_PROVIDER))
    assert failed.value.status_code == 400
    assert registry.status()["active_sourcing_count"] == 0

    service.fail = False
    writer = registry.try_begin_update()
    with pytest.raises(HTTPException) as blocked:
        main.sourcing_search(request(SourcingSourceMode.ONE_C_ONLY))
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "ONE_C_HISTORY_UPDATING"
    assert main.sourcing_search(request(SourcingSourceMode.PROVIDER_ONLY)) == {"ok": True}
    assert registry.status()["update_in_progress"] is True
    writer.release()


@pytest.mark.parametrize("mode", [SourcingSourceMode.ONE_C_ONLY, SourcingSourceMode.ONE_C_THEN_PROVIDER])
def test_project_job_holds_history_lease_while_queued_and_releases_after_worker(monkeypatch, tmp_path, mode):
    main = _main_for_test(monkeypatch, tmp_path)
    registry = OneCHistoryActivityRegistry()
    monkeypatch.setattr(main, "one_c_history_activity", registry)
    monkeypatch.setattr(main.one_c_history_repository, "catalog_version", lambda: "catalog-A")
    captured = {}

    class FakeSourcingService:
        def search_project_routed(self, rows, **kwargs):
            captured["version"] = main.one_c_history_repository.catalog_version()
            return FakeProject({
                "source_mode": mode.value,
                "catalog_version": "fallback-provider-version",
                "results": [
                    {"route": {"history_catalog_version": captured["version"]}},
                    {"route": {"history_catalog_version": captured["version"]}},
                ],
            })

    class FakeProject:
        def __init__(self, payload):
            self.payload = payload
            self.catalog_version = payload["catalog_version"]

        def model_dump(self, **_kwargs):
            return self.payload

    class FakeJob:
        def public(self):
            return {"id": "queued-job"}

    def submit(run):
        captured["run"] = run
        return FakeJob()

    monkeypatch.setattr(main, "sourcing_service", FakeSourcingService())
    monkeypatch.setattr(main.job_service, "submit", submit)
    job = main._submit_sourcing_project_job([{"row_type": "item"}], provider_key=None, limit=10, source_mode=mode)
    assert job == {"id": "queued-job"}
    assert registry.status()["active_sourcing_count"] == 1
    with pytest.raises(OneCHistoryActivityConflict):
        registry.try_begin_update()

    result = captured["run"](lambda *_args: None)
    assert result["results"][0]["route"]["history_catalog_version"] == "catalog-A"
    assert registry.status()["active_sourcing_count"] == 0


def test_project_mixed_snapshot_and_submit_failure_release_lease(monkeypatch, tmp_path):
    main = _main_for_test(monkeypatch, tmp_path)
    registry = OneCHistoryActivityRegistry()
    monkeypatch.setattr(main, "one_c_history_activity", registry)
    monkeypatch.setattr(main.one_c_history_repository, "catalog_version", lambda: "catalog-A")
    captured = {}

    class FakeSourcingService:
        def search_project_routed(self, *_args, **_kwargs):
            return FakeProject({"results": [
                {"route": {"history_catalog_version": "catalog-A"}},
                {"route": {"history_catalog_version": "catalog-B"}},
            ]})

    class FakeProject:
        catalog_version = "catalog-A"

        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, **_kwargs):
            return self.payload

    monkeypatch.setattr(main, "sourcing_service", FakeSourcingService())
    def capture(run):
        captured["run"] = run
        return SimpleNamespace(public=lambda: {"id": "queued-job"})

    monkeypatch.setattr(main.job_service, "submit", capture)
    main._submit_sourcing_project_job([{"row_type": "item"}], provider_key=None, limit=10, source_mode=SourcingSourceMode.ONE_C_ONLY)
    with pytest.raises(RuntimeError, match="mixed 1C history snapshots"):
        captured["run"](lambda *_args: None)
    assert registry.status()["active_sourcing_count"] == 0

    monkeypatch.setattr(main.job_service, "submit", lambda _run: (_ for _ in ()).throw(RuntimeError("submit failed")))
    with pytest.raises(RuntimeError, match="submit failed"):
        main._submit_sourcing_project_job([{"row_type": "item"}], provider_key=None, limit=10, source_mode=SourcingSourceMode.ONE_C_ONLY)
    assert registry.status()["active_sourcing_count"] == 0


def test_project_history_version_guard_rejects_mixed_and_unexpected_versions(monkeypatch, tmp_path):
    main = _main_for_test(monkeypatch, tmp_path)
    _assert_project_history_version = main._assert_project_history_version

    _assert_project_history_version({"results": [{"route": {"history_catalog_version": "v1"}}]}, "v1")
    with pytest.raises(RuntimeError, match="mixed 1C history snapshots"):
        _assert_project_history_version({"results": [
            {"route": {"history_catalog_version": "v1"}},
            {"route": {"history_catalog_version": "v2"}},
        ]}, "v1")
