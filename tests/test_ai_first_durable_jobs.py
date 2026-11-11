"""B3.2 offline acceptance: real schema-v2 SQLite, private files, no Qwen."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from averon_import.services.ai_first import dispatch_policy, durable_job_store as jobs
from averon_import.services.ai_first.coordination_contracts import CoordinationError, canonical, fingerprint
from averon_import.services.ai_first.durable_dispatch import HistoricalSettlementContext
from averon_import.services.ai_first.durable_execution_store import PrivateExecutionStore
from averon_import.services.ai_first.durable_job_contracts import (
    ExecutionInput, ExecutionProfile, FinancialSnapshotInput, PageSelection, QueueLimits,
    SubmissionReceipt, SubmissionRequest, parse_execution_bytes, parse_submission_request,
)
from averon_import.services.ai_first.orchestration import OrchestrationConfig, TokenPricing
from averon_import.services.ai_first.private_acl import PrivateAclError, secure_private_path
from b32_test_support import close_case, finish_worker, make_case, new_generation, payload, receive, start_worker


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    monkeypatch.setenv("AVERON_AI_DURABLE_DISPATCH", "1")
    monkeypatch.setenv("QWEN_API_KEY", "")
    monkeypatch.setenv("YANDEX_AI_API_KEY", "")
    monkeypatch.setattr(dispatch_policy, "_required", False)
    def denied(*_args, **_kwargs):
        raise AssertionError("b3_2_network_forbidden")
    import socket
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)


@pytest.fixture
def case(tmp_path):
    value = make_case(tmp_path / "b32", pages=3)
    try:
        yield value
    finally:
        close_case(value)


def exact_input(case, *, profile=None, financial=None, actor=None, request=None):
    from averon_import.services.ai_first.durable_job_contracts import build_execution_input
    return build_execution_input(actor or case.actor, request or case.request,
        financial or case.financial, profile or case.profile)


def stored_rows(case):
    with sqlite3.connect(case.ledger.path) as connection:
        return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("durable_job_snapshots", "durable_jobs", "durable_job_pages", "durable_job_commands")}


def execution_file(case, receipt):
    with sqlite3.connect(case.ledger.path) as connection:
        row = connection.execute("SELECT execution_id,execution_digest,execution_bytes"
                                 " FROM durable_jobs WHERE job_id=?", (receipt.job_id,)).fetchone()
    parent, final, pending = case.execution_store._paths(receipt.job_id, row[0])
    return parent, final, pending


def pause_for_restart(case, receipt):
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("UPDATE durable_jobs SET state='paused_restart',revision=revision+1"
                           " WHERE job_id=?", (receipt.job_id,))


def resume_command_count(case):
    with sqlite3.connect(case.ledger.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM durable_job_commands"
                                  " WHERE kind='resume'").fetchone()[0]


def attempt_count(case):
    with sqlite3.connect(case.ledger.path) as connection:
        return connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]


def job_and_page_revisions(case, job_id):
    with sqlite3.connect(case.ledger.path) as connection:
        job = connection.execute("SELECT state,execution_state,revision,cancel_revision,updated_at"
                                 " FROM durable_jobs WHERE job_id=?", (job_id,)).fetchone()
        pages = connection.execute("SELECT page_number,state,revision,safe_code"
                                   " FROM durable_job_pages WHERE job_id=? ORDER BY page_number",
                                   (job_id,)).fetchall()
        commands = connection.execute("SELECT COUNT(*) FROM durable_job_commands"
                                      " WHERE job_id=?", (job_id,)).fetchone()[0]
    return job, pages, commands


def financial_and_liability_rows(case):
    with sqlite3.connect(case.ledger.path) as connection:
        return {
            "snapshots": connection.execute(
                "SELECT * FROM durable_job_snapshots ORDER BY snapshot_id").fetchall(),
            "attempts": connection.execute(
                "SELECT * FROM attempts ORDER BY attempt_id").fetchall(),
            "unknown_pages": connection.execute(
                "SELECT job_id,page_number,state,attempt_id FROM durable_job_pages"
                " WHERE state IN ('intent','send_committed','settlement_pending','unknown_outcome')"
                " ORDER BY job_id,page_number").fetchall(),
        }


def prepare_pending_after_failed_staging(case):
    def lost_t0(point):
        if point == "after_t0_commit":
            raise OSError("synthetic lost submission receipt")
    submit_backend = jobs.DurableJobStore(case.ledger, case.execution_store, _fault_hook=lost_t0)
    receipt = submit_backend.submit(case.actor, case.request, financial=case.financial,
                                    execution_profile=case.profile)
    assert submit_backend.get_status(case.actor, receipt.job_id).state == "preparing_input"

    def pause_after_pending(point):
        if point == "after_pending_fsync":
            raise OSError("synthetic cancellation barrier")
    paused_store = PrivateExecutionStore(case.execution_store.root, case.installation_id,
                                         _fault_hook=pause_after_pending)
    paused_backend = jobs.DurableJobStore(case.ledger, paused_store)
    with pytest.raises(CoordinationError, match="execution_stage_write_failed"):
        paused_backend.recover_preparing(case.actor, receipt.job_id,
                                         recovery_input=exact_input(case))
    _parent, final, pending = execution_file(case, receipt)
    assert pending.exists() and not final.exists()
    return receipt, final, pending


def test_submit_snapshots_exact_financial_preimages_and_bounded_public_status(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    status = case.backend.get_status(case.actor, receipt.job_id, page_limit=2)
    assert status.state == "queued" and status.publication_state == "not_published"
    assert status.revision == 2 and len(status.pages) == 2 and status.next_page_cursor == 2
    second = case.backend.get_status(case.actor, receipt.job_id, page_after=2, page_limit=2,
                                     expected_revision=status.revision)
    assert [page.page_number for page in second.pages] == [3]
    assert second.next_page_cursor is None and second.revision == status.revision
    serialized = json.dumps(asdict(status))
    for secret in (str(case.pdf), case.profile.prompt_template, case.request.user_context,
                   case.financial.binding.credential_tag):
        assert secret not in serialized
    assert "pricing_digest" not in serialized and "snapshot_digest" not in serialized
    with sqlite3.connect(case.ledger.path) as connection:
        snapshot = connection.execute("SELECT run_json,config_json,binding_json,pricing_digest,snapshot_digest"
                                      " FROM durable_job_snapshots WHERE snapshot_id=?", (receipt.snapshot_id,)).fetchone()
        assert snapshot is not None
        assert json.loads(snapshot[0]) == asdict(case.financial.run)
        assert json.loads(snapshot[1]) == case.financial.config.model_dump(mode="json")
        assert json.loads(snapshot[2]) == {**asdict(case.financial.binding),
                                          "quota_scope_ids": list(case.financial.binding.quota_scope_ids)}
        assert snapshot[3] == fingerprint(case.financial.config.pricing.model_dump(mode="json"))
    with pytest.raises(CoordinationError, match="status_page_invalid"):
        case.backend.get_status(case.actor, receipt.job_id, page_limit=101)
    with pytest.raises(CoordinationError, match="status_revision_changed"):
        case.backend.get_status(case.actor, receipt.job_id, expected_revision=status.revision - 1)
    with pytest.raises(CoordinationError, match="status_revision_changed"):
        case.backend.get_status(case.actor, receipt.job_id, expected_revision=status.revision - 1)
    with pytest.raises(CoordinationError, match="server_authorization_context_required"):
        case.backend.get_status({"tenant_id": case.actor.tenant_id}, receipt.job_id)


def test_exact_operation_replay_ignores_new_settings_but_changed_body_conflicts(case):
    first = case.backend.submit(case.actor, case.request, financial=case.financial,
                                execution_profile=case.profile)
    current_actor, current_run = new_generation(case, document_id=case.actor.document_id)
    expensive_config = case.current_financial.config.model_copy(update={"pricing": TokenPricing(
        profile_id="rotated-settings", input_rub_per_1k=Decimal("99"),
        cached_input_rub_per_1k=Decimal("88"), output_rub_per_1k=Decimal("77"))})
    from averon_import.services.ai_first.durable_qwen_adapter import configuration_digest
    current_run = replace(current_run, config_digest=configuration_digest(expensive_config,
                                                                          case.current_financial.binding))
    # Keep the caller operation body identical; current financial/prompt values
    # are deliberately ignored after the prior command has been durably recorded.
    replay = case.backend.submit(current_actor, case.request,
        financial=FinancialSnapshotInput(current_run, expensive_config, case.current_financial.binding),
        execution_profile=replace(case.profile, prompt_template="A new current prompt."))
    assert replay == first
    with pytest.raises(CoordinationError, match="operation_conflict"):
        case.backend.submit(current_actor, replace(case.request, user_context="different"),
            financial=FinancialSnapshotInput(current_run, expensive_config, case.current_financial.binding),
            execution_profile=case.profile)
    assert stored_rows(case) == {"durable_job_snapshots": 1, "durable_jobs": 1,
                                 "durable_job_pages": 3, "durable_job_commands": 2}


def test_same_operation_id_cannot_be_reused_for_cancel_or_another_owner(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    status = case.backend.get_status(case.actor, receipt.job_id)
    cancel = case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, status.revision)
    with pytest.raises(CoordinationError, match="operation_conflict"):
        case.backend.cancel(case.actor, receipt.job_id, case.request.operation_id, status.revision)
    assert case.backend.submit(case.actor, case.request) == receipt
    other_actor, _ = new_generation(case, owner="other-owner")
    with pytest.raises(CoordinationError, match="durable_job_not_found"):
        case.backend.get_status(other_actor, receipt.job_id)
    assert cancel.state == "cancelled"


def test_duplicate_json_keys_unknown_execution_fields_and_credentials_are_rejected(case):
    raw = ('{"operation_id":"' + uuid4().hex + '","document_id":"document",'
           '"pages":[],"user_context":"","user_context":"second"}')
    with pytest.raises(CoordinationError, match="durable_input_invalid"):
        parse_submission_request(raw)
    execution = exact_input(case)
    value = execution.to_dict()
    value["credential"] = "synthetic-sensitive-value"
    with pytest.raises(CoordinationError, match="execution_input_invalid"):
        ExecutionInput.from_dict(value)
    with pytest.raises(CoordinationError, match="execution_prompt_invalid"):
        ExecutionProfile("qwen", "qwen-vl-synthetic", "fixture-v1", "ocr-schema-v1",
                         "prompt-v1", "Authorization: Bearer synthetic-secret-value")
    with pytest.raises(CoordinationError, match="execution_secret_rejected"):
        replace(case.request, user_context="api_key=syntheticsecret")


def test_cancel_during_preparing_input_prevents_t1_and_frees_active_document(case):
    def pause_after_pending(point):
        if point == "after_pending_fsync":
            raise OSError("synthetic cancellation barrier")
    # First make T0 durable without a file, then let recovery pause after fsync.
    def lost_t0(point):
        if point == "after_t0_commit":
            raise OSError("synthetic lost submission receipt")
    backend = jobs.DurableJobStore(case.ledger, case.execution_store, _fault_hook=lost_t0)
    receipt = backend.submit(case.actor, case.request, financial=case.financial,
                             execution_profile=case.profile)
    assert backend.get_status(case.actor, receipt.job_id).state == "preparing_input"
    paused_execution = PrivateExecutionStore(case.execution_store.root, case.installation_id,
                                             _fault_hook=pause_after_pending)
    paused_backend = jobs.DurableJobStore(case.ledger, paused_execution)
    with pytest.raises(CoordinationError, match="execution_stage_write_failed"):
        paused_backend.recover_preparing(case.actor, receipt.job_id, recovery_input=exact_input(case))
    before = backend.get_status(case.actor, receipt.job_id)
    cancelled = backend.cancel(case.actor, receipt.job_id, uuid4().hex, before.revision)
    assert cancelled.state == "cancelled"
    with pytest.raises(CoordinationError, match="durable_job_state_conflict"):
        backend.recover_preparing(case.actor, receipt.job_id, recovery_input=exact_input(case))
    # Higher authorized generation can submit after the stranded preparation is cancelled.
    actor2, _run2 = new_generation(case, document_id=case.actor.document_id)
    accepted = backend.submit(actor2, replace(case.request, operation_id=uuid4().hex),
                              financial=case.current_financial,
                              execution_profile=case.profile)
    assert accepted.job_id != receipt.job_id


def test_history_snapshot_survives_generation_binding_and_policy_rotation_and_file_removal(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    actor2, run2 = new_generation(case, document_id=case.actor.document_id)
    context = case.backend.load_financial_snapshot(actor2, receipt.job_id)
    restored_run, restored_config = context.materialize()
    assert restored_run == case.financial.run
    assert restored_config == case.financial.config
    assert restored_config.pricing.input_rub_per_1k == Decimal("1")
    assert case.financial.binding.credential_generation == "generation_1"
    with sqlite3.connect(case.ledger.path) as connection:
        row = connection.execute("SELECT execution_id FROM durable_jobs WHERE job_id=?", (receipt.job_id,)).fetchone()
    parent, final, _pending = case.execution_store._paths(receipt.job_id, row[0])
    final.unlink()
    _sync = case.execution_store._sync_parent(parent)
    again = case.backend.load_financial_snapshot(actor2, receipt.job_id)
    assert again == context and run2.generation == 2


@pytest.mark.parametrize("fault,expected", [
    ("before_t0_commit", "no_job"),
    ("after_t0_commit", "preparing_input"),
    ("during_pending", "preparing_input"),
    ("after_pending_fsync", "preparing_input"),
    ("after_final_install", "preparing_input"),
    ("after_final_fsync", "preparing_input"),
    ("after_t1_commit", "queued"),
])
def test_subprocess_crash_at_each_t0_file_and_t1_boundary(case, fault, expected):
    child = start_worker(payload(case, crash_at=fault))
    signal = receive(child)
    assert signal == {"point": fault}
    code, _stderr = finish_worker(child, go=False)
    assert code == 73
    if fault == "before_t0_commit":
        assert stored_rows(case) == {"durable_job_snapshots": 0, "durable_jobs": 0,
                                     "durable_job_pages": 0, "durable_job_commands": 0}
        receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                      execution_profile=case.profile)
    else:
        receipt = case.backend._find_submit(case.actor, case.request)
        assert receipt is not None
        status = case.backend.get_status(case.actor, receipt.job_id)
        assert status.state == expected
        if fault in ("after_t0_commit", "during_pending"):
            with pytest.raises(CoordinationError):
                case.backend.recover_preparing(case.actor, receipt.job_id)
            assert case.backend.get_status(case.actor, receipt.job_id).state == "blocked"
        elif fault in ("after_pending_fsync", "after_final_install", "after_final_fsync"):
            recovered = case.backend.recover_preparing(case.actor, receipt.job_id)
            assert recovered == receipt
            assert case.backend.get_status(case.actor, receipt.job_id).state == "queued"
            if fault == "after_final_install":
                assert not case.execution_store._paths(receipt.job_id, case.backend._job_artifact(
                    case.actor, receipt)["execution_id"])[2].exists()
    if fault == "after_t1_commit":
        assert receipt == case.backend.submit(case.actor, case.request,
            financial=case.financial, execution_profile=case.profile)


@pytest.mark.parametrize("fault", ["after_t0_commit", "after_t1_commit"])
def test_real_commit_receipt_loss_is_resolved_idempotently(case, fault):
    calls = 0
    def lose(point):
        nonlocal calls
        if point == fault:
            calls += 1
            if calls == 1:
                raise OSError("synthetic commit reply lost")
    backend = jobs.DurableJobStore(case.ledger, case.execution_store, _fault_hook=lose)
    receipt = backend.submit(case.actor, case.request, financial=case.financial,
                             execution_profile=case.profile)
    if fault == "after_t0_commit":
        assert backend.get_status(case.actor, receipt.job_id).state == "preparing_input"
        assert backend.recover_preparing(case.actor, receipt.job_id,
            recovery_input=exact_input(case)) == receipt
    assert backend.get_status(case.actor, receipt.job_id).state == "queued"
    assert calls == 1 and backend.submit(case.actor, case.request) == receipt


@pytest.mark.parametrize("damage,code", [
    ("missing", "execution_input_missing"), ("bytes", "execution_input_corrupt"),
    ("hash", "execution_input_corrupt"), ("duplicate_json", "execution_input_corrupt"),
])
def test_missing_or_tampered_ready_input_blocks_without_restoring_prompt(case, damage, code):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    with sqlite3.connect(case.ledger.path) as connection:
        row = connection.execute("SELECT execution_id,execution_digest,execution_bytes FROM durable_jobs"
                                 " WHERE job_id=?", (receipt.job_id,)).fetchone()
    _parent, final, _pending = case.execution_store._paths(receipt.job_id, row[0])
    if damage == "missing":
        final.unlink()
    elif damage == "duplicate_json":
        final.write_bytes(b'{"version":1,"version":1}')
        secure_private_path(final, directory=False)
    elif damage == "bytes":
        final.write_bytes(b"truncated")
        secure_private_path(final, directory=False)
    else:
        data = final.read_bytes()
        final.write_bytes(data.replace(b"Synthetic OCR prompt", b"Tampered OCR prompt", 1))
        secure_private_path(final, directory=False)
    with pytest.raises(CoordinationError, match=code):
        case.backend.recover_preparing(case.actor, receipt.job_id)
    status = case.backend.get_status(case.actor, receipt.job_id)
    assert status.state == "blocked" and all(p.state == "blocked" for p in status.pages)
    public = json.dumps(asdict(status))
    assert "Tampered OCR" not in public and str(final) not in public


def test_corrupt_source_identity_is_rejected_before_t0(case):
    case.pdf.write_bytes(b"not the authorized source bytes")
    with pytest.raises(CoordinationError, match="execution_source_changed"):
        case.backend.submit(case.actor, case.request, financial=case.financial,
                            execution_profile=case.profile)
    assert not any(stored_rows(case).values())


def test_wrong_acl_and_symlink_paths_fail_closed(case, monkeypatch):
    from averon_import.services.ai_first import private_acl
    root = case.execution_store.root
    target = root / "durable-execution-stage.lock"
    try:
        if os.name == "nt":
            with monkeypatch.context() as patch:
                patch.setattr(private_acl, "_sddl", lambda **_kwargs: "D:P(A;;FA;;;WD)")
                private_acl._apply_windows_dacl(target, sid=private_acl._current_user_sid(), directory=False)
        else:
            os.chmod(target, 0o644)
        with pytest.raises(CoordinationError, match="execution_storage_unverified"):
            case.execution_store.lock()
    finally:
        secure_private_path(target, directory=False)
    link = case.root / "source-link.pdf"
    try:
        link.symlink_to(case.pdf)
    except (OSError, NotImplementedError):
        pytest.skip("host cannot create a real file symlink without extra privileges")
    from averon_import.services.ai_first.durable_job_contracts import _verified_document_context
    actor = _verified_document_context(tenant_id=case.actor.tenant_id, owner_id=case.actor.owner_id,
        document_id=case.actor.document_id, generation=case.actor.generation,
        source_digest=case.actor.source_digest, source_size_bytes=case.actor.source_size_bytes,
        source_mtime_ns=case.actor.source_mtime_ns, page_count=case.actor.page_count,
        source_path=link, authorization_digest=fingerprint(["symlink-source"]))
    with pytest.raises(CoordinationError, match="execution_source_unverified"):
        case.backend.submit(actor, case.request, financial=case.financial, execution_profile=case.profile)


def test_page_and_owner_queue_caps_are_transactional(case, monkeypatch):
    monkeypatch.setattr(jobs, "QUEUE_LIMITS", QueueLimits(10, 10, 20, 2))
    request2 = replace(case.request, pages=(PageSelection(1), PageSelection(2)))
    first = case.backend.submit(case.actor, request2, financial=case.financial,
                                execution_profile=case.profile)
    actor2, run2 = new_generation(case, document_id="other_document", owner=case.actor.owner_id)
    financial2 = FinancialSnapshotInput(run2, case.financial.config, case.financial.binding)
    second_request = SubmissionRequest(uuid4().hex, actor2.document_id, (PageSelection(1),), "")
    with pytest.raises(CoordinationError, match="queue_owner_page_limit"):
        case.backend.submit(actor2, second_request, financial=financial2, execution_profile=case.profile)
    assert case.backend.get_status(case.actor, first.job_id).state == "queued"
    assert stored_rows(case)["durable_jobs"] == 1


def test_resume_cannot_turn_unknown_page_into_a_new_paid_attempt(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("UPDATE durable_jobs SET state='paused_restart',revision=revision+1 WHERE job_id=?",
                           (receipt.job_id,))
        connection.execute("UPDATE durable_job_pages SET state='unknown_outcome',revision=revision+1 WHERE job_id=?",
                           (receipt.job_id,))
    status = case.backend.get_status(case.actor, receipt.job_id)
    with pytest.raises(CoordinationError, match="unknown_outcome_not_resumable"):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, status.revision)
    assert case.backend.get_status(case.actor, receipt.job_id).pages[0].state == "unknown_outcome"


def test_new_generation_blocks_stale_queued_job_without_financial_side_effects(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    before = stored_rows(case)
    actor2, _run2 = new_generation(case, document_id=case.actor.document_id)
    with pytest.raises(CoordinationError, match="execution_source_changed"):
        case.backend.read_execution_input(actor2, receipt.job_id)
    status = case.backend.get_status(actor2, receipt.job_id)
    assert status.state == "blocked"
    assert all(page.state == "blocked" for page in status.pages)
    assert all(page.safe_code == "execution_source_changed" for page in status.pages)
    assert stored_rows(case) == before
    assert attempt_count(case) == 0


def test_repeated_stale_generation_reads_keep_blocked_revisions(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    actor2, _run2 = new_generation(case, document_id=case.actor.document_id)
    before_financial = financial_and_liability_rows(case)
    with pytest.raises(CoordinationError, match="execution_source_changed"):
        case.backend.read_execution_input(actor2, receipt.job_id)
    blocked = case.backend.get_status(actor2, receipt.job_id)
    assert blocked.state == "blocked"
    assert all(page.state == "blocked" and page.safe_code == "execution_source_changed"
               for page in blocked.pages)
    revisions = job_and_page_revisions(case, receipt.job_id)
    for _attempt in range(3):
        with pytest.raises(CoordinationError, match="execution_source_changed"):
            case.backend.read_execution_input(actor2, receipt.job_id)
        assert job_and_page_revisions(case, receipt.job_id) == revisions
        assert financial_and_liability_rows(case) == before_financial


def test_cancel_with_corrupt_final_removes_pending_and_preserves_final(case):
    receipt, final, pending = prepare_pending_after_failed_staging(case)
    corrupt = b"synthetic corrupt immutable execution final"
    final.write_bytes(corrupt)
    secure_private_path(final, directory=False)
    before_financial = financial_and_liability_rows(case)
    status = case.backend.get_status(case.actor, receipt.job_id)
    operation = uuid4().hex
    cancelled = case.backend.cancel(case.actor, receipt.job_id, operation, status.revision)
    assert cancelled.state == "cancelled" and cancelled.kind == "cancel"
    assert case.backend.get_status(case.actor, receipt.job_id).state == "cancelled"
    assert not pending.exists()
    assert final.read_bytes() == corrupt
    assert financial_and_liability_rows(case) == before_financial
    assert attempt_count(case) == 0
    with sqlite3.connect(case.ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM durable_job_commands"
                                  " WHERE kind='cancel'").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["io", "acl"])
def test_cancel_returns_saved_receipt_when_pending_cleanup_fails_after_commit(
        case, monkeypatch, failure):
    receipt, _final, pending = prepare_pending_after_failed_staging(case)
    if failure == "io":
        original_unlink = Path.unlink
        def deny_pending(self, *args, **kwargs):
            if self.name.endswith(".pending"):
                raise OSError("synthetic pending cleanup I/O failure")
            return original_unlink(self, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", deny_pending)
    else:
        from averon_import.services.ai_first import durable_execution_store as execution_store_module
        original_verify = execution_store_module.verify_private_path
        def deny_pending(path, directory=False):
            if Path(path).name.endswith(".pending"):
                raise PrivateAclError("synthetic pending cleanup ACL failure")
            return original_verify(path, directory=directory)
        monkeypatch.setattr(execution_store_module, "verify_private_path", deny_pending)
    before_financial = financial_and_liability_rows(case)
    status = case.backend.get_status(case.actor, receipt.job_id)
    operation = uuid4().hex
    cancelled = case.backend.cancel(case.actor, receipt.job_id, operation, status.revision)
    assert cancelled.state == "cancelled"
    assert case.backend.get_status(case.actor, receipt.job_id).state == "cancelled"
    assert pending.exists()  # left as a private orphan for later strict cleanup
    after_commit = job_and_page_revisions(case, receipt.job_id)
    replay = case.backend.cancel(case.actor, receipt.job_id, operation, status.revision)
    assert replay == cancelled
    assert job_and_page_revisions(case, receipt.job_id) == after_commit
    assert pending.exists()
    assert financial_and_liability_rows(case) == before_financial
    with sqlite3.connect(case.ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM durable_job_commands"
                                  " WHERE kind='cancel'").fetchone()[0] == 1


def test_repeated_cancel_returns_saved_receipt_without_new_operation(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    status = case.backend.get_status(case.actor, receipt.job_id)
    operation = uuid4().hex
    first = case.backend.cancel(case.actor, receipt.job_id, operation, status.revision)
    after_first = job_and_page_revisions(case, receipt.job_id)
    replay = case.backend.cancel(case.actor, receipt.job_id, operation, status.revision)
    assert replay == first
    assert job_and_page_revisions(case, receipt.job_id) == after_first
    with sqlite3.connect(case.ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM durable_job_commands"
                                  " WHERE kind='cancel'").fetchone()[0] == 1


def test_cancel_cleanup_preserves_financial_attempts_snapshots_and_unknown_liabilities(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    pause_for_restart(case, receipt)
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        candidate, run_id = connection.execute("SELECT candidate_attempt_id,run_id"
            " FROM durable_job_pages WHERE job_id=? AND page_number=1", (receipt.job_id,)).fetchone()
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate, uuid4().hex, run_id, 1, 1, 1, 1,
             case.financial.binding.binding_id, case.financial.binding.revision,
             case.financial.binding.billing_scope_id, 1, fingerprint(["synthetic-attempt"]), "{}",
             0, 0, 1, None, "intent", None, None, None))
        connection.execute("UPDATE durable_job_pages SET attempt_id=?,state='claimed',claim_seq=1,"
            "owner_epoch=1,revision=revision+1 WHERE job_id=? AND page_number=1",
            (candidate, receipt.job_id))
        connection.execute("UPDATE durable_job_pages SET state='unknown_outcome',revision=revision+1"
            " WHERE job_id=? AND page_number=2", (receipt.job_id,))
    before_financial = financial_and_liability_rows(case)
    before = stored_rows(case)
    status = case.backend.get_status(case.actor, receipt.job_id)
    cancelled = case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, status.revision)
    # B3.4a: a page with unknown_outcome makes the cancelled job blocked; the
    # command transaction never settles B1 or clears the unknown liability.
    assert cancelled.state == "blocked"
    assert financial_and_liability_rows(case) == before_financial
    assert attempt_count(case) == 1
    current = case.backend.get_status(case.actor, receipt.job_id)
    assert current.pages[0].state == "claimed"
    assert current.pages[1].state == "unknown_outcome"
    assert stored_rows(case)["durable_job_commands"] == before["durable_job_commands"] + 1


def test_cancelled_stale_generation_frees_document_for_current_generation(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    actor2, _run2 = new_generation(case, document_id=case.actor.document_id)
    with pytest.raises(CoordinationError, match="execution_source_changed"):
        case.backend.read_execution_input(actor2, receipt.job_id)
    stale = case.backend.get_status(actor2, receipt.job_id)
    cancelled = case.backend.cancel(actor2, receipt.job_id, uuid4().hex, stale.revision)
    assert cancelled.state == "cancelled"
    accepted = case.backend.submit(actor2, replace(case.request, operation_id=uuid4().hex),
        financial=case.current_financial, execution_profile=case.profile)
    assert accepted.job_id != receipt.job_id
    assert case.backend.get_status(actor2, accepted.job_id).state == "queued"
    assert attempt_count(case) == 0


@pytest.mark.parametrize("damage,code", [
    ("missing", "execution_input_missing"), ("tampered", "execution_input_corrupt"),
])
def test_paused_restart_without_attempts_blocks_on_unusable_execution_input(case, damage, code):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    pause_for_restart(case, receipt)
    _parent, final, _pending = execution_file(case, receipt)
    if damage == "missing":
        final.unlink()
    else:
        final.write_bytes(b"tampered private execution input")
        secure_private_path(final, directory=False)
    before_snapshots = stored_rows(case)["durable_job_snapshots"]
    status = case.backend.get_status(case.actor, receipt.job_id)
    with pytest.raises(CoordinationError, match=code):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, status.revision)
    blocked = case.backend.get_status(case.actor, receipt.job_id)
    assert blocked.state == "blocked"
    assert all(page.state == "blocked" for page in blocked.pages)
    assert resume_command_count(case) == 0
    assert stored_rows(case)["durable_job_snapshots"] == before_snapshots
    assert attempt_count(case) == 0


@pytest.mark.parametrize("failure,code", [
    ("source", "execution_source_changed"), ("acl", "execution_source_unverified"),
])
def test_resume_source_identity_or_acl_failure_blocks_without_side_effects(case, monkeypatch,
                                                                          failure, code):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    pause_for_restart(case, receipt)
    if failure == "source":
        case.pdf.write_bytes(b"changed source bytes")
    else:
        def deny(*_args, **_kwargs):
            raise PrivateAclError("synthetic acl failure")
        monkeypatch.setattr(jobs, "verify_private_path", deny)
    before_snapshots = stored_rows(case)["durable_job_snapshots"]
    status = case.backend.get_status(case.actor, receipt.job_id)
    with pytest.raises(CoordinationError, match=code):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, status.revision)
    blocked = case.backend.get_status(case.actor, receipt.job_id)
    assert blocked.state == "blocked"
    assert all(page.state == "blocked" for page in blocked.pages)
    assert resume_command_count(case) == 0
    assert stored_rows(case)["durable_job_snapshots"] == before_snapshots
    assert attempt_count(case) == 0


def test_paused_restart_with_significant_attempt_keeps_recovery_stop(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    pause_for_restart(case, receipt)
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        candidate, run_id = connection.execute("SELECT candidate_attempt_id,run_id"
            " FROM durable_job_pages WHERE job_id=? AND page_number=1", (receipt.job_id,)).fetchone()
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate, uuid4().hex, run_id, 1, 1, 1, 1,
             case.financial.binding.binding_id, case.financial.binding.revision,
             case.financial.binding.billing_scope_id, 1, fingerprint(["synthetic-attempt"]), "{}",
             0, 0, 1, None, "intent", None, None, None))
        connection.execute("UPDATE durable_job_pages SET attempt_id=?,state='claimed',claim_seq=1,"
            "owner_epoch=1,revision=revision+1 WHERE job_id=? AND page_number=1",
            (candidate, receipt.job_id))
    final = execution_file(case, receipt)[1]
    final.unlink()
    before = stored_rows(case)
    status = case.backend.get_status(case.actor, receipt.job_id)
    with pytest.raises(CoordinationError, match="execution_input_requires_attempt_recovery"):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, status.revision)
    current = case.backend.get_status(case.actor, receipt.job_id)
    assert current.state == "paused_restart"
    assert current.pages[0].state == "claimed"
    assert stored_rows(case) == before
    assert resume_command_count(case) == 0
    assert attempt_count(case) == 1


def test_repeated_resume_after_blocked_error_adds_no_commands(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    pause_for_restart(case, receipt)
    execution_file(case, receipt)[1].unlink()
    status = case.backend.get_status(case.actor, receipt.job_id)
    with pytest.raises(CoordinationError, match="execution_input_missing"):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, status.revision)
    blocked = case.backend.get_status(case.actor, receipt.job_id)
    assert blocked.state == "blocked"
    with pytest.raises(CoordinationError, match="job_not_resumable"):
        case.backend.resume(case.actor, receipt.job_id, uuid4().hex, blocked.revision)
    assert case.backend.get_status(case.actor, receipt.job_id).revision == blocked.revision
    assert resume_command_count(case) == 0
    assert attempt_count(case) == 0


def test_document_over_max_pages_is_rejected_before_t0(case):
    from averon_import.services.ai_first.coordination_contracts import Run
    from averon_import.services.ai_first.durable_job_contracts import _verified_document_context
    from averon_import.services.ai_first.durable_qwen_adapter import configuration_digest
    config = case.financial.config.model_copy(update={"max_pages": case.actor.page_count - 1})
    binding = case.financial.binding
    run = Run(uuid4().hex, case.actor.tenant_id, case.actor.document_id, 2,
              case.actor.source_digest, configuration_digest(config, binding),
              case.financial.run.budget_units, case.financial.run.request_cap)
    with case.ledger.dispatcher() as dispatcher:
        case.ledger.register_run(dispatcher, uuid4().hex, run)
    actor = _verified_document_context(tenant_id=case.actor.tenant_id, owner_id=case.actor.owner_id,
        document_id=case.actor.document_id, generation=2, source_digest=case.actor.source_digest,
        source_size_bytes=case.actor.source_size_bytes, source_mtime_ns=case.actor.source_mtime_ns,
        page_count=case.actor.page_count, source_path=case.actor.source_path,
        authorization_digest=fingerprint(["over-max-pages"]))
    request = SubmissionRequest(uuid4().hex, case.actor.document_id, (PageSelection(1),), "")
    with pytest.raises(CoordinationError, match="document_identity_mismatch"):
        case.backend.submit(actor, request, financial=FinancialSnapshotInput(run, config, binding),
                            execution_profile=case.profile)
    assert not any(stored_rows(case).values())
    assert attempt_count(case) == 0


def test_repr_hides_user_context_and_prompt(case):
    request_text = repr(case.request)
    profile_text = repr(case.profile)
    execution_text = repr(exact_input(case))
    assert case.request.user_context not in request_text
    assert case.profile.prompt_template not in profile_text
    assert case.request.user_context not in execution_text
    assert case.profile.prompt_template not in execution_text
    assert "user_context" not in request_text
    assert "prompt_template" not in profile_text


def _release_workers(workers):
    for child in workers:
        child.stdin.write("GO\n")
        child.stdin.flush()
        child.stdin.close()
    outputs = []
    for child in workers:
        code = child.wait(timeout=40)
        stderr = child.stderr.read()
        stdout = child.stdout.read()
        assert code in (0, 2), stderr
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    return outputs


def test_two_processes_same_submit_operation_create_one_durable_job_and_stable_receipt(case):
    workers = [start_worker(payload(case, wait_for_go=True)) for _ in range(2)]
    assert receive(workers[0]) == {"ready": True}
    assert receive(workers[1]) == {"ready": True}
    outputs = _release_workers(workers)
    receipts = [SubmissionReceipt(**item["receipt"]) for item in outputs]
    assert receipts[0] == receipts[1]
    assert stored_rows(case) == {"durable_job_snapshots": 1, "durable_jobs": 1,
                                 "durable_job_pages": 3, "durable_job_commands": 2}
    assert case.backend.get_status(case.actor, receipts[0].job_id).state == "queued"


@pytest.mark.parametrize("limits,other_owner,expected", [
    (QueueLimits(1, 10, 10, 10), True, "queue_global_job_limit"),
    (QueueLimits(10, 1, 10, 10), False, "queue_owner_job_limit"),
    (QueueLimits(10, 10, 1, 10), True, "queue_global_page_limit"),
    (QueueLimits(10, 10, 10, 1), False, "queue_owner_page_limit"),
])
def test_two_processes_cannot_overbook_transactional_queue_caps(case, limits, other_owner, expected):
    actor2, run2 = new_generation(case, document_id="other-document",
                                  owner="other-owner" if other_owner else case.actor.owner_id)
    request1 = replace(case.request, pages=(PageSelection(1),))
    request2 = SubmissionRequest(uuid4().hex, actor2.document_id, (PageSelection(1),), "")
    financial2 = case.current_financial
    workers = [
        start_worker(payload(case, request=request1, queue_limits=limits, wait_for_go=True)),
        start_worker(payload(case, request=request2, actor=actor2, financial=financial2,
                             queue_limits=limits, wait_for_go=True)),
    ]
    assert receive(workers[0]) == {"ready": True}
    assert receive(workers[1]) == {"ready": True}
    outputs = _release_workers(workers)
    accepted = [item for item in outputs if "receipt" in item]
    rejected = [item for item in outputs if "error" in item]
    assert len(accepted) == len(rejected) == 1
    assert rejected[0]["error"] == expected
    assert stored_rows(case)["durable_jobs"] == 1
    assert stored_rows(case)["durable_job_snapshots"] == 1


def test_document_busy_and_generation_uniqueness_are_atomic(case):
    first = case.backend.submit(case.actor, case.request, financial=case.financial,
                                execution_profile=case.profile)
    actor2, _run2 = new_generation(case, document_id=case.actor.document_id)
    request2 = replace(case.request, operation_id=uuid4().hex)
    with pytest.raises(CoordinationError, match="durable_document_busy"):
        case.backend.submit(actor2, request2, financial=case.current_financial,
                            execution_profile=case.profile)
    assert case.backend.get_status(case.actor, first.job_id).state == "queued"
    assert stored_rows(case)["durable_jobs"] == 1


def test_owner_document_context_cannot_read_another_owners_job(case):
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    from averon_import.services.ai_first.durable_job_contracts import _verified_document_context
    wrong_owner = _verified_document_context(tenant_id=case.actor.tenant_id, owner_id="wrong-owner",
        document_id=case.actor.document_id, generation=case.actor.generation,
        source_digest=case.actor.source_digest, source_size_bytes=case.actor.source_size_bytes,
        source_mtime_ns=case.actor.source_mtime_ns, page_count=case.actor.page_count,
        source_path=case.actor.source_path, authorization_digest=fingerprint(["wrong-owner"]))
    with pytest.raises(CoordinationError, match="durable_job_not_found"):
        case.backend.get_status(wrong_owner, receipt.job_id)
    with pytest.raises(CoordinationError, match="durable_job_not_found"):
        case.backend.cancel(wrong_owner, receipt.job_id, uuid4().hex,
                            case.backend.get_status(case.actor, receipt.job_id).revision)
    wrong_tenant = _verified_document_context(tenant_id="other-tenant", owner_id=case.actor.owner_id,
        document_id=case.actor.document_id, generation=case.actor.generation,
        source_digest=case.actor.source_digest, source_size_bytes=case.actor.source_size_bytes,
        source_mtime_ns=case.actor.source_mtime_ns, page_count=case.actor.page_count,
        source_path=case.actor.source_path, authorization_digest=fingerprint(["wrong-tenant"]))
    with pytest.raises(CoordinationError, match="durable_job_not_found"):
        case.backend.get_status(wrong_tenant, receipt.job_id)
    wrong_tenant = _verified_document_context(tenant_id="other-tenant", owner_id=case.actor.owner_id,
        document_id=case.actor.document_id, generation=case.actor.generation,
        source_digest=case.actor.source_digest, source_size_bytes=case.actor.source_size_bytes,
        source_mtime_ns=case.actor.source_mtime_ns, page_count=case.actor.page_count,
        source_path=case.actor.source_path, authorization_digest=fingerprint(["wrong-tenant"]))
    with pytest.raises(CoordinationError, match="durable_job_not_found"):
        case.backend.get_status(wrong_tenant, receipt.job_id)


def test_concurrent_cancel_uses_optimistic_revision_and_one_command(case):
    from concurrent.futures import ThreadPoolExecutor
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    revision = case.backend.get_status(case.actor, receipt.job_id).revision
    def cancel():
        try:
            return case.backend.cancel(case.actor, receipt.job_id, uuid4().hex, revision)
        except CoordinationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: cancel(), range(2)))
    assert sum(type(item) is str and item == "job_revision_conflict" for item in results) == 1
    status = case.backend.get_status(case.actor, receipt.job_id)
    assert status.state == "cancelled" and status.cancel_revision == 1
    with sqlite3.connect(case.ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM durable_job_commands WHERE kind='cancel'").fetchone()[0] == 1


def test_concurrent_resume_uses_optimistic_revision_without_retrying_unknown(case):
    from concurrent.futures import ThreadPoolExecutor
    receipt = case.backend.submit(case.actor, case.request, financial=case.financial,
                                  execution_profile=case.profile)
    with sqlite3.connect(case.ledger.path) as connection:
        connection.execute("UPDATE durable_jobs SET state='paused_restart',revision=revision+1 WHERE job_id=?",
                           (receipt.job_id,))
    revision = case.backend.get_status(case.actor, receipt.job_id).revision
    def resume():
        try:
            return case.backend.resume(case.actor, receipt.job_id, uuid4().hex, revision)
        except CoordinationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: resume(), range(2)))
    assert sum(type(item) is str and item == "job_revision_conflict" for item in results) == 1, results
    assert case.backend.get_status(case.actor, receipt.job_id).state == "queued"
    with sqlite3.connect(case.ledger.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM durable_job_commands WHERE kind='resume'").fetchone()[0] == 1


def test_surrogate_text_is_rejected_as_typed_input_error():
    raw = json.dumps({"operation_id": uuid4().hex, "document_id": "document",
        "pages": [{"page_number": 1, "crop": None}], "user_context": "\ud800"})
    with pytest.raises(CoordinationError, match="durable_input_invalid"):
        parse_submission_request(raw)


def test_large_page_status_is_bounded_and_cursor_stable(tmp_path):
    case = make_case(tmp_path / "large-status", pages=128)
    try:
        request = SubmissionRequest(uuid4().hex, case.actor.document_id,
            tuple(PageSelection(page) for page in range(1, 129)), "")
        receipt = case.backend.submit(case.actor, request, financial=case.financial,
                                      execution_profile=case.profile)
        first = case.backend.get_status(case.actor, receipt.job_id, page_limit=100)
        assert len(first.pages) == 100 and first.next_page_cursor == 100
        second = case.backend.get_status(case.actor, receipt.job_id, page_after=100,
            page_limit=100, expected_revision=first.revision)
        assert len(second.pages) == 28 and second.next_page_cursor is None
        assert first.publication_state == second.publication_state == "not_published"
    finally:
        close_case(case)

