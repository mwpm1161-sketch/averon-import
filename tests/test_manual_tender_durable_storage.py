from __future__ import annotations

import hashlib
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import averon_import.services.manual_tenders.durable_storage as storage
import averon_import.services.manual_tenders.repository as repository_module
import averon_import.services.manual_tenders.sourcing as sourcing
from averon_import.services.manual_tenders.durable_projection import encode_tender_sourcing_run_v2
from averon_import.services.manual_tenders.durable_read import (
    MAX_DURABLE_V2_BYTES, DurableTenderSourcingRowV2, DurableTenderSourcingRunV2,
)
from averon_import.services.manual_tenders.durable_storage import DurableV2StorageCode as Code, DurableV2StorageError
from averon_import.services.manual_tenders.parser import TenderWorkbookParser
from averon_import.services.manual_tenders.repository import (
    MAX_TENDER_RUN_BYTES, MAX_TENDER_RUNS_PER_WORKSPACE, TenderWorkspaceError, TenderWorkspaceRepository,
)
from averon_import.services.manual_tenders.sourcing import TenderSourcingRunStore
from test_manual_tender_durable_projection import _capacity_rows, _chain, _near_budget_rows, _project, _raw_wire_size, _run
from test_manual_tender_xlsx import _official


@pytest.fixture
def workspace(tmp_path):
    repository = TenderWorkspaceRepository(tmp_path / "data")
    payload = _official(tmp_path / "input.xlsx")
    digest = hashlib.sha256(payload).hexdigest()
    preview = repository.reserve_preview("owner", "input.xlsx", len(payload), digest)
    repository.write_preview_source(preview["preview_id"], payload)
    source = repository.preview_root / preview["preview_id"] / "source.xlsx"
    analysis = TenderWorkbookParser().parse(source, tender_id=preview["preview_id"])
    repository.update_preview(preview["preview_id"], "owner", status="ready", analysis=analysis)
    confirmed = repository.confirm(preview["preview_id"], "owner")
    path = repository.workspace_root / confirmed["tender_id"]
    return repository, TenderSourcingRunStore(repository), path, repository._metadata(path)


def _bind(run, workspace, **updates):
    metadata = workspace[3]
    payload = run.model_dump(mode="python")
    payload.update(tender_id=metadata["tender_id"], source_sha256=metadata["source_sha256"],
                   workspace_revision=metadata["revision"], **updates)
    return DurableTenderSourcingRunV2.model_validate(payload)


def _small(workspace, status="completed", **updates):
    return _bind(_run((_project(_chain("multi")),), status), workspace, **updates)


def _path(workspace, run):
    return workspace[2] / "runs" / f"{run.run_id}.json"


def _legacy(workspace, *, complete=True):
    _, store, path, metadata = workspace
    run = store.create_running(path, metadata, source_mode="provider_only", provider="etm_ipro",
        selected_ids=[f"{1:032x}"], history_catalog_version=None)
    if complete:
        store.complete(path, run["run_id"], summary={}, catalog_version=None, history_catalog_version=None, rows=[])
    return run["run_id"]


def _assert_safe(error, code=None):
    assert isinstance(error, DurableV2StorageError)
    if code is not None:
        assert error.code == code
    assert str(error) == "Durable v2 storage operation failed."
    assert error.__suppress_context__ or error.__context__ is None


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_terminal_exact_bytes_round_trip_immutable_input_permissions(workspace, status):
    _, store, path, _ = workspace
    run = _small(workspace, status)
    before = run.model_dump(mode="python")
    encoded = encode_tender_sourcing_run_v2(run)
    store.persist_durable_v2(path, run)
    assert _path(workspace, run).read_bytes() == encoded
    assert store.read_durable_v2(path, run.run_id) == run
    assert run.model_dump(mode="python") == before
    assert list((path / "runs").iterdir()) == [_path(workspace, run)]
    if os.name != "nt":
        assert _path(workspace, run).stat().st_mode & 0o777 == 0o600
        assert (path / "runs").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["running", "dict", "v1", "construct", "corrupt-copy"])
def test_explicit_typed_terminal_boundary_rejects_before_file_creation(workspace, kind):
    _, store, path, _ = workspace
    run = _small(workspace, "running" if kind == "running" else "completed")
    if kind == "dict": run = run.model_dump(mode="python")
    elif kind == "v1": run = {"schema_version": 1}
    elif kind == "construct": run = DurableTenderSourcingRunV2.model_construct()
    elif kind == "corrupt-copy": run = run.model_copy(update={"rows": []})
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.NON_TERMINAL if kind == "running" else Code.INVALID_INPUT)
    assert not (path / "runs").exists()


@pytest.mark.parametrize("existing", ["v1", "v2", "different-v2", "corrupt"])
def test_existing_file_conflict_before_temp_or_encoder(workspace, monkeypatch, existing):
    _, store, path, _ = workspace
    run = _small(workspace)
    if existing == "v1":
        run = _small(workspace, run_id=_legacy(workspace))
    else:
        store.persist_durable_v2(path, run)
        if existing == "different-v2": run = _small(workspace, "failed")
        elif existing == "corrupt": _path(workspace, run).write_bytes(b"broken")
    previous = _path(workspace, run).read_bytes()
    def forbidden(*args, **kwargs): raise AssertionError("conflict must precede mutation/encoding")
    monkeypatch.setattr(storage.tempfile, "mkstemp", forbidden)
    monkeypatch.setattr(storage, "encode_tender_sourcing_run_v2", forbidden)
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.ALREADY_EXISTS)
    assert _path(workspace, run).read_bytes() == previous
    assert not list((path / "runs").glob("*.tmp"))


@pytest.mark.parametrize("field,value", [("tender_id", "c" * 32), ("source_sha256", "c" * 64), ("workspace_revision", 2)])
def test_snapshot_metadata_correlation_on_both_write_and_read(workspace, field, value):
    _, store, path, _ = workspace
    run = _small(workspace)
    bad = run.model_copy(update={field: value})
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, bad)
    _assert_safe(caught.value, Code.METADATA_MISMATCH)
    assert not (path / "runs").exists()
    store.persist_durable_v2(path, run)
    _path(workspace, run).write_bytes(encode_tender_sourcing_run_v2(bad))
    with pytest.raises(DurableV2StorageError) as caught:
        store.read_durable_v2(path, run.run_id)
    _assert_safe(caught.value, Code.METADATA_MISMATCH)


@pytest.mark.parametrize("kind", ["outside", "missing", "preview", "metadata-json", "metadata-size",
    "metadata-id", "revision-bool", "revision-string", "sha-invalid", "owner-missing", "source-missing", "source-changed"])
def test_confirmed_workspace_and_safe_metadata_required(workspace, tmp_path, kind):
    repository, store, path, metadata = workspace
    run = _small(workspace)
    if kind == "outside":
        path = tmp_path / metadata["tender_id"]; path.mkdir()
    elif kind == "missing": path = repository.workspace_root / ("c" * 32)
    elif kind == "preview":
        path = repository.preview_root / metadata["tender_id"]; path.mkdir()
    elif kind == "metadata-json": (path / "workspace.json").write_bytes(b"{")
    elif kind == "metadata-size": (path / "workspace.json").write_bytes(b"x" * (repository_module.MAX_WORKSPACE_METADATA_BYTES + 1))
    elif kind.startswith("source-"):
        source = path / "source.xlsx"; source.chmod(0o600)
        if kind == "source-missing": source.unlink()
        else: source.write_bytes(b"changed")
    else:
        if kind == "metadata-id": metadata["tender_id"] = "c" * 32
        elif kind == "revision-bool": metadata["revision"] = True
        elif kind == "revision-string": metadata["revision"] = "1"
        elif kind == "sha-invalid": metadata["source_sha256"] = "invalid"
        else: metadata.pop("owner_id")
        repository._atomic_json(path / "workspace.json", metadata)
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.WORKSPACE_INVALID)
    assert not (path / "runs").exists()


@pytest.mark.parametrize("kind,code", [("json", Code.CORRUPT), ("v1", Code.UNSUPPORTED_VERSION), ("v1-large", Code.UNSUPPORTED_VERSION),
    ("unknown", Code.UNSUPPORTED_VERSION), ("wire", Code.CORRUPT), ("oversize", Code.TOO_LARGE),
    ("filename", Code.METADATA_MISMATCH), ("running", Code.NON_TERMINAL)])
def test_malformed_v2_reader_fails_closed_without_repair(workspace, kind, code):
    _, store, path, _ = workspace
    run = _small(workspace)
    payload = json.loads(encode_tender_sourcing_run_v2(run))
    if kind == "json": encoded = b'{"schema_version":2,'
    elif kind == "oversize":
        encoded = encode_tender_sourcing_run_v2(run)
        encoded += b" " * (MAX_DURABLE_V2_BYTES + 1 - len(encoded))
    else:
        if kind in {"v1", "v1-large"}:
            payload = {"schema_version": 1, "tender_id": run.tender_id}
            if kind == "v1-large": payload["padding"] = "x" * MAX_DURABLE_V2_BYTES
        elif kind == "unknown": payload["schema_version"] = 9
        elif kind == "wire": payload["rows"][0].append("broken")
        elif kind == "filename": payload["run_id"] = "c" * 32
        elif kind == "running": payload.update(status="running", completed_at=None)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    (path / "runs").mkdir()
    target = _path(workspace, run); target.write_bytes(encoded)
    with pytest.raises(DurableV2StorageError) as caught:
        store.read_durable_v2(path, run.run_id)
    _assert_safe(caught.value, code)
    assert target.read_bytes() == encoded


@pytest.mark.parametrize("run_id", ["../escape", "a" * 31, "A" * 32, "a" * 32 + "/", None, 123])
def test_reader_run_id_is_exact_filename_identity(workspace, run_id):
    with pytest.raises(DurableV2StorageError) as caught:
        workspace[1].read_durable_v2(workspace[2], run_id)
    _assert_safe(caught.value, Code.INVALID_INPUT)


@pytest.mark.parametrize("phase", ["encoder", "temp", "chmod", "fdopen", "write", "short-write", "flush", "fsync", "publish"])
def test_atomic_failures_leave_no_target_or_temp_and_preserve_unrelated_files(workspace, monkeypatch, phase):
    _, store, path, _ = workspace
    other = _small(workspace, run_id="c" * 32)
    store.persist_durable_v2(path, other)
    previous = _path(workspace, other).read_bytes()
    run = _small(workspace)
    def fail(*args, **kwargs): raise OSError("C:\\private\\secret Authorization: Bearer password=raw")
    if phase == "encoder": monkeypatch.setattr(storage, "encode_tender_sourcing_run_v2", fail)
    elif phase == "temp": monkeypatch.setattr(storage.tempfile, "mkstemp", fail)
    elif phase in {"chmod", "fdopen", "fsync"}: monkeypatch.setattr(storage.os, phase, fail)
    elif phase == "publish": monkeypatch.setattr(storage.os, "link", fail)
    else:
        original = storage.os.fdopen
        class FaultStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def write(self, data):
                self.stream.write(data[:5])
                if phase == "write": fail()
                if phase == "short-write": return 5
                return self.stream.write(data[5:]) + 5
            def flush(self): fail()
        monkeypatch.setattr(storage.os, "fdopen", lambda *args, **kwargs: FaultStream(original(*args, **kwargs)))
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.INVALID_INPUT if phase == "encoder" else Code.STORAGE_FAILED)
    assert not _path(workspace, run).exists()
    assert not list((path / "runs").glob("*.tmp"))
    assert _path(workspace, other).read_bytes() == previous


def test_atomic_publication_cannot_replace_a_racing_v1_file(workspace, monkeypatch):
    _, store, path, _ = workspace
    run = _small(workspace)
    rival = b'{"schema_version":1,"status":"completed"}'
    link = os.link
    def competing_writer(source, destination):
        Path(destination).write_bytes(rival)
        link(source, destination)
    monkeypatch.setattr(storage.os, "link", competing_writer)
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.ALREADY_EXISTS)
    assert _path(workspace, run).read_bytes() == rival
    assert not list((path / "runs").glob("*.tmp"))


def test_concurrent_duplicate_persistence_has_one_winner(workspace):
    repository, store, path, _ = workspace
    run = _small(workspace)
    other = TenderSourcingRunStore(repository)
    def save(current):
        try: current.persist_durable_v2(path, run); return "saved"
        except DurableV2StorageError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (store, other)))
    assert set(results) == {"saved", Code.ALREADY_EXISTS}
    assert _path(workspace, run).read_bytes() == encode_tender_sourcing_run_v2(run)


def test_v1_v2_public_boundaries_and_restart_coexistence(workspace):
    repository, store, path, metadata = workspace
    active = _legacy(workspace, complete=False)
    terminal = _legacy(workspace)
    run = _small(workspace); store.persist_durable_v2(path, run)
    old_v1 = (path / "runs" / f"{terminal}.json").read_bytes()
    old_v2 = _path(workspace, run).read_bytes()
    public = store.get_public(path, metadata["tender_id"], terminal)
    with pytest.raises(TenderWorkspaceError) as caught:
        store.get_public(path, metadata["tender_id"], run.run_id)
    assert caught.value.code == "TENDER_RUN_CORRUPT"
    with pytest.raises(TenderWorkspaceError):
        store.complete(path, run.run_id, summary={}, catalog_version=None, history_catalog_version=None, rows=[])
    store.fail(path, run.run_id, code="FAILED", progress_current=0, progress_total=1)
    restarted = TenderSourcingRunStore(repository)
    recovered = restarted.get_public(path, metadata["tender_id"], active)
    assert recovered["schema_version"] == 1 and recovered["status"] == "interrupted"
    assert recovered["failure"]["code"] == "SERVER_RESTART"
    assert restarted.get_public(path, metadata["tender_id"], terminal) == public
    assert (path / "runs" / f"{terminal}.json").read_bytes() == old_v1
    assert _path(workspace, run).read_bytes() == old_v2
    assert restarted.read_durable_v2(path, run.run_id) == run
    assert {item["run_id"] for item in restarted.list_public(path, run.tender_id)["runs"]} == {active, terminal}
    assert isinstance(restarted._read_any_path(_path(workspace, run), tender_id=run.tender_id), DurableTenderSourcingRunV2)


@pytest.mark.parametrize("bad", [b'{"schema_version":2,', "running"])
def test_corrupt_and_external_running_v2_are_ignored_without_harming_recovery(workspace, bad):
    repository, store, path, _ = workspace
    active = _legacy(workspace, complete=False)
    terminal = _legacy(workspace)
    healthy = (path / "runs" / f"{terminal}.json").read_bytes()
    run = _small(workspace, "running")
    encoded = encode_tender_sourcing_run_v2(run) if bad == "running" else bad
    _path(workspace, run).write_bytes(encoded)
    restarted = TenderSourcingRunStore(repository)
    assert json.loads((path / "runs" / f"{active}.json").read_bytes())["status"] == "interrupted"
    assert (path / "runs" / f"{terminal}.json").read_bytes() == healthy
    assert _path(workspace, run).read_bytes() == encoded
    assert repository._workspace_storage_bytes() == sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    assert len(restarted.list_public(path, run.tender_id)["runs"]) == 2


def test_mixed_terminal_pruning_uses_same_time_order_without_schema_preference(workspace, monkeypatch):
    repository, store, path, metadata = workspace
    ids = []
    originals = {}
    for index in range(8):
        timestamp = (datetime(2026, 10, 9, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat()
        if index % 2:
            run = _small(workspace, run_id=f"{index + 1:032x}", created_at=timestamp,
                         started_at=timestamp, completed_at=timestamp)
            store.persist_durable_v2(path, run); run_id = run.run_id
        else:
            monkeypatch.setattr(sourcing, "_now", lambda timestamp=timestamp: timestamp)
            run_id = _legacy(workspace)
        ids.append(run_id)
        originals[run_id] = (path / "runs" / f"{run_id}.json").read_bytes()
        assert len(list((path / "runs").glob("*.json"))) <= MAX_TENDER_RUNS_PER_WORKSPACE
    assert {p.stem for p in (path / "runs").glob("*.json")} == set(ids[-5:])
    TenderSourcingRunStore(repository)
    for run_id in ids[-5:]: assert (path / "runs" / f"{run_id}.json").read_bytes() == originals[run_id]
    assert {item["run_id"] for item in store.list_public(path, metadata["tender_id"])["runs"]} == {ids[4], ids[6]}


def test_pruning_timestamp_ties_are_deterministic_by_run_id(workspace):
    _, store, path, _ = workspace
    for index in reversed(range(7)):
        store.persist_durable_v2(path, _small(workspace, run_id=f"{index + 1:032x}"))
    assert {p.stem for p in (path / "runs").glob("*.json")} == {f"{i:032x}" for i in range(3, 8)}


def test_storage_quota_counts_exact_v2_bytes_and_blocks_before_mutation(workspace, monkeypatch):
    repository, store, path, _ = workspace
    before = repository._workspace_storage_bytes()
    run = _small(workspace); store.persist_durable_v2(path, run)
    assert repository._workspace_storage_bytes() == before + len(encode_tender_sourcing_run_v2(run))
    current = repository._workspace_storage_bytes()
    monkeypatch.setattr(repository_module, "MAX_TENDER_STORAGE_BYTES", current + MAX_TENDER_RUN_BYTES - 1)
    other = _small(workspace, run_id="c" * 32)
    with pytest.raises(TenderWorkspaceError) as caught:
        store.persist_durable_v2(path, other)
    assert caught.value.code == "TENDER_DISK_QUOTA"
    assert not _path(workspace, other).exists()
    assert repository._workspace_storage_bytes() == current


@pytest.mark.parametrize("count,retained,expected", [(370, 2, 734507), (500, 1, 583927), (370, 0, 786432)])
def test_approved_m3b_capacity_through_real_store_exact_bytes_and_dto(workspace, count, retained, expected):
    repository, store, path, _ = workspace
    rows = _capacity_rows(retained)[:count] if retained else _near_budget_rows()
    run = _bind(_run(rows), workspace)
    encoded = encode_tender_sourcing_run_v2(run)
    before = repository._workspace_storage_bytes()
    store.persist_durable_v2(path, run)
    assert _path(workspace, run).stat().st_size == len(encoded) == expected
    assert _path(workspace, run).read_bytes() == encoded
    assert store.read_durable_v2(path, run.run_id) == run
    assert repository._workspace_storage_bytes() == before + expected
    assert len(list((path / "runs").glob("*.json"))) == 1
    print(f"real-store {count}x{retained or 2}: {expected} bytes, exact bytes and DTO")


@pytest.mark.parametrize("kind,expected", [("one-over", 786433), ("500x2", 992427)])
def test_over_budget_rejects_before_destination_directory_or_file(workspace, kind, expected):
    _, store, path, _ = workspace
    run = _small(workspace)
    if kind == "one-over":
        data = [row.model_dump(mode="python") for row in _near_budget_rows()]
        item = next(item for row in data for item in row["offers"] if len(item["title"]) < 320)
        item["title"] += "x"
        rows = tuple(DurableTenderSourcingRowV2.model_validate(row) for row in data)
    else: rows = _capacity_rows(2)
    assert _raw_wire_size(rows) == expected
    run = run.model_copy(update={"rows": rows, "selected_source_row_ids": tuple(row.source_row_id for row in rows)})
    with pytest.raises(DurableV2StorageError) as caught:
        store.persist_durable_v2(path, run)
    _assert_safe(caught.value, Code.TOO_LARGE)
    assert not (path / "runs").exists()
    print(f"real-store rejected {expected} bytes before target creation")


@pytest.mark.parametrize("mode", ["delete", "expire"])
def test_whole_workspace_cleanup_removes_both_versions(workspace, mode):
    repository, store, path, metadata = workspace
    _legacy(workspace); store.persist_durable_v2(path, _small(workspace))
    if mode == "delete": assert repository.delete(metadata["tender_id"], "owner")
    else:
        metadata["absolute_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        repository._atomic_json(path / "workspace.json", metadata)
        assert repository.cleanup()["workspaces_removed"] == 1
    assert not path.exists()


def test_storage_uses_only_projected_bytes_without_network_or_decision_engines(workspace, monkeypatch):
    import averon_import.services.sourcing.provider_commercial as commercial
    import averon_import.services.sourcing.provider_commercial_selection as selection
    import averon_import.services.sourcing.provider_matching as matching
    from averon_import.services.sourcing.matching import OfferMatcher
    from averon_import.services.sourcing.models import Offer
    from averon_import.services.sourcing.providers.execution import ProviderRunner
    run = _small(workspace)
    expected = encode_tender_sourcing_run_v2(run)
    def forbidden(*args, **kwargs): raise AssertionError("supplier/network/AI/decision execution forbidden")
    for obj, name in [(socket, "socket"), (socket, "getaddrinfo"), (ProviderRunner, "run"),
        (OfferMatcher, "match"), (Offer, "model_dump"), (matching.ProviderMatchEvaluator, "evaluate"),
        (matching, "_recommendation_quality"), (matching, "_unique_identity_recommendation"),
        (commercial, "resolve_commercial_evidence"), (commercial, "evaluate_provider_commercial_evidence"),
        (commercial, "compare_commercial_evidence"), (selection, "select_provider_commercial_winner")]:
        monkeypatch.setattr(obj, name, forbidden)
    for resolver in commercial.COMMERCIAL_EVIDENCE_RESOLVERS.values(): monkeypatch.setattr(type(resolver), "resolve", forbidden)
    workspace[1].persist_durable_v2(workspace[2], run)
    assert workspace[1].read_durable_v2(workspace[2], run.run_id) == run
    stored = _path(workspace, run).read_bytes()
    assert stored == expected
    for secret in (b"Authorization", b"Bearer", b"password", b"token", b"client_secret", b"raw_payload", b"raw payload", b"must-never-persist"):
        assert secret not in stored


def test_current_v1_writer_recovery_never_encode_or_persist_v2(workspace, monkeypatch):
    import averon_import.services.manual_tenders.durable_projection as projection
    def forbidden(*args, **kwargs): raise AssertionError("automatic v2 activation")
    for name in ("project_durable_provider_row_v2", "build_durable_tender_sourcing_run_v2", "encode_tender_sourcing_run_v2"):
        monkeypatch.setattr(projection, name, forbidden)
    monkeypatch.setattr(storage, "encode_tender_sourcing_run_v2", forbidden)
    monkeypatch.setattr(TenderSourcingRunStore, "persist_durable_v2", forbidden)
    _legacy(workspace)
    failed = _legacy(workspace, complete=False)
    workspace[1].fail(workspace[2], failed, code="FAILED", progress_current=0, progress_total=1)
    _legacy(workspace, complete=False)
    TenderSourcingRunStore(workspace[0])
    files = list((workspace[2] / "runs").iterdir())
    assert len(files) == 3
    values = [json.loads(p.read_bytes()) for p in files]
    assert all(value["schema_version"] == 1 for value in values)
    assert {value["status"] for value in values} == {"completed", "failed", "interrupted"}
