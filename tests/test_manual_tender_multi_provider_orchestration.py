from __future__ import annotations

import hashlib
import os
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import Workbook

import averon_import.services.manual_tenders.multi_provider_orchestration as orchestration
from averon_import.services.manual_tenders.durable_projection import (
    DurableProjectionCode, DurableProjectionError, encode_tender_sourcing_run_v2,
)
from averon_import.services.manual_tenders.durable_read import MAX_DURABLE_V2_BYTES
from averon_import.services.manual_tenders.durable_storage import DurableV2StorageCode, DurableV2StorageError
from averon_import.services.manual_tenders.parser import TenderWorkbookParser
from averon_import.services.manual_tenders.repository import TenderWorkspaceError, TenderWorkspaceRepository
from averon_import.services.manual_tenders.sourcing import TenderSourcingRunStore
from averon_import.services.sourcing.models import MatchDecision, Offer, SourcingSourceMode
from averon_import.services.sourcing.providers.contracts import (
    ProviderFailureCategory, ProviderSearchOutcome, ProviderSearchRequestIdentity, ProviderSearchState, ProviderSelection,
)
from averon_import.services.sourcing.providers.execution import ProviderRunner
from averon_import.services.sourcing.service import SourcingService
from test_manual_tender_xlsx import tender_api  # noqa: F401: reuse isolated public-route fixture

ETM, LEMANA, LOCAL = "etm_ipro", "lemana_b2b", "local_catalog"
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
Code = orchestration.MultiProviderOrchestrationCode


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("M3D tests must never contact suppliers")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    original = socket.socket.connect
    def guarded(sock, address):
        # Windows asyncio uses a loopback socketpair even for in-process ASGI.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original(sock, address)
        return forbidden()
    monkeypatch.setattr(socket.socket, "connect", guarded)


def _workspace(tmp_path, count=1, *, identical=False, section=False, article="A-1", model="X"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Наименование", "Количество", "Ед. изм.", "Артикул", "Производитель", "Модель"])
    for index in range(count):
        sheet.append(["Valve" if identical else f"Valve {index}", 2, "шт", article, "", model])
    if section:
        sheet.append(["Раздел", None, None, None, None, None])
    source = tmp_path / "input.xlsx"
    workbook.save(source)
    workbook.close()
    payload = source.read_bytes()
    repository = TenderWorkspaceRepository(tmp_path / "data")
    preview = repository.reserve_preview("owner", "input.xlsx", len(payload), hashlib.sha256(payload).hexdigest())
    repository.write_preview_source(preview["preview_id"], payload)
    analysis = TenderWorkbookParser().parse(repository.preview_root / preview["preview_id"] / "source.xlsx", tender_id=preview["preview_id"])
    repository.update_preview(preview["preview_id"], "owner", status="ready", analysis=analysis)
    confirmed = repository.confirm(preview["preview_id"], "owner")
    snapshot = orchestration.capture_multi_provider_tender_snapshot(repository, confirmed["tender_id"], "owner")
    ids = tuple(row["source_row_id"] for row in confirmed["rows"] if row["row_type"] == "item")
    assert len(ids) == count
    return repository, TenderSourcingRunStore(repository), snapshot, ids


class Adapter:
    """Outcome-native stub: real contracts and observable runner accounting."""
    def __init__(self, key, *, amount="100", state=ProviderSearchState.SUCCESS, requests=1, weaker=False,
                 invalid=False, review=False, reject=False, equivalent=False, hook=None,
                 second_amount=None, second_currency="RUB", representative=False):
        self.key, self.amount, self.state, self.requests = key, amount, state, requests
        self.weaker, self.invalid, self.review, self.reject = weaker, invalid, review, reject
        self.equivalent, self.hook, self.calls = equivalent, hook, []
        self.second_amount, self.second_currency, self.representative = second_amount, second_currency, representative

    def request_identity(self, intent, *, limit, execution_scope_id):
        fingerprint = hashlib.sha256(("same" if self.equivalent else intent.source_row_id).encode()).hexdigest()
        return ProviderSearchRequestIdentity(provider_key=self.key, request_fingerprint=fingerprint,
            execution_scope_id=execution_scope_id, limit=limit)

    def execute_search(self, intent, *, limit, request_counter):
        self.calls.append(intent.source_row_id)
        if self.hook:
            self.hook(intent)
        for _ in range(self.requests):
            request_counter.record_outbound_attempt()
        offers = ()
        if self.state == ProviderSearchState.SUCCESS:
            proof = {"source": self.key}
            if self.key == ETM:
                proof.update(source_item_id="123", price_field="wrong" if self.invalid else "pricewnds")
                if self.invalid:
                    proof["source_item_id"] = "foreign"
            elif self.key == LEMANA:
                proof.update(product_item="123", mirror_revision="a" * 64, region_id=1)
            offers = (Offer(provider=self.key, offer_id="123", source_item_id="123",
                title=intent.normalized_name + (" " + intent.model if self.weaker else ""),
                article="" if self.review else "foreign" if self.reject else intent.article,
                price=Decimal(self.amount), currency="RUB", price_unit="шт", retrieved_at=NOW,
                attributes={} if self.weaker else {"model": intent.model}, data_provenance=proof),)
            if self.second_amount is not None:
                offers += (Offer(**dict(offers[0].model_dump(mode="python"), offer_id="124", source_item_id="124",
                    price=Decimal(self.second_amount), currency=self.second_currency,
                    data_provenance={"source": self.key, "source_item_id": "124", "price_field": "pricewnds"})),)
            if self.representative:
                offers = tuple(Offer(**dict(item.model_dump(mode="python"), title="Industrial control valve " + "V" * 215,
                    brand="B" * 80, url="https://supplier.example/products/" + "p" * 127,
                    availability=True, availability_text="In stock")) for item in offers)
        return ProviderSearchOutcome(provider_key=self.key, state=self.state, offers=offers,
            request_count=request_counter.request_count,
            failure_category=ProviderFailureCategory.TIMEOUT if self.state == ProviderSearchState.FAILURE else
                ProviderFailureCategory.AUTHENTICATION if self.state == ProviderSearchState.SUPPRESSED else None)


def _service(*adapters):
    return SourcingService({}, provider_runner=ProviderRunner({adapter.key: adapter for adapter in adapters}))


def _execute(workspace, service, keys=(ETM,), **updates):
    _, store, snapshot, ids = workspace
    options = dict(selected_source_row_ids=ids, source_mode=SourcingSourceMode.PROVIDER_ONLY, clock=lambda: NOW)
    options.update(updates)
    return orchestration.execute_multi_provider_tender_run(snapshot=snapshot, service=service, store=store,
        selection=ProviderSelection(provider_keys=keys), **options)


def _read(workspace, result):
    _, store, snapshot, _ = workspace
    stored = store.read_durable_v2(snapshot.workspace_path, result.run_id)
    assert stored == result.run
    assert (snapshot.workspace_path / "runs" / f"{result.run_id}.json").read_bytes() == encode_tender_sourcing_run_v2(result.run)
    assert result.run.schema_version == 2 and result.run.tender_id == snapshot.tender_id
    return stored


@pytest.mark.parametrize("keys", [(ETM,), (ETM, LEMANA)])
def test_success_exact_chain_once_terminal_readback(tmp_path, monkeypatch, keys):
    workspace = _workspace(tmp_path, 3)
    service = _service(*(Adapter(key) for key in keys))
    chains, projected, writes = [], [], []
    for method in ("understand_row", "execute_provider_selection", "evaluate_provider_execution",
                   "evaluate_provider_commercial_evidence", "select_provider_commercial_winner"):
        original = getattr(service, method)
        def spy(*args, _method=method, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            chains.append((_method, result))
            return result
        monkeypatch.setattr(service, method, spy)
    project = orchestration.project_durable_provider_row_v2
    def exact(identity, **kwargs):
        assert kwargs["execution"] is chains[-4][1]
        assert kwargs["matching"] is chains[-3][1]
        assert kwargs["commercial"] is chains[-2][1]
        assert kwargs["selection"] is chains[-1][1]
        projected.append(identity.source_row_id)
        return project(identity, **kwargs)
    monkeypatch.setattr(orchestration, "project_durable_provider_row_v2", exact)
    persist = workspace[1].persist_durable_v2
    def terminal(path, run):
        assert run.status == "completed"
        assert not (path / "runs").exists()
        writes.append(run)
        persist(path, run)
    monkeypatch.setattr(workspace[1], "persist_durable_v2", terminal)
    monkeypatch.setattr(workspace[1], "create_running", lambda *a, **k: pytest.fail("v1 placeholder"))
    result = _execute(workspace, service, keys, selected_source_row_ids=tuple(reversed(workspace[3])))
    stored = _read(workspace, result)
    assert result.status == "completed" and result.failure_code is None
    assert projected == list(workspace[3])  # physical source order, not input ID order
    assert len(chains) == 15 and len(writes) == 1
    assert set(stored.selected_source_row_ids) == {row.source_row_id for row in stored.rows}
    assert all(row.result_limit == 20 for row in stored.rows)
    assert not workspace[0].activity.active(result.run.tender_id)


@pytest.mark.parametrize("scenario,expected", [("weaker", None), ("unique", None), ("tie", "LOWEST_PRICE_TIED"),
    ("incomplete", "COMMERCIAL_EVIDENCE_INCOMPLETE"), ("invalid", "COMMERCIAL_EVIDENCE_INVALID"),
    ("noncomparable", "COMMERCIAL_BASIS_NOT_COMPARABLE")])
def test_commercial_decisions_and_same_offer_id(tmp_path, monkeypatch, scenario, expected):
    workspace = _workspace(tmp_path, article="" if scenario == "weaker" else "A-1",
        model="K-100" if scenario == "weaker" else "X")
    etm = Adapter(ETM, amount="1000" if scenario == "weaker" else "100", invalid=scenario == "invalid")
    lemana = Adapter(LEMANA, amount="100" if scenario == "tie" else "1", weaker=scenario == "weaker")
    same_provider = scenario in {"unique", "tie", "noncomparable"}
    if same_provider:
        # Lemana VAT is unproven in the approved durable contract. Exercise two
        # COMPLETE strongest candidates using real ETM evidence, without changing trust.
        etm.second_amount = "100.00" if scenario == "tie" else "1"
        etm.second_currency = "USD" if scenario == "noncomparable" else "RUB"
        lemana.state = ProviderSearchState.EMPTY
    service = _service(etm, lemana)
    matches = []
    evaluate = service.evaluate_provider_execution
    def spy(*args):
        result = evaluate(*args)
        matches.extend(result.matches)
        return result
    monkeypatch.setattr(service, "evaluate_provider_execution", spy)
    row = _read(workspace, _execute(workspace, service, (ETM, LEMANA))).rows[0]
    selected = row.commercial_selection
    if expected:
        assert selected.state.value == "NO_SAFE_WINNER" and selected.selected_reference is None
        assert expected in [item.value for item in selected.reason_codes]
        assert len(selected.candidate_references) == 2
    else:
        assert selected.selected_reference.provider_key == ETM
        assert selected.selected_reference.offer_id == ("123" if scenario == "weaker" else "124")
    if scenario == "weaker":
        assert row.matches[0].decision == MatchDecision.MATCH
        assert selected.candidate_references[0].provider_key == ETM
        assert next(item for item in matches if item.offer.provider == LEMANA).decision == MatchDecision.LIKELY_MATCH
    else:
        assert {ref.ordering_key for ref in selected.candidate_references} == (
            {(ETM, "123"), (ETM, "124")} if same_provider else {(ETM, "123"), (LEMANA, "123")})
        assert row.recommended_offer_reference is None  # identity ambiguity remains truthful


@pytest.mark.parametrize("all_failed", [False, True])
def test_provider_failure_is_truthful_completed_row(tmp_path, all_failed):
    workspace = _workspace(tmp_path)
    service = _service(Adapter(ETM, state=ProviderSearchState.FAILURE if all_failed else ProviderSearchState.SUCCESS),
        Adapter(LEMANA, state=ProviderSearchState.FAILURE))
    run = _read(workspace, _execute(workspace, service, (ETM, LEMANA)))
    assert run.status == "completed"
    assert run.rows[0].outcomes[1].state == ProviderSearchState.FAILURE
    assert run.rows[0].outcomes[1].failure_category == ProviderFailureCategory.TIMEOUT
    assert [outcome.request_count for outcome in run.rows[0].outcomes] == [1, 1]
    assert run.partial_failure == (not all_failed)
    assert len(run.rows[0].offers) == (0 if all_failed else 1)
    ref = run.rows[0].commercial_selection.selected_reference
    assert ref is None if all_failed else ref.provider_key == ETM


@pytest.mark.parametrize("case", ["review", "reject"])
def test_noncommercial_identity_does_not_get_forced_winner(tmp_path, case):
    workspace = _workspace(tmp_path)
    row = _read(workspace, _execute(workspace, _service(Adapter(ETM, **{case: True})))).rows[0]
    assert row.commercial_selection.selected_reference is None
    assert not row.commercial_selection.candidate_references


def test_cache_request_counts_local_suppression_and_independent_runs(tmp_path):
    workspace = _workspace(tmp_path, 2, identical=True)
    etm = Adapter(ETM, state=ProviderSearchState.SUPPRESSED, requests=0, equivalent=True)
    local = Adapter(LOCAL, requests=0, equivalent=True)
    lemana = Adapter(LEMANA, requests=2, equivalent=True)
    service = _service(etm, local, lemana)
    for _ in range(2):
        result = _execute(workspace, service, (ETM, LOCAL, LEMANA))
        run = _read(workspace, result)
        by_id = {row.source_row_id: row for row in run.rows}
        first, reused = [by_id[key] for key in workspace[3]]
        assert [item.request_count for item in first.outcomes] == [0, 2, 0]
        assert [item.request_count for item in reused.outcomes] == [0, 0, 0]
        assert reused.reused_provider_keys == (ETM, LEMANA, LOCAL)
        assert first.outcomes[0].state == ProviderSearchState.SUPPRESSED
    assert len(lemana.calls) == len(local.calls) == len(etm.calls) == 2
    assert len(list((workspace[2].workspace_path / "runs").glob("*.json"))) == 2


@pytest.mark.parametrize("mutation", ["revision", "source", "owner", "rows", "manifest", "path"])
def test_pre_call_snapshot_mutation_zero_runner_calls(tmp_path, monkeypatch, mutation):
    workspace = _workspace(tmp_path)
    repository, _, snapshot, _ = workspace
    if mutation == "source":
        source = snapshot.workspace_path / "source.xlsx"
        os.chmod(source, 0o600)
        source.write_bytes(b"changed")
    elif mutation == "path":
        workspace = (*workspace[:2], snapshot.model_copy(update={"workspace_path": tmp_path}), workspace[3])
    else:
        metadata = repository._metadata(snapshot.workspace_path)
        if mutation == "revision": metadata["revision"] += 1
        elif mutation == "owner": metadata["owner_id"] = "different"
        elif mutation == "rows": metadata["rows"][0]["name"] = "changed"
        else: metadata["source_manifest"]["selected_sheet"] = "changed"
        repository._atomic_json(snapshot.workspace_path / "workspace.json", metadata)
    service = _service(Adapter(ETM))
    monkeypatch.setattr(service.provider_runner, "run", lambda **kwargs: pytest.fail("runner before fence"))
    with pytest.raises(orchestration.MultiProviderOrchestrationError) as error:
        _execute(workspace, service)
    assert error.value.code == Code.SNAPSHOT_CHANGED
    assert not repository.activity.active(snapshot.tender_id)
    assert not (snapshot.workspace_path / "runs").exists()


@pytest.mark.parametrize("case", ["duplicate", "foreign", "empty", "section", "over500", "malformed", "list"])
def test_selected_row_fence_before_runner(tmp_path, monkeypatch, case):
    workspace = _workspace(tmp_path, section=True)
    ids = workspace[3]
    section_id = next(row["source_row_id"] for row in workspace[0]._metadata(workspace[2].workspace_path)["rows"] if row["row_type"] == "section")
    selected = {"duplicate": ids * 2, "foreign": ("f" * 32,), "empty": (), "section": (section_id,),
                "over500": tuple(f"{index:032x}" for index in range(501)), "malformed": ("bad",), "list": list(ids)}[case]
    service = _service(Adapter(ETM))
    monkeypatch.setattr(service.provider_runner, "run", lambda **kwargs: pytest.fail("runner before row fence"))
    with pytest.raises(orchestration.MultiProviderOrchestrationError) as error:
        _execute(workspace, service, selected_source_row_ids=selected)
    assert error.value.code == Code.INVALID_ROWS
    assert not workspace[0].activity.active(workspace[2].tender_id)


@pytest.mark.parametrize("mode", [SourcingSourceMode.ONE_C_ONLY, SourcingSourceMode.ONE_C_THEN_PROVIDER])
def test_history_scope_rejected_before_any_live_or_understanding(tmp_path, monkeypatch, mode):
    workspace = _workspace(tmp_path)
    service = _service(Adapter(ETM))
    monkeypatch.setattr(service.provider_runner, "run", lambda **kwargs: pytest.fail("history row reached runner"))
    monkeypatch.setattr(service, "understand_row", lambda *args: pytest.fail("history row reached live chain"))
    with pytest.raises(orchestration.MultiProviderOrchestrationError) as error:
        _execute(workspace, service, source_mode=mode)
    assert error.value.code == Code.UNSUPPORTED_MODE
    assert not (workspace[2].workspace_path / "runs").exists()


@pytest.mark.parametrize("exit_kind", ["success", "row-failed", "projection-failed", "zero-rows", "corrupt-build", "storage", "unexpected-storage", "clock"])
def test_lease_exactly_once_and_terminal_failure_policy(tmp_path, monkeypatch, exit_kind):
    workspace = _workspace(tmp_path, 3)
    repository, store, snapshot, ids = workspace
    releases, allocated = [], []
    original_acquire = repository.acquire_sourcing_workspace
    def acquire(*args):
        path, metadata, lease = original_acquire(*args)
        release = lease.release
        def once():
            releases.append(True)
            release()
        lease.release = once
        return path, metadata, lease
    monkeypatch.setattr(repository, "acquire_sourcing_workspace", acquire)
    real_uuid = orchestration.uuid.uuid4
    def allocate():
        value = real_uuid()
        allocated.append(value.hex)
        return value
    monkeypatch.setattr(orchestration.uuid, "uuid4", allocate)
    def busy(_intent):
        assert repository.activity.active(snapshot.tender_id)
        with pytest.raises(TenderWorkspaceError) as error:
            repository.delete(snapshot.tender_id, "owner")
        assert error.value.code == "TENDER_WORKSPACE_BUSY"
        assert not (snapshot.workspace_path / "runs").exists()
    service = _service(Adapter(ETM, hook=busy))
    if exit_kind in {"row-failed", "zero-rows"}:
        original = service.evaluate_provider_execution
        def evaluate(intent, execution):
            if intent.source_row_id == ids[0 if exit_kind == "zero-rows" else 1]:
                raise RuntimeError("Authorization: Bearer secret C:/private/path")
            return original(intent, execution)
        monkeypatch.setattr(service, "evaluate_provider_execution", evaluate)
    if exit_kind == "projection-failed":
        project = orchestration.project_durable_provider_row_v2
        def invalid_row(identity, **kwargs):
            if identity.source_row_id == ids[1]:
                raise DurableProjectionError()
            return project(identity, **kwargs)
        monkeypatch.setattr(orchestration, "project_durable_provider_row_v2", invalid_row)
    if exit_kind == "corrupt-build":
        monkeypatch.setattr(orchestration, "build_durable_tender_sourcing_run_v2", lambda *args: (_ for _ in ()).throw(DurableProjectionError()))
    if exit_kind in {"storage", "unexpected-storage"}:
        def bad_storage(*args):
            if exit_kind == "storage": raise DurableV2StorageError(DurableV2StorageCode.STORAGE_FAILED)
            raise OSError("Authorization: Bearer secret C:/private/path")
        monkeypatch.setattr(store, "persist_durable_v2", bad_storage)
    times = iter([NOW, datetime(2026, 10, 9)]) if exit_kind == "clock" else iter([NOW, NOW])
    if exit_kind in {"success", "row-failed", "projection-failed"}:
        result = _execute(workspace, service, clock=lambda: next(times))
        assert result.run_id == allocated[0]
        assert result.status == ("completed" if exit_kind == "success" else "failed")
        assert len(_read(workspace, result).rows) == (3 if exit_kind == "success" else 1)
        assert set(result.run.selected_source_row_ids) == set(ids)
        assert result.run.completed_at == NOW.isoformat()
        assert result.failure_code == (None if exit_kind == "success" else Code.EVALUATION_FAILED)
    else:
        with pytest.raises((orchestration.MultiProviderOrchestrationError, DurableProjectionError, DurableV2StorageError)) as error:
            _execute(workspace, service, clock=lambda: next(times))
        assert "secret" not in str(error.value) and "private" not in str(error.value)
        assert not (snapshot.workspace_path / "runs").exists()
    assert len(allocated) == len(releases) == 1
    assert not repository.activity.active(snapshot.tender_id)


@pytest.mark.parametrize("storage_code", [DurableV2StorageCode.TOO_LARGE, DurableV2StorageCode.ALREADY_EXISTS,
    DurableV2StorageCode.METADATA_MISMATCH, DurableV2StorageCode.STORAGE_FAILED])
def test_closed_storage_errors_never_become_success_or_retry(tmp_path, monkeypatch, storage_code):
    workspace = _workspace(tmp_path)
    attempts = []
    def fail(*args):
        attempts.append(True)
        raise DurableV2StorageError(storage_code)
    monkeypatch.setattr(workspace[1], "persist_durable_v2", fail)
    with pytest.raises(DurableV2StorageError) as error:
        _execute(workspace, _service(Adapter(ETM)))
    assert error.value.code == storage_code and len(attempts) == 1
    assert not workspace[0].activity.active(workspace[2].tender_id)


def test_same_id_conflict_uses_m3c_no_clobber(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    service = _service(Adapter(ETM))
    fixed = orchestration.uuid.uuid4()
    monkeypatch.setattr(orchestration.uuid, "uuid4", lambda: fixed)
    first = _execute(workspace, service)
    before = encode_tender_sourcing_run_v2(first.run)
    with pytest.raises(DurableV2StorageError) as error:
        _execute(workspace, service)
    assert error.value.code == DurableV2StorageCode.ALREADY_EXISTS
    assert (workspace[2].workspace_path / "runs" / f"{fixed.hex}.json").read_bytes() == before


def test_equivalent_provider_and_selected_order_produce_exact_same_bytes(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, 3)
    fixed = orchestration.uuid.uuid4()
    monkeypatch.setattr(orchestration.uuid, "uuid4", lambda: fixed)
    first = _execute(workspace, _service(Adapter(ETM), Adapter(LEMANA)), (ETM, LEMANA))
    first_bytes = encode_tender_sourcing_run_v2(_read(workspace, first))
    (workspace[2].workspace_path / "runs" / f"{fixed.hex}.json").unlink()
    second = _execute(workspace, _service(Adapter(LEMANA), Adapter(ETM)), (LEMANA, ETM),
        selected_source_row_ids=tuple(reversed(workspace[3])))
    assert encode_tender_sourcing_run_v2(_read(workspace, second)) == first_bytes


def test_same_offer_id_on_different_rows_has_separate_commercial_chain(tmp_path):
    workspace = _workspace(tmp_path, 2)
    adapter = Adapter(ETM)
    def price_per_row(intent):
        adapter.amount = "1000" if intent.source_row_id == workspace[3][0] else "1"
    adapter.hook = price_per_row
    run = _read(workspace, _execute(workspace, _service(adapter)))
    rows = {row.source_row_id: row for row in run.rows}
    assert [rows[row_id].offers[0].price for row_id in workspace[3]] == [Decimal("1000"), Decimal("1")]
    assert all(row.commercial_selection.selected_reference.offer_id == "123" for row in run.rows)


@pytest.mark.parametrize("case", ["unknown", "unavailable", "unconfigured", "dict", "bad-limit", "naive-clock"])
def test_strict_admission_and_registry_fail_closed_zero_calls(tmp_path, case):
    workspace = _workspace(tmp_path)
    adapter = Adapter(ETM)
    service = _service(adapter)
    keys, options = (ETM,), {}
    if case == "unknown": keys = (ETM, "vseinstrumenti")
    elif case == "unavailable": keys = (ETM, LEMANA)
    elif case == "unconfigured": service.provider_runner = None
    elif case == "dict": workspace = (*workspace[:2], {}, workspace[3])
    elif case == "bad-limit": options["limit"] = True
    else: options["clock"] = lambda: NOW.replace(tzinfo=None)
    with pytest.raises(orchestration.MultiProviderOrchestrationError) as error:
        _execute(workspace, service, keys, **options)
    assert error.value.run_id and not adapter.calls
    assert str(error.value) == "Internal multi-provider run could not be produced."


@pytest.mark.parametrize("count", [370, 500])
def test_real_runner_capacity_all_or_nothing(tmp_path, count):
    workspace = _workspace(tmp_path, count)
    etm, lemana = Adapter(ETM, representative=True), Adapter(LEMANA, representative=True)
    if count == 370:
        run = _read(workspace, _execute(workspace, _service(etm, lemana), (ETM, LEMANA)))
        assert len(run.rows) == 370 and sum(len(row.offers) for row in run.rows) == 740
        assert len(encode_tender_sourcing_run_v2(run)) <= MAX_DURABLE_V2_BYTES
    else:
        with pytest.raises(DurableProjectionError) as error:
            _execute(workspace, _service(etm, lemana), (ETM, LEMANA))
        assert error.value.code == DurableProjectionCode.TOO_LARGE
        assert not (workspace[2].workspace_path / "runs").exists()
    assert len(etm.calls) == len(lemana.calls) == count
    assert not workspace[0].activity.active(workspace[2].tender_id)


@pytest.mark.parametrize("name", ["test_tender_sourcing_is_server_authoritative_durable_and_owner_bound",
    "test_tender_history_modes_hold_captured_history_lease_and_use_existing_router"])
def test_existing_public_v1_routes_never_invoke_m3d(tender_api, tmp_path, monkeypatch, name):
    import test_manual_tender_xlsx as legacy
    monkeypatch.setattr(orchestration, "execute_multi_provider_tender_run", lambda **kwargs: pytest.fail("public v1 activated M3D"))
    getattr(legacy, name)(tender_api, tmp_path, monkeypatch)
