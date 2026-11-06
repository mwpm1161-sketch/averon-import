"""B3.1 exact schemas, native locks, real SQLite commits/crashes, fake-only B2."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from averon_import.services.ai_first.coordination import CoordinationLedger, _expected_schema_digest
from averon_import.services.ai_first.coordination_contracts import CoordinationError, canonical, fingerprint, rub_units
from averon_import.services.ai_first.coordination_maintenance import MaintenanceLayout, MaintenanceSession
from averon_import.services.ai_first.coordination_migrations import MigrationRequest, migrate_v1_to_v2, protected_history
from averon_import.services.ai_first.private_acl import secure_private_path
from b3_test_support import SyntheticQuiescence, open_v2_runtime
from test_ai_first_coordination import FakeClock, specification, register, request, outcome
from test_ai_first_durable_dispatch import receive

WORKER = Path(__file__).with_name("ai_first_migration_worker.py")


@pytest.fixture
def case(tmp_path):
    spec, clock = specification(rpm=10), FakeClock()
    ledger = CoordinationLedger.bootstrap(tmp_path / "ledger", spec, clock=clock)
    control = tmp_path / "control"
    MaintenanceLayout.provision(control, ledger.root, spec.installation_id)
    value = SimpleNamespace(ledger=ledger, spec=spec, clock=clock, control=control,
        payload={"ledger": str(ledger.root), "installation_id": spec.installation_id,
                 "control": str(control), "migration_id": uuid4().hex})
    yield value
    value.ledger.close()


def migrate(case, *, guard=None):
    with MaintenanceSession(case.control, case.ledger.root, case.spec.installation_id,
            legacy_guard=guard or SyntheticQuiescence(case.ledger.root, case.spec.installation_id)) as session:
        return migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(
            case.payload["migration_id"], case.spec.installation_id))


def v2(case):
    return CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id,
        clock=case.clock, schema_version=2, maintenance_root=case.control)


def history(case):
    with sqlite3.connect(case.ledger.path) as connection:
        return protected_history(connection)


@contextmanager
def worker(payload, mode):
    process = subprocess.Popen([sys.executable, str(WORKER), mode], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=WORKER.parents[1])
    try:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def test_v1_defaults_exact_schema_and_no_automatic_v2_bootstrap(case):
    assert case.ledger.schema_version == 1
    assert case.ledger.capabilities() == {"financial_core_v1"}
    before = case.ledger.path.read_bytes()
    with pytest.raises(CoordinationError, match="maintenance_protocol_required"):
        CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id, schema_version=2)
    with pytest.raises(CoordinationError, match="bootstrap_requires_schema_v1"):
        CoordinationLedger.bootstrap(case.ledger.root.parent / "must-not-exist", case.spec, schema_version=2)
    assert not (case.ledger.root.parent / "must-not-exist").exists()
    assert case.ledger.path.read_bytes() == before


def test_migration_preserves_all_financial_rows_metadata_epoch_and_triggers(case):
    with case.ledger.dispatcher() as owner:
        run = register(case.ledger, owner)
        attempts = [request(run, case.spec.bindings[0], page=page) for page in range(1, 5)]
        for item in attempts:
            case.ledger.reserve(owner, item)
        case.ledger.record_outcome(owner, outcome(attempts[0], actual=70))
        case.ledger.record_outcome(owner, outcome(attempts[1], state="not_sent", actual=None))
        case.ledger.record_outcome(owner, outcome(attempts[2], state="unknown", actual=None))
        case.ledger.revise_billing(owner, uuid4().hex, "bill_a", 1, 1100)
        case.ledger.revise_quota(owner, uuid4().hex, "quota_a", 1,
            requests=20, tokens=20000, concurrency=9, window_ms=90000)
        case.ledger.put_binding(owner, uuid4().hex, replace(case.spec.bindings[0], revision=2,
            credential_generation="credential_2"))
        newer = replace(run, run_id=uuid4().hex, generation=2)
        case.ledger.register_run(owner, uuid4().hex, newer)
    before = history(case)
    with sqlite3.connect(case.ledger.path) as connection:
        meta = connection.execute("SELECT epoch,owner_nonce,boot_id,high_water_ms,lease_until_ms FROM meta").fetchone()
        assert meta[0] == 1 and meta[1] is not None
        old_states = connection.execute("SELECT attempt_id,state FROM attempts ORDER BY attempt_id").fetchall()
    case.ledger.close()
    result = migrate(case)
    assert result.newly_applied and not result.commit_receipt_recovered
    assert history(case) == before
    with v2(case) as ledger:
        with ledger._connection() as connection:
            assert connection.execute("SELECT epoch,owner_nonce,boot_id,high_water_ms,lease_until_ms FROM meta").fetchone() == meta
            assert connection.execute("SELECT attempt_id,state FROM attempts ORDER BY attempt_id").fetchall() == old_states
            for sql in ("DELETE FROM operations", "DELETE FROM attempts",
                        "UPDATE billing_scopes SET blocked=0 WHERE blocked=1"):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql)
    with pytest.raises(CoordinationError, match="schema_mismatch"):
        CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id)


def test_opening_balances_stops_and_quota_history_are_never_reset(tmp_path):
    spec = specification(opening_spent=7, opening_held=9)
    spec = replace(spec, quotas=(replace(spec.quotas[0], opening_requests=3, opening_tokens=40,
                                       opening_inflight=1), spec.quotas[1]))
    clock = FakeClock()
    ledger = CoordinationLedger.bootstrap(tmp_path / "ledger", spec, clock=clock)
    control = tmp_path / "control"
    MaintenanceLayout.provision(control, ledger.root, spec.installation_id)
    value = SimpleNamespace(ledger=ledger, spec=spec, control=control,
                           payload={"migration_id": uuid4().hex})
    before = history(value)
    ledger.close()
    migrate(value)
    assert history(value) == before
    with CoordinationLedger(ledger.root, installation_id=spec.installation_id, clock=clock,
            schema_version=2, maintenance_root=control) as migrated:
        assert migrated.snapshot()["billing"]["bill_a"]["held_units"] == 9
        assert migrated.snapshot()["billing"]["bill_a"]["spent_units"] == 7
        with migrated.dispatcher() as owner:
            run = register(migrated, owner)
            with pytest.raises(CoordinationError, match="needs_reconciliation"):
                migrated.reserve(owner, request(run, spec.bindings[0]))


def test_v2_capability_gate_is_not_numeric_and_cannot_be_enabled_by_db_flag(case):
    case.ledger.close()
    migrate(case)
    with v2(case) as ledger:
        assert ledger.capabilities() == {"financial_core_v1", "durable_jobs_v1"}
        with pytest.raises(CoordinationError, match="reconciliation_requires_schema_v3"):
            ledger.require_reconciliation_schema()
        with pytest.raises(CoordinationError, match="capability_not_authorized"):
            ledger.require_capability("b6_reconciliation")
        ledger.schema_version = 3
        with pytest.raises(CoordinationError):
            ledger.require_capability("b6_reconciliation")
        ledger.schema_version = 2
        with sqlite3.connect(ledger.path) as connection:
            connection.execute("DROP VIEW schema_capabilities")
            connection.execute("CREATE VIEW schema_capabilities AS SELECT 1,1,1")
        with pytest.raises(CoordinationError, match="schema_mismatch"):
            ledger.require_reconciliation_schema()


def test_shared_client_lifetime_covers_idle_connections_and_owner(case):
    aware = CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id,
                               clock=case.clock, maintenance_root=case.control)
    try:
        with aware._connection():
            with pytest.raises(CoordinationError, match="ledger_in_use"):
                aware.close()
            with pytest.raises(CoordinationError, match="maintenance_busy"):
                migrate(case)
        with aware.dispatcher():
            with pytest.raises(CoordinationError, match="ledger_in_use"):
                aware.close()
        with pytest.raises(CoordinationError, match="maintenance_busy"):
            migrate(case)  # no connection open, but the CLIENT is still alive
    finally:
        aware.close()
    case.ledger.close()
    migrate(case)


@pytest.mark.parametrize("mode,version", [("client", 1), ("client", 2), ("maintenance", 1)])
def test_two_processes_compete_for_maintenance_and_idle_clients_block(case, mode, version):
    case.ledger.close()
    if version == 2:
        migrate(case)
    case.payload["version"] = version
    with worker(case.payload, mode) as process:
        assert receive(process) == {"window": mode}
        with pytest.raises(CoordinationError, match="maintenance_busy"):
            migrate(case)
    assert migrate(case).newly_applied is (version == 1)


def test_existing_dispatcher_os_lock_is_required_without_taking_a_new_epoch(case):
    with case.ledger.dispatcher() as owner:
        before = history(case)
        with pytest.raises(CoordinationError, match="dispatcher_busy"):
            migrate(case)
        assert history(case) == before and owner.epoch == 1


@pytest.mark.parametrize("mode", ["raw_reader", "raw_writer"])
def test_real_sqlite_readers_and_writers_prevent_exclusive_migration(case, mode):
    before = history(case)
    case.ledger.close()
    with worker(case.payload, mode) as process:
        assert receive(process) == {"window": mode}
        with pytest.raises(CoordinationError, match="ledger_busy"):
            migrate(case)
    assert history(case) == before
    assert migrate(case).newly_applied


def test_legacy_idle_runtime_requires_verified_stop_not_a_boolean(case):
    case.ledger.close()
    with pytest.raises(CoordinationError, match="legacy_quiescence_required"):
        MaintenanceSession(case.control, case.ledger.root, case.spec.installation_id, legacy_guard=True)
    with worker(case.payload, "raw_idle") as process:
        assert receive(process) == {"window": "raw_idle"}
        guard = SyntheticQuiescence(case.ledger.root, case.spec.installation_id, processes=(process,))
        with pytest.raises(CoordinationError, match="legacy_quiescence_unverified"):
            migrate(case, guard=guard)
        process.kill()
        process.wait(timeout=10)
        assert migrate(case, guard=guard).newly_applied


@pytest.mark.parametrize("mode,committed", [("during_ddl", False), ("before_commit", False), ("after_commit", True)])
def test_real_process_kill_at_migration_boundaries(case, mode, committed):
    before = history(case)
    case.ledger.close()
    with worker(case.payload, mode) as process:
        assert receive(process) == {"window": mode}
        process.kill()
        process.wait(timeout=10)
    # Native SQLite hot-journal recovery, not a simulated transaction result.
    with sqlite3.connect(case.ledger.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == (2 if committed else 1)
    assert history(case) == before
    result = migrate(case)
    assert result.newly_applied is (not committed)


@pytest.mark.parametrize("mode,success", [("lost_receipt", True), ("write_failure", False)])
def test_actual_commit_receipt_loss_and_precommit_write_failure(case, mode, success):
    case.ledger.close()
    with worker(case.payload, mode) as process:
        result = receive(process)
    if success:
        assert result["recovered"] is True and result["new"] is True
        assert not migrate(case).newly_applied
    else:
        assert result == {"error": "migration_not_committed"}
        assert migrate(case).newly_applied


def test_repeat_migration_id_after_new_v2_expense_does_not_restore_old_history(case):
    case.ledger.close()
    first = migrate(case)
    with v2(case) as ledger:
        with ledger.dispatcher() as owner:
            run = register(ledger, owner)
            item = request(run, case.spec.bindings[0])
            ledger.reserve(owner, item)
            ledger.record_outcome(owner, outcome(item, actual=80))
    after = history(case)
    second = migrate(case)
    assert first.receipt == second.receipt and not second.newly_applied
    assert history(case) == after
    case.payload["migration_id"] = uuid4().hex
    with pytest.raises(CoordinationError, match="migration_already_applied"):
        migrate(case)


def test_actual_pre_b3_reader_rejects_v2_in_a_new_process(case):
    case.ledger.close()
    migrate(case)
    source = subprocess.check_output(["git", "show",
        "94d6ef391f198fb323a6b86abf0c6425570baa76:averon_import/services/ai_first/coordination.py"],
        cwd=WORKER.parents[1]).decode("utf-8")
    payload = {**case.payload, "legacy_source": source}
    with worker(payload, "old_reader") as process:
        assert receive(process) == {"error": "schema_mismatch"}


def test_v2_snapshot_constraints_reject_execution_inputs_and_history_updates(case):
    from averon_import.services.ai_first.orchestration import OrchestrationConfig, TokenPricing
    from test_ai_first_coordination import run_spec
    case.ledger.close()
    migrate(case)
    config = OrchestrationConfig(max_budget_rub=Decimal("1"), pricing=TokenPricing(
        input_rub_per_1k=Decimal("1"), output_rub_per_1k=Decimal("1"))).model_dump(mode="json")
    run, binding = run_spec(), case.spec.bindings[0]
    snapshot_id = uuid4().hex
    with v2(case) as ledger:
        with ledger._connection() as connection:
            values = (snapshot_id, run.run_id, 1, binding.binding_id, binding.revision,
                      canonical(asdict(run)), canonical(config), canonical(asdict(binding)), "a" * 64, "b" * 64)
            for extra in ("user_context", "api_key", "http_body", "ocr"):
                bad = {**config, extra: "SYNTHETIC PRIVATE INPUT MUST NOT ENTER LEDGER"}
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO durable_job_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                                       (*values[:6], canonical(bad), *values[7:]))
            connection.execute("INSERT INTO durable_job_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)", values)
            for sql in ("UPDATE durable_job_snapshots SET snapshot_digest='x'", "DELETE FROM durable_job_snapshots",
                        "DELETE FROM schema_migrations", "UPDATE schema_migrations SET receipt_json='{}'"):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql)


def test_quiescence_loss_rejects_and_does_not_leak_native_locks(case):
    class Revoked(SyntheticQuiescence):
        active = True
        def verify(self, root, installation):
            if not self.active:
                raise CoordinationError("legacy_quiescence_unverified")
            return super().verify(root, installation)
    case.ledger.close()
    guard = Revoked(case.ledger.root, case.spec.installation_id)
    with MaintenanceSession(case.control, case.ledger.root, case.spec.installation_id, legacy_guard=guard) as session:
        guard.active = False
        with pytest.raises(CoordinationError, match="legacy_quiescence_unverified"):
            migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(uuid4().hex, case.spec.installation_id))
    assert migrate(case).newly_applied


def test_maintenance_cannot_close_with_a_borrowed_client_or_idle_connection(case):
    case.ledger.close()
    with MaintenanceSession(case.control, case.ledger.root, case.spec.installation_id,
            legacy_guard=SyntheticQuiescence(case.ledger.root, case.spec.installation_id)) as session:
        borrowed = CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id,
                                      _maintenance=session.borrow())
        with borrowed._connection():
            with pytest.raises(CoordinationError, match="maintenance_in_use"):
                session.close()
            with pytest.raises(CoordinationError, match="ledger_in_use"):
                borrowed.close()
        with pytest.raises(CoordinationError, match="maintenance_in_use"):
            session.close()
        borrowed.close()
        # Failed construction must release its internal borrowed token as well.
        with pytest.raises(CoordinationError, match="schema_mismatch"):
            CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id,
                               schema_version=2, _maintenance=session.borrow())
    assert migrate(case).newly_applied


def test_v2_real_financial_admission_settlement_quota_unknown_and_fencing(case):
    case.ledger.close()
    migrate(case)
    with v2(case) as ledger:
        with ledger.dispatcher() as owner:
            run = register(ledger, owner)
            first = request(run, case.spec.bindings[0])
            assert ledger.reserve(owner, first).newly_created
            assert not ledger.reserve(owner, first).newly_created
            ledger.validate_dispatch(owner, first, expected_run=run)
            ledger.record_outcome(owner, outcome(first, actual=70))
            unknown = request(run, case.spec.bindings[0], page=2)
            ledger.reserve(owner, unknown)
            ledger.record_outcome(owner, outcome(unknown, state="unknown", actual=None))
            with pytest.raises(CoordinationError, match="needs_reconciliation"):
                ledger.reserve(owner, request(run, case.spec.bindings[0], page=3))
            # Independent scope still enforces its own rolling quota on v2.
            ledger.revise_quota(owner, uuid4().hex, "quota_b", 1,
                requests=1, tokens=10000, concurrency=10, window_ms=60000)
            other = request(run, case.spec.bindings[2], page=3, quota_revisions=(("quota_b", 2),))
            # bill_a is stopped: never bypass a shared billing authority.
            with pytest.raises(CoordinationError, match="needs_reconciliation"):
                ledger.reserve(owner, other)
        with pytest.raises(CoordinationError, match="owner_fenced"):
            ledger.reserve(owner, first)
        snapshot = ledger.snapshot()
        assert snapshot["billing"]["bill_a"]["spent_units"] == 70
        assert snapshot["billing"]["bill_a"]["held_units"] == 100


def test_v2_real_quota_window_and_capacity_are_not_reset_by_migration(case):
    with case.ledger.dispatcher() as owner:
        run = register(case.ledger, owner)
        case.ledger.revise_quota(owner, uuid4().hex, "quota_a", 1,
            requests=1, tokens=20, concurrency=1, window_ms=60000)
        item = request(run, case.spec.bindings[0], quota_revisions=(("quota_a", 2),))
        case.ledger.reserve(owner, item)
        case.ledger.record_outcome(owner, outcome(item, actual=20))
    case.ledger.close()
    migrate(case)
    with v2(case) as ledger:
        with ledger.dispatcher() as owner:
            pending = request(run, case.spec.bindings[0], page=2, quota_revisions=(("quota_a", 2),))
            with pytest.raises(CoordinationError, match="quota_exhausted"):
                ledger.reserve(owner, pending)
            for _index in range(3):
                case.clock.ms += 20000
                owner.renew()
            case.clock.ms += 1
            ledger.reserve(owner, pending)
            with pytest.raises(CoordinationError, match="quota_exhausted"):
                ledger.reserve(owner, replace(pending, operation_id=uuid4().hex, attempt_id=uuid4().hex, page_number=3))


@pytest.mark.parametrize("damage", ["schema", "corrupt", "binding", "control", "request", "history"])
def test_migration_fails_closed_on_invalid_storage_or_history(case, damage):
    case.ledger.close()
    if damage == "schema":
        with sqlite3.connect(case.ledger.path) as connection:
            connection.execute("DROP TRIGGER operations_no_delete")
    elif damage == "corrupt":
        case.ledger.path.write_bytes(b"SYNTHETIC INVALID SQLITE")
    elif damage == "binding":
        (case.control / "binding.json").write_bytes(b"invalid")
    elif damage == "control":
        case.payload["migration_id"] = "invalid"
    elif damage == "history":
        with sqlite3.connect(case.ledger.path) as connection:
            connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (
                uuid4().hex, "outcome", "f" * 64, "{}", "{}", 0, "1" * 32, 1000))
    else:
        with MaintenanceSession(case.control, case.ledger.root, case.spec.installation_id,
                legacy_guard=SyntheticQuiescence(case.ledger.root, case.spec.installation_id)) as session:
            bad = replace(MigrationRequest.v1_to_v2(uuid4().hex, case.spec.installation_id), source_schema_digest="f" * 64)
            with pytest.raises(CoordinationError, match="migration_request_mismatch"):
                migrate_v1_to_v2(session, bad)
        return
    before = case.ledger.path.read_bytes()
    with pytest.raises((CoordinationError, OSError)):
        migrate(case)
    assert case.ledger.path.read_bytes() == before


def test_actual_bad_control_acl_is_rejected_without_repair(case, monkeypatch):
    from averon_import.services.ai_first import private_acl
    case.ledger.close()
    target = case.control / "maintenance.lock"
    try:
        if os.name == "nt":
            with monkeypatch.context() as patch:
                patch.setattr(private_acl, "_sddl", lambda **_kwargs: "D:P(A;;FA;;;WD)")
                private_acl._apply_windows_dacl(target, sid=private_acl._current_user_sid(), directory=False)
        else:
            os.chmod(target, 0o644)
        with pytest.raises(CoordinationError, match="maintenance_storage_unverified"):
            migrate(case)
    finally:
        secure_private_path(target, directory=False)


def test_real_v2_b2_transport_and_historical_recovery_after_process_kill(tmp_path, monkeypatch):
    from averon_import.services.ai_first import dispatch_policy
    from averon_import.services.ai_first.durable_dispatch import HistoricalSettlementContext
    from test_ai_first_durable_dispatch import setup_runtime
    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setattr(dispatch_policy, "_required", False)
    value = setup_runtime(tmp_path)
    payload = value.payload
    value.dispatcher.close()
    value.dispatcher.ledger.close()
    control = tmp_path / "control"
    MaintenanceLayout.provision(control, payload["ledger"], payload["installation_id"])
    payload["control"], payload["migration_id"] = str(control), uuid4().hex
    with MaintenanceSession(control, payload["ledger"], payload["installation_id"],
            legacy_guard=SyntheticQuiescence(Path(payload["ledger"]), payload["installation_id"])) as session:
        migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(payload["migration_id"], payload["installation_id"]))
    with worker(payload, "b2_evidence") as process:
        signal = receive(process)
        assert signal == {"window": "b2_evidence", "posts": 1}
        process.kill()
        process.wait(timeout=10)
    recovered = open_v2_runtime(payload)
    try:
        attempt_id = recovered.dispatcher.attempt_id(recovered.run.run_id, 1)
        snapshot = recovered.dispatcher.ledger.snapshot()
        assert snapshot["attempts"][0]["state"] == "unknown"
        assert snapshot["billing"]["billing"]["held_units"] == rub_units("0.2")
        newer = replace(recovered.run, run_id=uuid4().hex, generation=2)
        recovered.dispatcher.ledger.register_run(recovered.dispatcher._owner, uuid4().hex, newer)
        context = HistoricalSettlementContext.capture(recovered.run, recovered.config)
        receipt = recovered.dispatcher.settle_saved_evidence(attempt_id, context)
        assert receipt["state"] == "terminal" and recovered.transport.calls == []
        snapshot = recovered.dispatcher.ledger.snapshot()
        assert snapshot["billing"]["billing"]["spent_units"] == rub_units("0.14")
        assert snapshot["billing"]["billing"]["held_units"] == 0
        assert snapshot["billing"]["billing"]["blocked"] is True
    finally:
        recovered.dispatcher.close()
        recovered.dispatcher.ledger.close()


@pytest.mark.parametrize("pages", [2, 4, 10])
def test_v2_real_b2_parallel_fake_calls_share_original_financial_authority(tmp_path, monkeypatch, pages):
    import threading
    from averon_import.services.ai_first import dispatch_policy
    from test_ai_first_durable_dispatch import setup_runtime, request as page_request
    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setattr(dispatch_policy, "_required", False)
    initial = setup_runtime(tmp_path, pages=pages)
    payload = initial.payload
    initial.dispatcher.close()
    initial.dispatcher.ledger.close()
    control = tmp_path / "control"
    MaintenanceLayout.provision(control, payload["ledger"], payload["installation_id"])
    payload["control"] = str(control)
    with MaintenanceSession(control, payload["ledger"], payload["installation_id"],
            legacy_guard=SyntheticQuiescence(Path(payload["ledger"]), payload["installation_id"])) as session:
        migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(uuid4().hex, payload["installation_id"]))
    runtime = open_v2_runtime(payload)
    try:
        runtime.transport.request_barrier = threading.Barrier(pages)
        async def execute():
            return await asyncio.gather(*(runtime.adapter.extract(page_request(runtime, page))
                                         for page in range(1, pages + 1)))
        results = asyncio.run(execute())
        assert len(results) == len(runtime.transport.calls) == pages
        assert runtime.transport.max_active == pages
        snapshot = runtime.dispatcher.ledger.snapshot()
        assert snapshot["billing"]["billing"]["spent_units"] == pages * rub_units("0.14")
        assert snapshot["billing"]["billing"]["held_units"] == 0
        assert all(row["state"] == "terminal" for row in snapshot["attempts"])
    finally:
        runtime.dispatcher.close()
        runtime.dispatcher.ledger.close()


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux fork/flock gate; no emulation")
def test_linux_fork_cannot_release_parent_maintenance_guard(case):
    aware = CoordinationLedger(case.ledger.root, installation_id=case.spec.installation_id,
                               maintenance_root=case.control)
    pid = os.fork()
    if pid == 0:
        try:
            aware.close()  # closes child fd; MUST NOT unlock shared parent description
        finally:
            os._exit(0)
    assert os.waitpid(pid, 0)[1] == 0
    with pytest.raises(CoordinationError, match="maintenance_busy"):
        migrate(case)
    aware.close()
