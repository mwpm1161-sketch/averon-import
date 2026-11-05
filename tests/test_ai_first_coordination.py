"""Offline B1 invariants, actual SQLite failures and independent subprocesses."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
import json
import os
from pathlib import Path
import pickle
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from averon_import.services.ai_first import coordination as core
from averon_import.services.ai_first.coordination import CoordinationLedger, system_clock
from averon_import.services.ai_first.coordination_contracts import (
    Attempt, BillingScope, Binding, Bootstrap, ClockStamp, CoordinationError,
    MAX_INTEGER, Outcome, QuotaScope, Run, fingerprint, rub_units,
)
from averon_import.services.ai_first.private_acl import (
    PrivateAclError, secure_private_path,
)

PROOF = "e" * 64
WORKER = Path(__file__).with_name("ai_first_coordination_worker.py")


class FakeClock:
    def __init__(self):
        self.boot, self.ms = "1" * 32, 1000

    def __call__(self):
        return ClockStamp(self.boot, self.ms)


def specification(*, cap=1000, rpm=100, tpm=10000, concurrency=10,
                  complete=True, opening_spent=0, opening_held=0):
    bills = tuple(BillingScope(b, 1, cap, opening_spent, opening_held, complete, PROOF) for b in ("bill_a", "bill_b"))
    quotas = tuple(QuotaScope(q, 1, rpm, tpm, concurrency, 60_000, 0, 0, 0, complete, PROOF)
                   for q in ("quota_a", "quota_b"))
    bindings = (
        Binding("binding_a", 1, "credential_1", "c" * 64, "bill_a", ("quota_a",), "d" * 64),
        Binding("binding_b", 1, "credential_2", "b" * 64, "bill_b", ("quota_a",), "d" * 64),
        Binding("binding_c", 1, "credential_3", "a" * 64, "bill_a", ("quota_b",), "d" * 64),
    )
    return Bootstrap(uuid4().hex, uuid4().hex, PROOF, bills, quotas, bindings, global_concurrency=10)


def setup_ledger(tmp_path, *, real_clock=False, **options):
    spec = specification(**options)
    clock = system_clock if real_clock else FakeClock()
    ledger = CoordinationLedger.bootstrap(tmp_path / "ledger", spec, clock=clock)
    return ledger, spec, clock


def run_spec(*, tenant="tenant", document=None, generation=1, budget=1000, cap=100):
    return Run(uuid4().hex, tenant, document or uuid4().hex, generation, "a" * 64, "b" * 64, budget, cap)


def register(ledger, owner, **options):
    run = run_spec(**options)
    ledger.register_run(owner, run.run_id, run)
    return run


def request(run, binding, *, page=1, tokens=10, cost=100, **updates):
    item = Attempt(uuid4().hex, uuid4().hex, run.run_id, page, 1, 1, binding, 1,
                   tuple((q, 1) for q in binding.quota_scope_ids), "d" * 64, tokens, 0, cost)
    return replace(item, **updates)


def outcome(item, *, state="terminal", actual=50):
    return Outcome(uuid4().hex, item.attempt_id, item.request_digest, PROOF, state, actual)


@contextmanager
def worker(ledger, mode):
    process = subprocess.Popen([sys.executable, str(WORKER), str(ledger.root), ledger.installation_id, mode],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", cwd=WORKER.parents[1])
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def send(process, value):
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


def receive(process):
    # Bounded waits run in a helper; a failed worker cannot hang the suite.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process.stdout.readline)
        try:
            line = future.result(timeout=15)
        except TimeoutError:
            process.kill()
            raise
    if not line:
        pytest.fail(process.stderr.read())
    return json.loads(line)


def test_explicit_bootstrap_open_and_no_auto_create(tmp_path):
    target = tmp_path / "absent"
    with pytest.raises(CoordinationError):
        CoordinationLedger(target, installation_id=uuid4().hex)
    assert not target.exists()
    ledger, spec, _ = setup_ledger(tmp_path)
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == 0
    before = ledger.path.read_bytes()
    with pytest.raises(CoordinationError, match="bootstrap_target_exists"):
        CoordinationLedger.bootstrap(ledger.root, spec)
    assert ledger.path.read_bytes() == before
    with pytest.raises(CoordinationError, match="installation_mismatch"):
        CoordinationLedger(ledger.root, installation_id=uuid4().hex)


@pytest.mark.parametrize("complete,held", [(False, 0), (True, 1)])
def test_bootstrap_missing_history_is_not_zero_spend(tmp_path, complete, held):
    ledger, spec, _ = setup_ledger(tmp_path, complete=complete, opening_spent=200, opening_held=held)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        with pytest.raises(CoordinationError, match="needs_reconciliation"):
            ledger.reserve(owner, request(run, spec.bindings[0]))
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == held
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == 200


@pytest.mark.parametrize("value", [0.1, True, -1, "NaN", "Infinity", "-0.1", "1e100", "1e999999"])
def test_money_never_accepts_float_negative_nonfinite_or_overflow(value):
    with pytest.raises(CoordinationError):
        rub_units(value)


def test_money_rounding_and_signed_integer_boundary():
    assert rub_units("0.0000000001") == 1
    assert rub_units(Decimal("1.123456789")) == 1123456789
    assert rub_units("9223372036.854775807") == MAX_INTEGER
    with pytest.raises(CoordinationError):
        rub_units("9223372036.8547758071")
    assert rub_units("1e-999999999") == 1
    assert rub_units("1." + "0" * 200 + "1") == 1_000_000_001
    with pytest.raises(CoordinationError):
        rub_units("9223372036.854775807" + "0" * 200 + "1")


def test_reservation_idempotency_conflict_and_strict_inputs(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        first = ledger.reserve(owner, item)
        assert first.newly_created
        assert not ledger.reserve(owner, item).newly_created
        with pytest.raises(CoordinationError, match="operation_conflict"):
            ledger.reserve(owner, replace(item, reserve_units=101))
        with pytest.raises(CoordinationError):
            ledger.reserve(owner, replace(item, operation_id=uuid4().hex))
        with pytest.raises(CoordinationError):
            replace(item, reserve_units=1.1)
        with pytest.raises(CoordinationError):
            replace(item, input_tokens=MAX_INTEGER, output_tokens=1)
        with pytest.raises(CoordinationError):
            replace(item, quota_revisions=((),))
    assert len(ledger.snapshot()["attempts"]) == 1
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == 100


def test_multiple_quota_reservations_are_one_transaction(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, rpm=1)
    both = replace(spec.bindings[0], binding_id="both", quota_scope_ids=("quota_a", "quota_b"))
    with ledger.dispatcher() as owner:
        ledger.put_binding(owner, uuid4().hex, both)
        run = register(ledger, owner)
        ledger.reserve(owner, request(run, spec.bindings[2]))
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, both, page=2))
        # The failed check on quota_b did NOT consume quota_a or money.
        ledger.reserve(owner, request(run, spec.bindings[0], page=3))
    assert len(ledger.snapshot()["attempts"]) == 2
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == 200


def test_different_billing_scopes_share_quota(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, rpm=1)
    with ledger.dispatcher() as owner:
        first, second = register(ledger, owner), register(ledger, owner)
        ledger.reserve(owner, request(first, spec.bindings[0]))
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(second, spec.bindings[1]))
    assert ledger.snapshot()["billing"]["bill_b"]["held_units"] == 0


def test_same_billing_scope_shares_budget_across_quotas(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, cap=150)
    with ledger.dispatcher() as owner:
        first, second = register(ledger, owner), register(ledger, owner)
        ledger.reserve(owner, request(first, spec.bindings[0]))
        with pytest.raises(CoordinationError, match="billing_budget_exhausted"):
            ledger.reserve(owner, request(second, spec.bindings[2]))


@pytest.mark.parametrize("limit", ["rpm", "tpm", "concurrency", "budget"])
def test_concurrent_connections_cannot_oversubscribe(tmp_path, limit):
    options = {"rpm": 2} if limit == "rpm" else {"tpm": 20} if limit == "tpm" else {"concurrency": 2} if limit == "concurrency" else {"cap": 200}
    ledger, spec, _ = setup_ledger(tmp_path, **options)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        items = [request(run, spec.bindings[0], page=p) for p in range(1, 11)]

        def reserve(item):
            try:
                ledger.reserve(owner, item)
                return True
            except CoordinationError as exc:
                assert exc.code in {"quota_exhausted", "billing_budget_exhausted"}
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            assert sum(pool.map(reserve, items)) == 2
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == 200


def test_document_generation_and_lease_fencing(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        old = register(ledger, owner, document="doc")
        register(ledger, owner, document="doc", generation=2)
        with pytest.raises(CoordinationError, match="run_fenced"):
            ledger.reserve(owner, request(old, spec.bindings[0]))
        with pytest.raises(TypeError):
            pickle.dumps(owner)
        clock.ms += ledger.lease_ms
        with pytest.raises(CoordinationError, match="owner_fenced"):
            owner.renew()
        with pytest.raises(CoordinationError, match="dispatcher_busy"):
            CoordinationLedger(ledger.root, installation_id=spec.installation_id, clock=clock).dispatcher()
    with ledger.dispatcher() as replacement:
        with pytest.raises(CoordinationError, match="owner_fenced"):
            ledger.register_run(owner, uuid4().hex, run_spec())
        assert replacement.epoch > owner.epoch


def test_rotation_and_policy_revision_keep_expenses_and_cooldown(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path, cap=150, rpm=1)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(item, actual=100))
        new = replace(spec.bindings[0], revision=2, credential_generation="rotated", credential_tag="f" * 64)
        ledger.put_binding(owner, uuid4().hex, new)
        with pytest.raises(CoordinationError, match="binding_mismatch"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2, cost=1))
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, new, page=2, cost=1))
        ledger.revise_billing(owner, uuid4().hex, "bill_a", 1, 150)
        with pytest.raises(CoordinationError, match="policy_revision_mismatch"):
            ledger.reserve(owner, request(run, new, page=2, cost=1))
        clock.ms += 10_000
        owner.renew()
        with pytest.raises(CoordinationError, match="billing_budget_exhausted"):
            ledger.reserve(owner, request(run, new, page=2, cost=100, billing_revision=2))
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == 100


def test_policy_longer_window_cannot_forget_old_requests(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path, rpm=1)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(item))
        clock.ms += 10_000
        owner.renew()
        ledger.revise_quota(owner, uuid4().hex, "quota_a", 1, requests=1, tokens=10000, concurrency=10, window_ms=120_000)
        with pytest.raises(CoordinationError, match="policy_revision_mismatch"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2))
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2, quota_revisions=(("quota_a", 2),)))


def test_outcomes_keep_unknown_cost_and_never_clear_pause(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        a, b = request(run, spec.bindings[0]), request(run, spec.bindings[0], page=2)
        ledger.reserve(owner, a)
        ledger.reserve(owner, b)
        unknown = outcome(a, state="unknown", actual=None)
        ledger.record_outcome(owner, unknown)
        ledger.record_outcome(owner, unknown)
        with pytest.raises(CoordinationError, match="not_sent_unproven"):
            ledger.record_outcome(owner, outcome(a, state="not_sent", actual=None))
        ledger.record_outcome(owner, outcome(a, actual=60))
        ledger.record_outcome(owner, outcome(b, actual=None))
    summary = ledger.snapshot()["billing"]["bill_a"]
    assert summary["spent_units"] == 60 and summary["held_units"] == 100 and summary["blocked"]


def test_actual_over_reservation_is_preserved_and_stops_account(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(item, actual=MAX_INTEGER))
        ledger.revise_billing(owner, uuid4().hex, "bill_a", 1, MAX_INTEGER)
        with pytest.raises(CoordinationError, match="needs_reconciliation"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2, billing_revision=2))
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == MAX_INTEGER


def test_not_sent_releases_only_current_owned_intent(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, rpm=1)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner, cap=1)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        closed = outcome(item, state="not_sent", actual=None)
        ledger.record_outcome(owner, closed)
        ledger.record_outcome(owner, closed)
        ledger.reserve(owner, request(run, spec.bindings[0], page=2))
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == 100


def test_same_boot_restart_preserves_spend_and_does_not_grant_replay(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(item, actual=70))
    reopened = CoordinationLedger(ledger.root, installation_id=spec.installation_id, clock=clock)
    with reopened.dispatcher() as new_owner:
        assert not reopened.reserve(new_owner, item).newly_created
    assert reopened.snapshot()["billing"]["bill_a"]["spent_units"] == 70


def test_reboot_quarantines_terminal_quota_and_detects_clock_regression(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path, rpm=1)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(item))
    clock.boot, clock.ms = "2" * 32, 5
    with ledger.dispatcher() as owner:
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2))
        clock.ms = 4
        with pytest.raises(CoordinationError, match="clock_regressed"):
            owner.renew()
        clock.ms = 20_000
        owner.renew()
        clock.ms = 40_000
        owner.renew()
        clock.ms = 60_005
        owner.renew()
        ledger.reserve(owner, request(run, spec.bindings[0], page=2))


def test_process_lock_is_exclusive_and_kill_recovers_unknown_without_refund(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, real_clock=True)
    with worker(ledger, "hold") as child:
        old_epoch = receive(child)["epoch"]
        run = run_spec()
        item = request(run, spec.bindings[0])
        send(child, {"action": "reserve", "run": asdict(run), "attempt": asdict(item)})
        assert receive(child) == {"reserved": True}
        with pytest.raises(CoordinationError, match="dispatcher_busy"):
            ledger.dispatcher()
        with pytest.raises(CoordinationError, match="owner_required"):
            ledger.reserve(None, request(run, spec.bindings[0], page=2))
        child.kill()
        child.wait(timeout=10)
    reopened = CoordinationLedger(ledger.root, installation_id=spec.installation_id)
    with reopened.dispatcher() as owner:
        assert owner.epoch > old_epoch
        assert reopened.snapshot()["attempts"][0]["state"] == "unknown"
        with pytest.raises(CoordinationError, match="needs_reconciliation"):
            reopened.reserve(owner, request(run, spec.bindings[0], page=2))
    assert reopened.snapshot()["billing"]["bill_a"]["held_units"] == 100


@pytest.mark.parametrize("limit", ["rpm", "tpm", "budget"])
def test_two_processes_preserve_global_limits_across_owner_handoffs(tmp_path, limit):
    options = {"rpm": 2} if limit == "rpm" else {"tpm": 20} if limit == "tpm" else {"cap": 200}
    ledger, spec, _ = setup_ledger(tmp_path, real_clock=True, **options)
    with worker(ledger, "race") as first, worker(ledger, "race") as second:
        for process in (first, second):
            send(process, {"binding": asdict(spec.bindings[0]), "run": asdict(run_spec()), "tokens": 10, "cost": 100})
        results = [receive(first), receive(second)]
        assert sum(r["successes"] for r in results) == 2
        for process in (first, second):
            assert process.wait(timeout=10) == 0
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == 200


def test_boot_identity_is_really_shared_between_processes(tmp_path):
    ledger, _spec, _ = setup_ledger(tmp_path, real_clock=True)
    before = system_clock()
    with worker(ledger, "clock") as process:
        stamp = ClockStamp(**receive(process))
    after = system_clock()
    assert before.boot_id == stamp.boot_id == after.boot_id
    assert before.milliseconds <= stamp.milliseconds <= after.milliseconds


def test_sqlite_busy_from_independent_process_is_atomic_and_recoverable(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, real_clock=True)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        with worker(ledger, "sql_lock") as process:
            assert receive(process) == {"locked": True}
            with pytest.raises(CoordinationError, match="ledger_busy"):
                ledger.reserve(owner, item)
            send(process, {})
            assert process.wait(timeout=10) == 0
        assert ledger.snapshot()["attempts"] == []
        assert ledger.reserve(owner, item).newly_created


@pytest.mark.parametrize("failure", ["readonly", "after_commit", "disk_full"])
def test_sqlite_write_failure_and_lost_commit_receipt_fail_closed(tmp_path, monkeypatch, failure):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        original = core.sqlite3.connect

        class LostReceipt(sqlite3.Connection):
            def commit(self):
                if failure == "disk_full":
                    error = sqlite3.OperationalError("synthetic_disk_full")
                    error.sqlite_errorcode = sqlite3.SQLITE_FULL
                    raise error
                super().commit()
                raise sqlite3.OperationalError("synthetic_lost_receipt")

        def connect(*args, **kwargs):
            if failure in ("after_commit", "disk_full"):
                kwargs["factory"] = LostReceipt
            connection = original(*args, **kwargs)
            if failure == "readonly":
                connection.execute("PRAGMA query_only=ON")
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(core.sqlite3, "connect", connect)
            with pytest.raises(CoordinationError, match="ledger_unavailable"):
                ledger.reserve(owner, item)
        replay = ledger.reserve(owner, item)
        assert replay.newly_created == (failure != "after_commit")
    assert len(ledger.snapshot()["attempts"]) == 1


@pytest.mark.parametrize("failure", ["corruption", "future_version", "schema", "missing_lock"])
def test_invalid_existing_ledger_never_repairs_or_reinitializes(tmp_path, failure):
    ledger, spec, _ = setup_ledger(tmp_path)
    if failure == "corruption":
        ledger.path.write_bytes(b"not sqlite")
    elif failure == "missing_lock":
        ledger.lock_path.unlink()
    else:
        with sqlite3.connect(ledger.path) as connection:
            connection.execute("PRAGMA user_version=2" if failure == "future_version" else "DROP INDEX billing_attempts")
    before = ledger.path.read_bytes()
    with pytest.raises(CoordinationError):
        CoordinationLedger(ledger.root, installation_id=spec.installation_id)
    assert ledger.path.read_bytes() == before
    assert not ledger.lock_path.exists() if failure == "missing_lock" else True


def test_actual_bad_acl_is_rejected_without_repair(tmp_path, monkeypatch):
    from averon_import.services.ai_first import private_acl
    ledger, spec, _ = setup_ledger(tmp_path)
    try:
        if os.name == "nt":
            with monkeypatch.context() as patch:
                patch.setattr(private_acl, "_sddl", lambda **_kwargs: "D:P(A;;FA;;;WD)")
                with pytest.raises(PrivateAclError):
                    secure_private_path(ledger.path, directory=False)
        else:
            os.chmod(ledger.path, 0o644)
        with pytest.raises(CoordinationError):
            CoordinationLedger(ledger.root, installation_id=spec.installation_id)
    finally:
        secure_private_path(ledger.path, directory=False)


@pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux fork/flock; no emulation")
def test_linux_fork_cannot_reuse_or_unlock_parent_lease(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path, real_clock=True)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                try:
                    ledger.reserve(owner, request(run, spec.bindings[0]))
                except CoordinationError as exc:
                    os.write(write_fd, exc.code.encode())
                owner.close()
            finally:
                os._exit(0)
        os.close(write_fd)
        assert os.read(read_fd, 100) == b"owner_fenced"
        os.close(read_fd)
        assert os.waitpid(pid, 0)[1] == 0
        with pytest.raises(CoordinationError, match="dispatcher_busy"):
            ledger.dispatcher()
        ledger.reserve(owner, request(run, spec.bindings[0]))


def test_sql_constraints_prevent_history_erasure_and_float_money(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        with sqlite3.connect(ledger.path) as connection:
            for sql in ("DELETE FROM attempts", "DELETE FROM operations",
                        "UPDATE attempts SET reserve_units=0", "UPDATE billing_scopes SET ceiling_units=1.5"):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql)
    assert ledger.snapshot()["billing"]["bill_a"]["held_units"] == 100


def test_global_concurrency_spans_distinct_billing_and_quota_scopes(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        first, second = register(ledger, owner), register(ledger, owner)
        for page in range(1, 11):
            ledger.reserve(owner, request(first if page % 2 else second,
                spec.bindings[0] if page % 2 else spec.bindings[2], page=page, cost=1))
        with pytest.raises(CoordinationError, match="global_capacity_exhausted"):
            ledger.reserve(owner, request(second, spec.bindings[1], page=11, cost=1))
    assert len(ledger.snapshot()["attempts"]) == 10


def test_default_core_cap_is_one_and_never_reads_live_settings(tmp_path):
    spec = replace(specification(), global_concurrency=1)
    ledger = CoordinationLedger.bootstrap(tmp_path / "ledger", spec, clock=FakeClock())
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        ledger.reserve(owner, request(run, spec.bindings[0]))
        with pytest.raises(CoordinationError, match="global_capacity_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2))
    with pytest.raises(CoordinationError, match="invalid_global_concurrency"):
        replace(spec, global_concurrency=11)


def test_opening_quota_and_original_window_survive_policy_change(tmp_path):
    spec = specification(rpm=1)
    spec = replace(spec, quotas=(replace(spec.quotas[0], opening_requests=1), spec.quotas[1]))
    clock = FakeClock()
    ledger = CoordinationLedger.bootstrap(tmp_path / "ledger", spec, clock=clock)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0]))
        ledger.revise_quota(owner, uuid4().hex, "quota_a", 1, requests=1, tokens=10000, concurrency=10, window_ms=1000)
        clock.ms += 2000
        with pytest.raises(CoordinationError, match="quota_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], quota_revisions=(("quota_a", 2),)))


def test_document_budget_and_request_count_are_independent_of_billing_budget(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner, budget=100, cap=1)
        item = request(run, spec.bindings[0])
        ledger.reserve(owner, item)
        with pytest.raises(CoordinationError, match="run_budget_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2))
        ledger.record_outcome(owner, outcome(item, actual=1))
        with pytest.raises(CoordinationError, match="request_cap_exhausted"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=2, cost=1))


def test_expiry_at_commit_rolls_back_attempt_and_operation(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        stamps = iter((clock(), ClockStamp(clock.boot, clock.ms + ledger.lease_ms)))
        ledger.clock = lambda: next(stamps)
        with pytest.raises(CoordinationError, match="owner_fenced"):
            ledger.reserve(owner, item)
        ledger.clock = clock
        assert ledger.snapshot()["attempts"] == []
        assert ledger.reserve(owner, item).newly_created


def test_failed_owner_acquisition_releases_os_lock(tmp_path, monkeypatch):
    ledger, spec, _ = setup_ledger(tmp_path)
    original = core.sqlite3.connect

    def readonly(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.execute("PRAGMA query_only=ON")
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(core.sqlite3, "connect", readonly)
        with pytest.raises(CoordinationError, match="ledger_unavailable"):
            ledger.dispatcher()
    with ledger.dispatcher() as owner:
        assert owner.epoch == 1
        register(ledger, owner)


def test_live_schema_changes_fail_closed_for_existing_ledger_object(tmp_path):
    ledger, _spec, _ = setup_ledger(tmp_path)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DROP INDEX billing_attempts")
    with pytest.raises(CoordinationError, match="schema_mismatch"):
        ledger.dispatcher()


def test_failed_bootstrap_cannot_be_silently_resumed_or_replaced(tmp_path):
    spec = specification()
    spec = replace(spec, bindings=(replace(spec.bindings[0], billing_scope_id="unknown"),))
    root = tmp_path / "failed"
    with pytest.raises(CoordinationError, match="unknown_billing_scope"):
        CoordinationLedger.bootstrap(root, spec)
    with pytest.raises(CoordinationError):
        CoordinationLedger(root, installation_id=spec.installation_id)
    with pytest.raises(CoordinationError, match="bootstrap_target_exists"):
        CoordinationLedger.bootstrap(root, specification())


def test_import_is_inert_and_loads_no_provider_or_application(tmp_path):
    data_root = tmp_path / "must_not_exist"
    script = (
        "import os,sys; from pathlib import Path; "
        "os.environ['AVERON_DATA_DIR']=sys.argv[1]; "
        "import averon_import.services.ai_first.coordination; "
        "assert 'averon_import.main' not in sys.modules; "
        "assert 'averon_import.services.ocr.qwen_vision_client' not in sys.modules; "
        "assert not Path(sys.argv[1]).exists()"
    )
    result = subprocess.run([sys.executable, "-c", script, str(data_root)], cwd=WORKER.parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not data_root.exists()


@pytest.mark.parametrize("field,value", [("reserve_units", 0), ("reserve_units", True),
                                         ("input_tokens", 0.5), ("claim_seq", 0)])
def test_invalid_attempt_contract_cannot_reach_sqlite(field, value):
    spec = specification()
    run = run_spec()
    with pytest.raises(CoordinationError):
        request(run, spec.bindings[0], **{field: value})


@pytest.mark.parametrize("window", ["before", "after"])
def test_real_process_death_at_sqlite_commit_boundary(tmp_path, window):
    ledger, spec, _ = setup_ledger(tmp_path, real_clock=True)
    run = run_spec()
    item = request(run, spec.bindings[0])
    mode = f"crash_{window}_commit"
    with worker(ledger, mode) as process:
        send(process, {"run": asdict(run), "attempt": asdict(item)})
        assert receive(process) == {"window": mode}
        process.kill()
        process.wait(timeout=10)
    recovered = CoordinationLedger(ledger.root, installation_id=spec.installation_id)
    with recovered.dispatcher() as owner:
        if window == "before":
            assert recovered.snapshot()["attempts"] == []
            assert recovered.snapshot()["billing"]["bill_a"]["held_units"] == 0
            assert recovered.reserve(owner, item).newly_created
        else:
            assert recovered.snapshot()["attempts"][0]["state"] == "unknown"
            assert recovered.snapshot()["billing"]["bill_a"]["held_units"] == 100
            assert not recovered.reserve(owner, item).newly_created
            with pytest.raises(CoordinationError, match="needs_reconciliation"):
                recovered.reserve(owner, request(run, spec.bindings[0], page=2))


def test_slow_commit_cannot_return_fresh_receipt_after_lease_expiry(tmp_path, monkeypatch):
    ledger, spec, clock = setup_ledger(tmp_path)
    owner = ledger.dispatcher()
    try:
        run = register(ledger, owner)
        item = request(run, spec.bindings[0])
        original = core.sqlite3.connect

        class SlowCommit(sqlite3.Connection):
            def commit(self):
                super().commit()
                clock.ms += ledger.lease_ms

        def connect(*args, **kwargs):
            kwargs["factory"] = SlowCommit
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(core.sqlite3, "connect", connect)
            with pytest.raises(CoordinationError, match="owner_fenced"):
                ledger.reserve(owner, item)
    finally:
        owner.close()
    with ledger.dispatcher() as replacement:
        assert not ledger.reserve(replacement, item).newly_created
        assert ledger.snapshot()["attempts"][0]["state"] == "unknown"


def test_storage_replacement_is_not_silently_accepted(tmp_path):
    ledger, _spec, _ = setup_ledger(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(ledger.path.read_bytes())
    secure_private_path(replacement, directory=False)
    os.replace(replacement, ledger.path)
    with pytest.raises(CoordinationError, match="storage_replaced"):
        ledger.dispatcher()


def _sealed_case(ledger, spec, owner):
    run = register(ledger, owner)
    missing_usage = request(run, spec.bindings[0])
    later_terminal = request(run, spec.bindings[0], page=2)
    ledger.reserve(owner, missing_usage)
    ledger.reserve(owner, later_terminal)
    ledger.record_outcome(owner, outcome(missing_usage, actual=None))
    ledger.record_outcome(owner, outcome(later_terminal, state="unknown", actual=None))
    ledger.record_outcome(owner, outcome(later_terminal, actual=70))
    return run, missing_usage


def _history_rows(connection):
    """All original v1 tables except mutable ownership metadata, including receipts."""
    names = ("billing_scopes", "quota_scopes", "credential_bindings", "binding_quotas",
             "documents", "runs", "attempts", "attempt_quota_reservations", "operations")
    return {name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for name in names}


def _test_only_v2_migration_witness(connection, *, fail_at=None):
    """Existence/atomicity proof, NOT production B6 DDL, validators or an API.

    Additive receipts/views can express late cost and approved reopen decisions
    while every v1 financial row/trigger stays immutable. B6 must separately
    implement and review evidence, exit gates, admin authorization and readers.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        history_digest = fingerprint(_history_rows(connection))
        connection.execute("CREATE TABLE v2_cost_receipts (receipt_id TEXT PRIMARY KEY,"
                           " attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),"
                           " actual_units INTEGER NOT NULL CHECK(actual_units>=0),"
                           " evidence_digest TEXT NOT NULL) STRICT")
        connection.execute("CREATE TABLE v2_reopen_receipts (receipt_id TEXT PRIMARY KEY,"
                           " scope_kind TEXT NOT NULL CHECK(scope_kind IN ('billing','quota')),"
                           " scope_id TEXT NOT NULL, exit_gates_digest TEXT NOT NULL,"
                           " UNIQUE(scope_kind,scope_id)) STRICT")
        connection.execute("CREATE TABLE v2_migration_receipt (source_history_digest TEXT NOT NULL,"
                           " from_version INTEGER NOT NULL CHECK(from_version=1),"
                           " to_version INTEGER NOT NULL CHECK(to_version=2)) STRICT")
        for table in ("v2_cost_receipts", "v2_reopen_receipts", "v2_migration_receipt"):
            for action in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER {table}_{action} BEFORE {action} ON {table} "
                                   "BEGIN SELECT RAISE(ABORT,'immutable_v2_receipt'); END")
        if fail_at == "ddl":
            raise RuntimeError("migration_fault")
        meta_sql = connection.execute("SELECT sql FROM sqlite_schema WHERE name='meta'").fetchone()[0]
        columns = [row[1] for row in connection.execute("PRAGMA table_info(meta)")]
        connection.execute("ALTER TABLE meta RENAME TO meta_v1_history")
        connection.execute(meta_sql.replace("schema_version=1", "schema_version=2"))
        projection = ",".join("2" if name == "schema_version" else name for name in columns)
        connection.execute(f"INSERT INTO meta SELECT {projection} FROM meta_v1_history")
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER old_meta_{action} BEFORE {action} ON meta_v1_history "
                               "BEGIN SELECT RAISE(ABORT,'immutable_v1_meta'); END")
        if fail_at == "meta":
            raise RuntimeError("migration_fault")
        connection.execute("CREATE VIEW v2_effective_cost AS SELECT a.attempt_id,"
                           " COALESCE(r.actual_units,a.actual_units) AS actual_units,"
                           " CASE WHEN COALESCE(r.actual_units,a.actual_units) IS NULL"
                           " THEN a.reserve_units ELSE 0 END AS held_units"
                           " FROM attempts a LEFT JOIN v2_cost_receipts r USING(attempt_id)")
        connection.execute("CREATE VIEW v2_effective_stops AS"
                           " SELECT 'billing' AS kind,b.scope_id,CASE WHEN r.receipt_id IS NULL"
                           " THEN b.blocked ELSE 0 END AS blocked FROM billing_scopes b"
                           " LEFT JOIN v2_reopen_receipts r ON r.scope_kind='billing' AND r.scope_id=b.scope_id"
                           " UNION ALL SELECT 'quota',q.scope_id,CASE WHEN r.receipt_id IS NULL"
                           " THEN q.blocked ELSE 0 END FROM quota_scopes q"
                           " LEFT JOIN v2_reopen_receipts r ON r.scope_kind='quota' AND r.scope_id=q.scope_id")
        connection.execute("DROP VIEW schema_contract")
        connection.execute("CREATE VIEW schema_contract AS SELECT 2 AS schema_version,"
                           " 2 AS reconciliation_min_schema_version,'sealed_history' AS history_policy,"
                           " 'append_only_receipts' AS reconciliation_upgrade")
        connection.execute("INSERT INTO v2_migration_receipt VALUES(?,1,2)", (history_digest,))
        connection.execute("PRAGMA user_version=2")
        if fail_at == "version":
            raise RuntimeError("migration_fault")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def test_v1_schema_explicitly_refuses_reconciliation_and_keeps_history(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    contract = ledger.schema_contract()
    assert contract == {"schema_version": 1, "reconciliation_min_schema_version": 2,
                        "history_policy": "sealed_history",
                        "reconciliation_upgrade": "explicit_transactional_migration"}
    with ledger.dispatcher() as owner:
        _run, missing = _sealed_case(ledger, spec, owner)
        with ledger._connection() as connection:
            before = _history_rows(connection)
            for sql, parameters in (
                ("UPDATE attempts SET actual_units=40 WHERE attempt_id=?", (missing.attempt_id,)),
                ("UPDATE billing_scopes SET blocked=0 WHERE scope_id='bill_a'", ()),
                ("UPDATE quota_scopes SET blocked=0 WHERE scope_id='quota_a'", ()),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(sql, parameters)
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("UPDATE schema_contract SET reconciliation_min_schema_version=1")
            assert _history_rows(connection) == before
        with pytest.raises(CoordinationError, match="reconciliation_requires_schema_v2"):
            ledger.require_reconciliation_schema()
    assert ledger.snapshot()["billing"]["bill_a"] == {
        "spent_units": 70, "held_units": 100, "ceiling_units": 1000, "blocked": True}


@pytest.mark.parametrize("fail_at", ["ddl", "meta", "version"])
def test_explicit_v2_migration_witness_rolls_back_every_boundary(tmp_path, fail_at):
    ledger, spec, clock = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        _sealed_case(ledger, spec, owner)
        with ledger._connection() as connection:
            before = _history_rows(connection)
            before_schema = core._schema_digest(connection)
            with pytest.raises(RuntimeError, match="migration_fault"):
                _test_only_v2_migration_witness(connection, fail_at=fail_at)
            assert _history_rows(connection) == before
            assert core._schema_digest(connection) == before_schema
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    reopened = CoordinationLedger(ledger.root, installation_id=spec.installation_id, clock=clock)
    assert reopened.snapshot()["billing"]["bill_a"]["held_units"] == 100
    assert reopened.schema_contract()["schema_version"] == 1


def test_v2_witness_can_refine_and_reopen_without_rewriting_v1_rows(tmp_path):
    ledger, spec, clock = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run, missing = _sealed_case(ledger, spec, owner)
        # Capture the LATEST ledger, not an older pre-spend backup.
        with ledger._connection() as connection:
            before = _history_rows(connection)
            old_meta = connection.execute("SELECT * FROM meta").fetchall()
            _test_only_v2_migration_witness(connection)
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert connection.execute("SELECT * FROM meta_v1_history").fetchall() == old_meta
            assert connection.execute("SELECT schema_version,epoch FROM meta").fetchone() == (2, owner.epoch)
            assert _history_rows(connection) == before
            assert connection.execute("SELECT source_history_digest FROM v2_migration_receipt").fetchone()[0] == fingerprint(before)
            # TEST-ONLY approved facts. Real B6 must verify evidence/admin/exit gates.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO v2_cost_receipts VALUES(?,?,40,?)",
                               (uuid4().hex, missing.attempt_id, PROOF))
            connection.execute("INSERT INTO v2_reopen_receipts VALUES(?,'billing','bill_a',?)", (uuid4().hex, PROOF))
            connection.execute("INSERT INTO v2_reopen_receipts VALUES(?,'quota','quota_a',?)", (uuid4().hex, PROOF))
            connection.commit()
            assert connection.execute("SELECT actual_units,held_units FROM v2_effective_cost WHERE attempt_id=?",
                                      (missing.attempt_id,)).fetchone() == (40, 0)
            assert connection.execute("SELECT actual_units FROM attempts WHERE attempt_id=?",
                                      (missing.attempt_id,)).fetchone()[0] is None
            assert connection.execute("SELECT blocked FROM v2_effective_stops WHERE scope_id IN ('bill_a','quota_a')").fetchall() == [(0,), (0,)]
            assert connection.execute("SELECT blocked FROM billing_scopes WHERE scope_id='bill_a'").fetchone()[0] == 1
            for table in ("v2_cost_receipts", "v2_reopen_receipts", "v2_migration_receipt", "meta_v1_history"):
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(f"DELETE FROM {table}")
            assert _history_rows(connection) == before
        # Cached and freshly opened v1 readers BOTH refuse the new accounting.
        with pytest.raises(CoordinationError, match="schema_mismatch"):
            ledger.reserve(owner, request(run, spec.bindings[0], page=3))
        with pytest.raises(CoordinationError, match="schema_mismatch"):
            CoordinationLedger(ledger.root, installation_id=spec.installation_id, clock=clock)


def test_streamed_liability_retains_exact_totals_above_sqlite_int64(tmp_path):
    ledger, spec, _ = setup_ledger(tmp_path)
    with ledger.dispatcher() as owner:
        run = register(ledger, owner)
        items = [request(run, spec.bindings[0], page=page) for page in range(1, 6)]
        for item in items:
            ledger.reserve(owner, item)
        ledger.record_outcome(owner, outcome(items[0], actual=MAX_INTEGER))
        ledger.record_outcome(owner, outcome(items[1], actual=MAX_INTEGER))
        ledger.record_outcome(owner, outcome(items[2], state="not_sent", actual=None))
        ledger.record_outcome(owner, outcome(items[3], actual=None))
        ledger.record_outcome(owner, outcome(items[4], actual=0))
        with ledger._connection() as connection:
            assert ledger._liability(connection, "run_id", run.run_id) == (2 * MAX_INTEGER, 100)
            assert ledger._liability(connection, "billing_scope_id", "bill_a") == (2 * MAX_INTEGER, 100)
    assert ledger.snapshot()["billing"]["bill_a"]["spent_units"] == 2 * MAX_INTEGER
