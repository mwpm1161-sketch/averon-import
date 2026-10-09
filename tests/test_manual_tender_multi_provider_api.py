from __future__ import annotations

import json
import socket
import threading

import pytest

from averon_import.services.jobs import SOURCING
from test_manual_tender_multi_provider_orchestration import Adapter, ETM, LEMANA, _service
from test_manual_tender_xlsx import (
    _api_request,
    _auth_headers,
    _confirm_synthetic_tender,
    _persist_price_export_run,
    _post_tender_sourcing,
    _wait_tender_job,
    tender_api,
)


class Runtime:
    def __init__(self, service, keys=(ETM, LEMANA)):
        self.service = service
        self._keys = tuple(keys)

    @property
    def execution_provider_keys(self):
        return self._keys


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    original = socket.socket.connect

    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original(sock, address)
        raise AssertionError("M4A API tests must not contact external services")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("external network is forbidden")))


def _runtime(monkeypatch, main, *, keys=(ETM, LEMANA), adapters=None):
    adapters = adapters or (Adapter(ETM, amount="1234.5600"), Adapter(LEMANA, amount="1234.5600"))
    service = _service(*adapters)
    monkeypatch.setattr(main, "sourcing_runtime", Runtime(service, keys))
    return service, adapters


def _payload(workspace, **updates):
    row_id = next(row["source_row_id"] for row in workspace["rows"] if row["row_type"] == "item")
    value = {"source_row_ids": [row_id], "source_mode": "provider_only", "providers": [LEMANA, ETM], "limit": 20}
    value.update(updates)
    return value


def test_m4a_capabilities_are_authenticated_local_and_intersect_registered_runtime(tender_api, monkeypatch):
    main, _ = tender_api
    _, adapters = _runtime(monkeypatch, main, keys=("local_catalog", ETM, LEMANA))
    endpoint = "/api/manual-tenders/sourcing/multi-provider-capabilities"

    assert _api_request(main.app, "GET", endpoint).status_code == 401
    response = _api_request(main.app, "GET", endpoint, headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "source_mode": "provider_only",
        "providers": [{"key": ETM, "label": "ЭТМ iPRO"}, {"key": LEMANA, "label": "Лемана ПРО"}],
        "min_providers": 2,
        "max_providers": 2,
        "max_rows": 100,
        "max_result_limit": 20,
        "export_supported": False,
    }
    assert all(not adapter.calls for adapter in adapters)

    monkeypatch.setattr(main, "sourcing_runtime", Runtime(main.sourcing_runtime.service, (ETM, "local_catalog")))
    unavailable = _api_request(main.app, "GET", endpoint, headers=_auth_headers()).json()
    assert unavailable["enabled"] is False
    assert unavailable["providers"] == [{"key": ETM, "label": "ЭТМ iPRO"}]


@pytest.mark.parametrize(
    "updates,expected_status",
    [
        ({"providers": None}, 400),
        ({"providers": [ETM]}, 400),
        ({"providers": [ETM, ETM]}, 400),
        ({"providers": [ETM, "not-a-provider"]}, 400),
        ({"providers": [ETM, "local_catalog"]}, 400),
        ({"providers": [ETM, LEMANA], "source_mode": "one_c_only"}, 400),
        ({"providers": [ETM, LEMANA], "provider": ETM}, 400),
        ({"providers": [ETM, LEMANA], "limit": 21}, 400),
        ({"providers": [ETM, 123]}, 422),
        ({"source_row_ids": ["invalid-row-id"]}, 422),
    ],
)
def test_m4a_invalid_handshakes_are_rejected_before_jobs_or_provider_calls(
    tender_api, tmp_path, monkeypatch, updates, expected_status,
):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    service, adapters = _runtime(monkeypatch, main)
    initial_jobs = set(main.job_service.jobs)

    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(workspace, **updates))
    assert response.status_code == expected_status, response.text
    assert set(main.job_service.jobs) == initial_jobs
    assert all(not adapter.calls for adapter in adapters)
    assert service.provider_runner is not None


def test_m4a_unavailable_provider_is_rejected_before_job_and_runner(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _, adapters = _runtime(monkeypatch, main, keys=(ETM,))
    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(workspace))
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MULTI_PROVIDER_UNAVAILABLE"
    assert not main.job_service.jobs
    assert all(not adapter.calls for adapter in adapters)


def test_m4a_101_selected_rows_are_rejected_without_truncation_or_runner_calls(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=101)
    _, adapters = _runtime(monkeypatch, main)
    row_ids = [row["source_row_id"] for row in workspace["rows"] if row["row_type"] == "item"]
    assert len(row_ids) == 101
    response = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": row_ids, "source_mode": "provider_only", "providers": [ETM, LEMANA], "limit": 20,
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MULTI_PROVIDER_ROW_LIMIT"
    assert not main.job_service.jobs
    assert all(not adapter.calls for adapter in adapters)


def test_m4a_exactly_100_selected_rows_are_admitted_and_processed(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=100)
    _, adapters = _runtime(monkeypatch, main)
    row_ids = [row["source_row_id"] for row in workspace["rows"] if row["row_type"] == "item"]
    response = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": row_ids, "source_mode": "provider_only", "providers": [ETM, LEMANA], "limit": 20,
    })
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"], timeout=8)
    assert job["status"] == "completed", job
    assert job["result"]["positions_total"] == job["result"]["positions_processed"] == 100
    assert len(adapters[0].calls) == len(adapters[1].calls) == 100
    assert job["current"] == job["total"] == 100


def test_m4a_failed_v2_is_a_completed_job_with_safe_terminal_detail(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=2)
    service, _ = _runtime(monkeypatch, main)
    original = service.evaluate_provider_execution
    calls = 0

    def fail_second_row(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("secret token must not appear in public failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "evaluate_provider_execution", fail_second_row)
    response = _post_tender_sourcing(main, workspace["tender_id"], {
        "source_row_ids": [row["source_row_id"] for row in workspace["rows"] if row["row_type"] == "item"],
        "source_mode": "provider_only", "providers": [ETM, LEMANA], "limit": 20,
    })
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "completed"
    assert job["result"]["run_status"] == "failed"
    assert job["result"]["failure_code"]
    assert job["result"]["positions_total"] == 2
    assert job["result"]["positions_processed"] == 1
    run_id = job["result"]["run_id"]
    detail = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}", headers=_auth_headers())
    assert detail.status_code == 200
    assert detail.json()["status"] == "failed"
    assert len(detail.json()["rows"]) == 1
    assert "secret token" not in detail.text
    listed = _api_request(main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs", headers=_auth_headers()).json()["runs"]
    assert listed[0]["run_id"] == run_id and listed[0]["status"] == "failed"


def test_m4a_post_job_v2_detail_mixed_list_and_legacy_export_boundary(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    started, release = threading.Event(), threading.Event()
    wait_for_release = lambda _intent: (started.set(), release.wait(4))
    service, adapters = _runtime(monkeypatch, main, adapters=(
        Adapter(ETM, amount="1234.5600", hook=wait_for_release),
        Adapter(LEMANA, amount="1234.5600", hook=wait_for_release),
    ))
    # A different legacy alias proves this branch uses one captured runtime generation.
    monkeypatch.setattr(main, "sourcing_service", object())
    request = _payload(workspace)
    first = _post_tender_sourcing(main, workspace["tender_id"], request)
    assert first.status_code == 202, first.text
    assert started.wait(2), "the fake outcome-native adapter did not start"
    reversed_request = {**request, "providers": [ETM, LEMANA]}
    duplicate = _post_tender_sourcing(main, workspace["tender_id"], reversed_request)
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == first.json()["id"]
    assert repository.activity.active(workspace["tender_id"])  # execution owns the lease
    release.set()

    job = _wait_tender_job(main, first.json()["id"])
    assert job["status"] == "completed", job
    compact = job["result"]
    assert compact["schema_version"] == 2 and compact["run_kind"] == "multi_provider"
    assert compact["providers"] == [ETM, LEMANA]
    assert compact["run_status"] == "completed"
    assert "rows" not in compact and "offers" not in compact
    assert len(adapters[0].calls) == len(adapters[1].calls) == 1
    assert service.provider_runner is main.sourcing_runtime.service.provider_runner

    tender_id = workspace["tender_id"]
    list_response = _api_request(main.app, "GET", f"/api/manual-tenders/{tender_id}/runs", headers=_auth_headers())
    assert list_response.status_code == 200
    v2_summary = list_response.json()["runs"][0]
    assert v2_summary["run_id"] == compact["run_id"]
    assert v2_summary["schema_version"] == 2 and v2_summary["run_kind"] == "multi_provider"
    assert v2_summary["providers"] == [ETM, LEMANA]
    assert "source_sha256" not in v2_summary and "failure" not in v2_summary

    detail_response = _api_request(main.app, "GET", f"/api/manual-tenders/{tender_id}/runs/{compact['run_id']}", headers=_auth_headers())
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["status"] == "completed"
    row = detail["rows"][0]
    assert row["provider_outcomes"]
    offers = row["offers"]
    assert len(offers) == 2
    assert {offer["offer_id"] for offer in offers} == {"123"}
    assert {offer["provider_key"] for offer in offers} == {ETM, LEMANA}
    assert all(offer["price"] == "1234.56" for offer in offers), [offer["price"] for offer in offers]
    assert row["commercial_selection"]["state"] == "NO_SAFE_WINNER"
    serialized = json.dumps(detail, ensure_ascii=False)
    for forbidden in ("source_sha256", "workspace_revision", "provenance", "affinity", "config_revision", "adapter_revision", "request_count", "raw_payload", "must-never-persist", "Bearer"):
        assert forbidden not in serialized

    source_row = next(row for row in workspace["rows"] if row["row_type"] == "item")
    v1_id = _persist_price_export_run(main, repository, workspace, source_row)
    mixed = _api_request(main.app, "GET", f"/api/manual-tenders/{tender_id}/runs", headers=_auth_headers()).json()["runs"]
    assert {item["run_id"] for item in mixed} >= {v1_id, compact["run_id"]}
    v1_summary = next(item for item in mixed if item["run_id"] == v1_id)
    assert "schema_version" not in v1_summary and "run_kind" not in v1_summary
    expected_v1 = main.tender_sourcing_runs.get_public(repository.workspace_root / tender_id, tender_id, v1_id)
    v1_detail = _api_request(main.app, "GET", f"/api/manual-tenders/{tender_id}/runs/{v1_id}", headers=_auth_headers())
    assert v1_detail.status_code == 200 and v1_detail.json() == expected_v1

    jobs_before = set(main.job_service.jobs)
    unsupported_export = _api_request(
        main.app, "POST", f"/api/manual-tenders/{tender_id}/runs/{compact['run_id']}/export",
        headers={**_auth_headers(), "Content-Type": "application/json"}, body=b"{}",
    )
    assert unsupported_export.status_code != 202
    assert set(main.job_service.jobs) == jobs_before


def test_m4a_queued_job_revalidates_snapshot_without_holding_a_workspace_lease(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _, adapters = _runtime(monkeypatch, main)
    started, release = threading.Event(), threading.Event()

    def block(_progress):
        started.set()
        release.wait(4)
        return {"blocked": True}

    blocker = main.job_service.submit(block, lane=SOURCING, kind="m4a-test-blocker", owner_id="tender-user")
    assert started.wait(2)
    queued = _post_tender_sourcing(main, workspace["tender_id"], _payload(workspace))
    assert queued.status_code == 202, queued.text
    queued_job = main.job_service.get_public(queued.json()["id"], owner_id="tender-user")
    assert queued_job["status"] == "queued"
    assert not repository.activity.active(workspace["tender_id"])

    path, metadata = repository.workspace_path(workspace["tender_id"], "username:tender-user")
    metadata["rows"][0]["name"] += " изменено после постановки в очередь"
    repository._atomic_json(path / "workspace.json", metadata)
    release.set()
    _wait_tender_job(main, blocker.id)
    failed = _wait_tender_job(main, queued.json()["id"])
    assert failed["status"] == "failed"
    assert all(not adapter.calls for adapter in adapters)
    assert not (path / "runs").exists()
    assert not repository.activity.active(workspace["tender_id"])
