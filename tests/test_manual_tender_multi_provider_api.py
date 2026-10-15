from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from averon_import.services.app_settings import AppSettings
from averon_import.services.jobs import SOURCING
from averon_import.services.secrets import MemorySecretStore, VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED
from test_manual_tender_multi_provider_orchestration import Adapter, ETM, LEMANA, VI, VI_REGION, _service
from averon_import.services.sourcing.providers.execution import ProviderRunner
from averon_import.services.sourcing.providers.contracts import ProviderSearchState
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
    def __init__(self, service, keys=(ETM, LEMANA), configured=(ETM, LEMANA), vi_generation="a" * 32):
        self.service = service
        self._keys = tuple(keys)
        self.execution_providers = service.provider_runner._registry
        if VI in self._keys:
            adapter = self.execution_providers[VI]
            adapter.credential_generation = vi_generation
            adapter._affinity = lambda: SimpleNamespace(
                config_revision=hashlib.sha256(vi_generation.encode("ascii")).hexdigest(),
                region_id=VI_REGION,
            )
        self.providers = {
            key: type("ProviderConfiguration", (), {"configured": key in configured})()
            for key in (ETM, LEMANA)
        }

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


def _runtime(
    monkeypatch, main, *, keys=(ETM, LEMANA), configured=(ETM, LEMANA), adapters=None,
    vi_generation="a" * 32,
):
    adapters = adapters or (Adapter(ETM, amount="1234.5600"), Adapter(LEMANA, amount="1234.5600"))
    service = _service(*adapters)
    monkeypatch.setattr(main, "sourcing_runtime", Runtime(service, keys, configured, vi_generation))
    return service, adapters


def _payload(workspace, **updates):
    row_id = next(row["source_row_id"] for row in workspace["rows"] if row["row_type"] == "item")
    value = {"source_row_ids": [row_id], "source_mode": "provider_only", "providers": [LEMANA, ETM], "limit": 20}
    value.update(updates)
    return value


def _enable_vi_public_configuration(monkeypatch, main, generation="a" * 32):
    settings = AppSettings.model_validate({"sourcing": {"vseinstrumenti": {
        "enabled": True, "region_id": VI_REGION, "credential_generation": generation,
    }}})
    store = MemorySecretStore()
    store.set(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED, generation)
    monkeypatch.setattr(main, "app_settings_service", SimpleNamespace(settings=settings))
    monkeypatch.setattr(main, "secret_store", store)


TSTN = "tstn"


class FakeTstnPreparation:
    def __init__(self, adapter):
        self.adapter = adapter
        self.adapter.credential_generation = "c" * 32
        self.adapter._catalog_revision = lambda: "d" * 64
        self.readiness = SimpleNamespace(configured=True, catalog_state="ready", live_verified=False)
        self.private_runner = ProviderRunner({TSTN: adapter})
        self.runner_creations = 0

    def create_private_provider_runner(self):
        self.runner_creations += 1
        return self.private_runner


def _attach_fake_tstn_runtime(monkeypatch, main, runtime, *, gate=True, adapter=None):
    monkeypatch.setattr(main, "TSTN_PUBLIC_LIVE_EXECUTION_ENABLED", gate)
    preparation = FakeTstnPreparation(adapter or Adapter(TSTN, state=ProviderSearchState.EMPTY, requests=0))
    runtime._tstn_runtime = preparation
    monkeypatch.setattr(main, "sourcing_runtime", runtime)
    return preparation


def test_m5d_vi_credential_rotation_breaks_active_multi_provider_dedupe(
    tender_api, tmp_path, monkeypatch,
):
    main, repository = tender_api
    generation_a, generation_b = "a" * 32, "b" * 32
    _enable_vi_public_configuration(monkeypatch, main, generation_a)
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    started, release = threading.Event(), threading.Event()

    def block_first_generation(_intent):
        started.set()
        assert release.wait(5), "test did not release the first-generation fake adapter"

    first_adapters = (Adapter(ETM, hook=block_first_generation), Adapter(VI, hook=block_first_generation))
    _runtime(
        monkeypatch, main, keys=(ETM, VI), configured=(ETM,), adapters=first_adapters,
        vi_generation=generation_a,
    )
    payload = _payload(workspace, providers=[ETM, VI])

    first = _post_tender_sourcing(main, workspace["tender_id"], payload)
    assert first.status_code == 202, first.text
    first_job_id = first.json()["id"]
    try:
        assert started.wait(2), "first fake provider execution did not start"
        same_generation = _post_tender_sourcing(main, workspace["tender_id"], payload)
        assert same_generation.status_code == 202, same_generation.text
        assert same_generation.json()["id"] == first_job_id

        # Simulate a completed local credential rotation without reading or
        # storing a bearer token; both generations are test-only markers.
        _enable_vi_public_configuration(monkeypatch, main, generation_b)
        _runtime(
            monkeypatch, main, keys=(ETM, VI), configured=(ETM,),
            adapters=(Adapter(ETM), Adapter(VI)), vi_generation=generation_b,
        )
        rotated = _post_tender_sourcing(main, workspace["tender_id"], payload)
        assert rotated.status_code in {202, 409}, rotated.text
        if rotated.status_code == 202:
            assert rotated.json()["id"] != first_job_id
        else:
            assert rotated.json()["detail"]["code"] == "JOB_LANE_BUSY"
    finally:
        release.set()

    assert _wait_tender_job(main, first_job_id)["status"] == "completed"
    if rotated.status_code == 202:
        assert _wait_tender_job(main, rotated.json()["id"])["status"] == "completed"


def test_m4a_capabilities_are_authenticated_local_and_intersect_registered_runtime(tender_api, monkeypatch):
    main, _ = tender_api
    _, adapters = _runtime(monkeypatch, main, keys=("local_catalog", ETM, LEMANA))
    endpoint = "/api/manual-tenders/sourcing/multi-provider-capabilities"

    assert _api_request(main.app, "GET", endpoint).status_code == 401
    response = _api_request(main.app, "GET", endpoint, headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "enabled": True,
        "source_mode": "provider_only",
        "providers": [{"key": ETM, "label": "ЭТМ iPRO"}],
        "options": [
            {"key": ETM, "label": "ЭТМ iPRO", "description": "Каталог, цены и склады iPRO.", "available": True, "reason": ""},
            {"key": VI, "label": "ВсеИнструменты.ru", "description": "Поиск по каталогу ВсеИнструменты.ru.", "available": False, "reason": "Поставщик пока не настроен локально."},
            {"key": "tstn", "label": "ТЕХНОНИКОЛЬ", "description": "Локальный каталог; онлайн-поиск закрыт до отдельного разрешения.", "available": False, "reason": "Онлайн-поиск ТСТН пока отключён администратором."},
        ],
        "min_providers": 1,
        "max_providers": 3,
        "max_rows": 100,
        "max_result_limit": 20,
        "export_supported": True,
    }
    assert all(not adapter.calls for adapter in adapters)

    monkeypatch.setattr(main, "sourcing_runtime", Runtime(main.sourcing_runtime.service, (ETM, "local_catalog")))
    unavailable = _api_request(main.app, "GET", endpoint, headers=_auth_headers()).json()
    assert unavailable["enabled"] is True
    assert unavailable["providers"] == [{"key": ETM, "label": "ЭТМ iPRO"}]


def test_m5d_capabilities_include_registered_vi_but_hide_unconfigured_lemana(
    tender_api, monkeypatch,
):
    main, _ = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    adapters = (Adapter(ETM), Adapter(LEMANA), Adapter(VI))
    _, runtime_adapters = _runtime(
        monkeypatch, main, keys=(ETM, LEMANA, VI), configured=(ETM,), adapters=adapters,
    )

    response = _api_request(
        main.app, "GET", "/api/manual-tenders/sourcing/multi-provider-capabilities", headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["providers"] == [
        {"key": ETM, "label": "ЭТМ iPRO"},
        {"key": VI, "label": "ВсеИнструменты.ru"},
    ]
    assert payload["min_providers"] == 1 and payload["max_providers"] == 3
    assert payload["options"][-1]["key"] == "tstn" and payload["options"][-1]["available"] is False
    assert all(not adapter.calls for adapter in runtime_adapters)


def test_tstn_public_selection_stays_blocked_when_server_gate_is_off(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _, adapters = _runtime(monkeypatch, main, keys=(ETM,), adapters=(Adapter(ETM),))
    preparation = _attach_fake_tstn_runtime(monkeypatch, main, main.sourcing_runtime, gate=False)

    capabilities = _api_request(
        main.app, "GET", "/api/manual-tenders/sourcing/multi-provider-capabilities", headers=_auth_headers(),
    ).json()
    tstn_option = next(item for item in capabilities["options"] if item["key"] == TSTN)
    assert tstn_option["available"] is False
    assert "отключён" in tstn_option["reason"]

    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(workspace, providers=[TSTN]))
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MULTI_PROVIDER_UNAVAILABLE"
    assert not main.job_service.jobs
    assert preparation.runner_creations == 0
    assert not preparation.adapter.calls and all(not adapter.calls for adapter in adapters)


def test_tstn_admin_readiness_is_local_and_credential_value_free(tender_api, monkeypatch):
    main, _ = tender_api
    _, _adapters = _runtime(monkeypatch, main, keys=(ETM,), adapters=(Adapter(ETM),))
    preparation = _attach_fake_tstn_runtime(monkeypatch, main, main.sourcing_runtime, gate=False)
    preparation.readiness.live_verified = True

    response = _api_request(
        main.app, "GET", "/api/sourcing/providers/tstn/readiness", headers=_auth_headers("tender-admin"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["credentials_configured"] is True
    assert payload["catalog_state"] == "ready"
    assert payload["live_gate_enabled"] is False and payload["live_verified"] is True
    assert "token" not in response.text.casefold() and "bearer" not in response.text.casefold()
    assert preparation.runner_creations == 0 and not preparation.adapter.calls


def test_tstn_three_provider_excel_run_is_request_local_and_preserves_independent_outcomes(
    tender_api, tmp_path, monkeypatch,
):
    main, repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    service, adapters = _runtime(
        monkeypatch, main, keys=(ETM, VI), configured=(ETM,),
        adapters=(Adapter(ETM, amount="80"), Adapter(VI, state=ProviderSearchState.EMPTY, requests=0)),
    )
    tstn_adapter = Adapter(TSTN, state=ProviderSearchState.EMPTY, requests=0)
    preparation = _attach_fake_tstn_runtime(
        monkeypatch, main, main.sourcing_runtime, gate=True, adapter=tstn_adapter,
    )

    capabilities = _api_request(
        main.app, "GET", "/api/manual-tenders/sourcing/multi-provider-capabilities", headers=_auth_headers(),
    ).json()
    assert {item["key"] for item in capabilities["providers"]} == {ETM, VI, TSTN}
    assert preparation.runner_creations == 0 and not tstn_adapter.calls

    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(
        workspace, providers=[TSTN, VI, ETM],
    ))
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "completed", job
    run_id = job["result"]["run_id"]
    detail = _api_request(
        main.app,
        "GET",
        f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}",
        headers=_auth_headers(),
    )
    assert detail.status_code == 200, detail.text
    row = detail.json()["rows"][0]
    assert {item["provider_key"] for item in row["provider_outcomes"]} == {ETM, VI, TSTN}
    assert next(item for item in row["provider_outcomes"] if item["provider_key"] == TSTN)["state"] == "empty"
    assert row["commercial_selection"]["state"] == "SELECTED"
    assert row["commercial_selection"]["selected_reference"]["provider_key"] == ETM
    assert [len(adapter.calls) for adapter in adapters] == [1, 1]
    assert len(tstn_adapter.calls) == 1
    assert preparation.runner_creations == 1
    serialized = detail.text.casefold()
    assert "credential_generation" not in serialized and "bearer" not in serialized


@pytest.mark.parametrize("providers", [[ETM], [ETM, VI]])
def test_shared_single_row_selection_accepts_one_or_two_primary_providers(
    tender_api, monkeypatch, providers,
):
    main, _repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    service, adapters = _runtime(
        monkeypatch, main, keys=(ETM, VI), configured=(ETM,),
        adapters=(Adapter(ETM), Adapter(VI, amount="120")),
    )
    row = {"id": "manual-row-1", "row_type": "item", "name": "Синтетическая позиция", "article": "A-1",
           "model": "X", "quantity": "1", "unit": "шт"}
    response = _api_request(
        main.app, "POST", "/api/sourcing/search", headers={**_auth_headers(), "Content-Type": "application/json"},
        body=json.dumps({"row": row, "providers": providers, "source_mode": "provider_only", "limit": 20}).encode(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {item["provider_key"] for item in payload["provider_outcomes"]} == set(providers)
    assert all(not item["provider_key"].startswith("lemana") for item in payload["provider_outcomes"])
    assert service.provider_runner is main.sourcing_runtime.service.provider_runner
    assert [len(adapter.calls) for adapter in adapters] == [1, int(VI in providers)]


def test_document_project_selection_carries_provider_keys_into_shared_sourcing(tender_api, tmp_path, monkeypatch):
    from types import SimpleNamespace

    main, _repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    _service_instance, adapters = _runtime(
        monkeypatch, main, keys=(ETM, VI), configured=(ETM,),
        adapters=(Adapter(ETM), Adapter(VI, state=ProviderSearchState.EMPTY, requests=0)),
    )
    tstn_adapter = Adapter(TSTN, state=ProviderSearchState.EMPTY, requests=0)
    preparation = _attach_fake_tstn_runtime(
        monkeypatch, main, main.sourcing_runtime, gate=True, adapter=tstn_adapter,
    )
    document_id = "e" * 32
    document_workspace = SimpleNamespace(
        result_path=tmp_path / "document-result.json",
        sourcing_runs_dir=tmp_path / "document-sourcing-runs",
    )
    monkeypatch.setattr(main, "workspace_service", SimpleNamespace(
        get=lambda requested: document_workspace if requested == document_id else (_ for _ in ()).throw(FileNotFoundError()),
        read_json=lambda _path, default=None: {"revision": 4},
    ))
    request = main.SourcingProjectRequest(
        rows=[{"id": "pdf-row-1", "row_type": "item", "name": "Синтетическая позиция", "quantity": "1", "unit": "шт"}],
        source_mode="provider_only", providers=[ETM, VI, TSTN], limit=20,
    )

    admitted = main.document_sourcing_search_all(document_id, request, user=None)
    deadline = time.monotonic() + 3
    job = None
    while time.monotonic() < deadline:
        job = main.job_service.get(admitted["id"])
        if job.status in {"completed", "failed", "expired"}:
            break
        time.sleep(0.01)
    assert job is not None and job.status == "completed", getattr(job, "traceback", None)
    result = job.public()["result"]
    assert result["provider_keys"] == sorted([ETM, VI, TSTN])
    assert {item["provider_key"] for item in result["results"][0]["provider_outcomes"]} == {ETM, VI, TSTN}
    assert [len(adapter.calls) for adapter in adapters] == [1, 1]
    assert len(tstn_adapter.calls) == preparation.runner_creations == 1


def test_m5d_capabilities_hide_vi_when_generation_marker_is_missing(tender_api, tmp_path, monkeypatch):
    main, repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    main.secret_store.delete(VSEINSTRUMENTI_BEARER_TOKEN_CONFIGURED)
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    adapters = (Adapter(ETM), Adapter(LEMANA), Adapter(VI))
    _runtime(monkeypatch, main, keys=(ETM, LEMANA, VI), adapters=adapters)

    response = _api_request(
        main.app, "GET", "/api/manual-tenders/sourcing/multi-provider-capabilities", headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert VI not in {provider["key"] for provider in response.json()["providers"]}
    rejected = _post_tender_sourcing(main, workspace["tender_id"], _payload(workspace, providers=[ETM, VI]))
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "MULTI_PROVIDER_UNAVAILABLE"
    assert not main.job_service.jobs
    assert all(not adapter.calls for adapter in adapters)


def test_m5d_public_selection_runs_vi_outcome_only_and_projects_safe_provider_facts(
    tender_api, tmp_path, monkeypatch,
):
    main, repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    adapters = (Adapter(ETM, amount="100"), Adapter(LEMANA, amount="90"), Adapter(VI, amount="80"))
    service, runtime_adapters = _runtime(
        monkeypatch, main, keys=(ETM, LEMANA, VI), adapters=adapters,
    )

    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(
        workspace, providers=[VI, ETM],
    ))
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"])
    assert job["status"] == "completed", job
    assert job["result"]["providers"] == [ETM, VI]
    assert [len(adapter.calls) for adapter in runtime_adapters] == [1, 0, 1]

    detail = _api_request(
        main.app,
        "GET",
        f"/api/manual-tenders/{workspace['tender_id']}/runs/{job['result']['run_id']}",
        headers=_auth_headers(),
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert {item["provider_key"] for item in payload["rows"][0]["provider_outcomes"]} == {ETM, VI}
    assert {item["provider_key"] for item in payload["rows"][0]["offers"]} == {ETM, VI}
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("config_revision", "credential_generation", "Bearer", "must-not-be-retained-or-public"):
        assert forbidden not in serialized
    assert service.provider_runner is main.sourcing_runtime.service.provider_runner


@pytest.mark.parametrize(
    "updates,expected_status",
    [
        ({"providers": None}, 400),
        ({"providers": [ETM, "tstn"]}, 400),
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
        "source_row_ids": row_ids, "source_mode": "provider_only", "providers": [ETM], "limit": 20,
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
        "source_row_ids": row_ids, "source_mode": "provider_only", "providers": [ETM], "limit": 20,
    })
    assert response.status_code == 202, response.text
    job = _wait_tender_job(main, response.json()["id"], timeout=8)
    assert job["status"] == "completed", job
    assert job["result"]["positions_total"] == job["result"]["positions_processed"] == 100
    assert len(adapters[0].calls) == 100 and not adapters[1].calls
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
    no_safe_price_export = _api_request(
        main.app, "POST", f"/api/manual-tenders/{tender_id}/runs/{compact['run_id']}/export",
        headers={**_auth_headers(), "Content-Type": "application/json"}, body=b"{}",
    )
    assert no_safe_price_export.status_code == 409, no_safe_price_export.text
    assert no_safe_price_export.json()["detail"]["code"] == "TENDER_EXPORT_NO_ELIGIBLE_PRICES"
    assert set(main.job_service.jobs) == jobs_before


def test_tender_v2_export_accepts_only_the_commercially_selected_etm_price(
    tender_api, tmp_path, monkeypatch,
):
    main, repository = tender_api
    _enable_vi_public_configuration(monkeypatch, main)
    workspace = _confirm_synthetic_tender(main, repository, tmp_path, count=1)
    _service_instance, adapters = _runtime(
        monkeypatch, main, keys=(ETM, VI), configured=(ETM,),
        adapters=(Adapter(ETM, amount="80"), Adapter(VI, state=ProviderSearchState.EMPTY, requests=0)),
    )
    tstn_adapter = Adapter(TSTN, state=ProviderSearchState.EMPTY, requests=0)
    _attach_fake_tstn_runtime(monkeypatch, main, main.sourcing_runtime, gate=True, adapter=tstn_adapter)
    response = _post_tender_sourcing(main, workspace["tender_id"], _payload(
        workspace, providers=[ETM, VI, TSTN],
    ))
    assert response.status_code == 202, response.text
    completed = _wait_tender_job(main, response.json()["id"])
    assert completed["status"] == "completed", completed
    run_id = completed["result"]["run_id"]
    detail = _api_request(
        main.app, "GET", f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}", headers=_auth_headers(),
    ).json()
    selected = detail["rows"][0]["commercial_selection"]["selected_reference"]
    assert selected == {"provider_key": ETM, "offer_id": "123"}

    export = _api_request(
        main.app, "POST", f"/api/manual-tenders/{workspace['tender_id']}/runs/{run_id}/export",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        body=b'{"include_historical_prices":false,"allow_partial":false}',
    )
    assert export.status_code == 202, export.text
    exported = _wait_tender_job(main, export.json()["id"])
    assert exported["status"] == "completed", exported
    assert exported["result"]["priced_count"] == 1
    assert exported["result"]["blank_count"] == 0
    assert len(adapters[0].calls) == len(adapters[1].calls) == len(tstn_adapter.calls) == 1


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
