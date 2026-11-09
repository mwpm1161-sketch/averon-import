"""B3.2 bounded SQLite job storage. No scheduler, API route or provider calls."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from uuid import UUID, uuid5
import os

from .coordination import CoordinationLedger
from .coordination_contracts import CoordinationError, Binding, Run, canonical, fingerprint, uuid_hex
from .durable_dispatch import HistoricalSettlementContext
from .durable_execution_store import PrivateExecutionStore, _open_readonly
from .private_acl import PrivateAclError, verify_private_path, _reject_reparse
from .durable_job_contracts import (
    AuthorizedDocumentContext, CommandReceipt, ExecutionInput, ExecutionProfile,
    FinancialSnapshotInput, JobStatusSnapshot, PageStatus, QueueLimits, SubmissionReceipt,
    SubmissionRequest, build_execution_input, execution_bytes, public_safe_code,
    validate_snapshot,
)

_JOB_NAMESPACE = UUID("ddbe3de2-4ca8-5d97-9b4c-3a9e5c1a3fa8")
_PAGE_NAMESPACE = UUID("0189d75a-9155-5a17-94ba-71e15b0f4888")
_ACTIVE_SQL = "'preparing_input','queued','running','cancelling','paused_restart','blocked'"
_ACTIVE = ("preparing_input", "queued", "running", "cancelling", "paused_restart", "blocked")
_UNKNOWN_PAGES = ("intent", "send_committed", "settlement_pending", "unknown_outcome")
QUEUE_LIMITS = QueueLimits()  # Deployment-wide compiled policy; never per-request.


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(value):
    try:
        return uuid_hex(value)
    except CoordinationError:
        raise CoordinationError("invalid_operation_id") from None


def _safe_database_error(error):
    code = getattr(error, "sqlite_errorcode", 0) & 0xff
    return CoordinationError("ledger_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                             else "ledger_unavailable")


class DurableJobStore:
    """Store uses a schema-v2 aware ledger and separately provisioned file store.

    Inputs named ``actor`` are already verified server authorization contexts.
    The B3.5 HTTP authentication layer is intentionally not composed here.
    """
    def __init__(self, ledger: CoordinationLedger, execution_store: PrivateExecutionStore, *, _fault_hook=None):
        if type(ledger) is not CoordinationLedger or type(execution_store) is not PrivateExecutionStore:
            raise CoordinationError("durable_backend_invalid")
        if ledger.schema_version != 2:
            raise CoordinationError("durable_jobs_require_schema_v2")
        ledger.require_capability("durable_jobs_v1")
        if ledger.installation_id != execution_store.installation_id:
            raise CoordinationError("execution_storage_binding_mismatch")
        self.ledger, self.execution_store, self._fault_hook = ledger, execution_store, _fault_hook

    def _fault(self, label):
        if self._fault_hook is not None:
            self._fault_hook(label)

    def _check_actor(self, actor, document_id=None):
        if type(actor) is not AuthorizedDocumentContext:
            raise CoordinationError("server_authorization_context_required")
        if document_id is not None and actor.document_id != document_id:
            raise CoordinationError("document_authorization_mismatch")

    def _read(self, function):
        try:
            with self.ledger._connection() as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                try:
                    result = function(connection)
                    connection.commit()
                    return result
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        except CoordinationError:
            raise
        except sqlite3.Error as exc:
            raise _safe_database_error(exc) from None

    def _write(self, function, committed_label=None):
        try:
            with self.ledger._connection() as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = function(connection)
                    connection.commit()
                finally:
                    if connection.in_transaction:
                        connection.rollback()
            # The callback is outside SQLite. A caller never observes uncommitted state.
            if committed_label:
                self._fault(committed_label)
            return result
        except CoordinationError:
            raise
        except sqlite3.IntegrityError:
            raise CoordinationError("durable_constraint_conflict") from None
        except sqlite3.Error as exc:
            raise _safe_database_error(exc) from None
        except OSError:
            raise CoordinationError("durable_receipt_uncertain") from None

    @staticmethod
    def _job_identity(installation_id, actor, request):
        seed = fingerprint([installation_id, actor.tenant_id, actor.owner_id, request.operation_id])
        job_uuid = uuid5(_JOB_NAMESPACE, seed)
        return "dj_" + job_uuid.hex, uuid5(job_uuid, "financial-snapshot").hex

    @staticmethod
    def _execution_identity(job_id, raw):
        digest_value = hashlib.sha256(raw).hexdigest()
        return fingerprint(["averon-execution-input-v1", job_id, digest_value]), digest_value, len(raw)

    @staticmethod
    def _receipt_from_row(row, expected_digest):
        try:
            payload = json.loads(row["receipt_json"])
            receipt = SubmissionReceipt(**payload)
            if (canonical(payload) != row["receipt_json"] or row["request_digest"] != expected_digest
                    or row["kind"] not in ("submit_begin", "submit_ready")
                    or receipt.operation_id != row["operation_id"]
                    or receipt.job_id != row["job_id"] or receipt.request_digest != expected_digest):
                raise ValueError
            return receipt
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise CoordinationError("durable_receipt_corrupt") from None

    def _find_submit(self, actor, request):
        def query(connection):
            row = connection.execute("SELECT * FROM durable_job_commands WHERE tenant_id=? AND owner_id=? AND operation_id=?",
                (actor.tenant_id, actor.owner_id, request.operation_id)).fetchone()
            if row is None:
                return None
            if row["kind"] not in ("submit_begin", "submit_ready") or row["request_digest"] != request.request_digest:
                raise CoordinationError("operation_conflict")
            return self._receipt_from_row(row, request.request_digest)
        return self._read(query)

    @staticmethod
    def _verify_registered_financials(connection, actor, snapshot):
        run, binding = snapshot.run, snapshot.binding
        run_row = connection.execute("SELECT r.body_json,d.active_run_id,d.generation FROM runs r"
            " JOIN documents d USING(tenant_id,document_id) WHERE r.run_id=? AND r.tenant_id=? AND r.document_id=?",
            (run.run_id, actor.tenant_id, actor.document_id)).fetchone()
        if (run_row is None or run_row["body_json"] != canonical(asdict(run))
                or run_row["active_run_id"] != run.run_id or run_row["generation"] != run.generation):
            raise CoordinationError("financial_run_not_active_in_b1")
        binding_row = connection.execute("SELECT body_json,digest,active,billing_scope_id"
            " FROM credential_bindings WHERE binding_id=? AND revision=?",
            (binding.binding_id, binding.revision)).fetchone()
        if (binding_row is None or binding_row["active"] != 1
                or binding_row["body_json"] != canonical(asdict(binding))
                or binding_row["digest"] != binding.fingerprint
                or binding_row["billing_scope_id"] != binding.billing_scope_id):
            raise CoordinationError("financial_binding_not_active_in_b1")
        actual_quotas = tuple(row[0] for row in connection.execute(
            "SELECT quota_scope_id FROM binding_quotas WHERE binding_id=? AND binding_revision=? ORDER BY quota_scope_id",
            (binding.binding_id, binding.revision)))
        if actual_quotas != binding.quota_scope_ids:
            raise CoordinationError("financial_binding_not_active_in_b1")
        for scope in actual_quotas:
            if connection.execute("SELECT 1 FROM quota_scopes WHERE scope_id=?", (scope,)).fetchone() is None:
                raise CoordinationError("financial_quota_missing_in_b1")

    @staticmethod
    def _active_jobs(connection, *, tenant_id=None, owner_id=None):
        where = "state IN (" + _ACTIVE_SQL + ")"
        params = []
        cap = QUEUE_LIMITS.active_jobs_global
        if tenant_id is not None:
            where += " AND tenant_id=?"
            params.append(tenant_id)
        if owner_id is not None:
            where += " AND owner_id=?"
            params.append(owner_id)
            cap = QUEUE_LIMITS.active_jobs_per_owner
        if tenant_id is not None:
            rows = connection.execute("SELECT job_id FROM durable_jobs INDEXED BY durable_owner_status WHERE "
                + where + " LIMIT ?", (*params, cap + 1)).fetchall()
        else:
            rows = connection.execute("SELECT job_id FROM durable_jobs INDEXED BY durable_active_document WHERE "
                + where + " LIMIT ?", (*params, cap + 1)).fetchall()
        return [row[0] for row in rows]

    @classmethod
    def _check_capacity(cls, connection, actor, new_page_count):
        global_jobs = cls._active_jobs(connection)
        if len(global_jobs) >= QUEUE_LIMITS.active_jobs_global:
            raise CoordinationError("queue_global_job_limit")
        owner_jobs = cls._active_jobs(connection, tenant_id=actor.tenant_id, owner_id=actor.owner_id)
        if len(owner_jobs) >= QUEUE_LIMITS.active_jobs_per_owner:
            raise CoordinationError("queue_owner_job_limit")
        global_pages = 0
        for job_id in global_jobs:
            global_pages += connection.execute("SELECT COUNT(*) FROM durable_job_pages WHERE job_id=?",
                                               (job_id,)).fetchone()[0]
        owner_pages = 0
        for job_id in owner_jobs:
            owner_pages += connection.execute("SELECT COUNT(*) FROM durable_job_pages WHERE job_id=?",
                                              (job_id,)).fetchone()[0]
        if global_pages + new_page_count > QUEUE_LIMITS.active_pages_global:
            raise CoordinationError("queue_global_page_limit")
        if owner_pages + new_page_count > QUEUE_LIMITS.active_pages_per_owner:
            raise CoordinationError("queue_owner_page_limit")

    def submit(self, actor: AuthorizedDocumentContext, request: SubmissionRequest, *,
               financial: FinancialSnapshotInput | None = None,
               execution_profile: ExecutionProfile | None = None) -> SubmissionReceipt:
        """Commit T0, then stage/fsync privately, then commit T1.

        Existing exact operation replay is resolved before reading current config,
        source bytes or binding state. New snapshots require a currently active
        B1 Run/Binding. No POST, B1 reserve, dispatcher lock or owner epoch occurs.
        """
        self._check_actor(actor)
        if type(request) is not SubmissionRequest:
            raise CoordinationError("durable_request_invalid")
        if actor.document_id != request.document_id:
            raise CoordinationError("document_authorization_mismatch")
        prior = self._find_submit(actor, request)
        if prior is not None:
            return prior
        if type(financial) is not FinancialSnapshotInput or type(execution_profile) is not ExecutionProfile:
            raise CoordinationError("submission_snapshot_required")
        run, config, binding = financial.run, financial.config, financial.binding
        if ((actor.tenant_id, actor.document_id, actor.generation, actor.source_digest)
                != (run.tenant_id, run.document_id, run.generation, run.source_digest)):
            raise CoordinationError("document_identity_mismatch")
        run_json, config_json, binding_json, pricing_digest, snapshot_digest = validate_snapshot(run, config, binding)
        execution = build_execution_input(actor, request, financial, execution_profile)
        raw = execution_bytes(execution)
        execution_id, execution_digest, execution_size = self._execution_identity(
            self._job_identity(self.ledger.installation_id, actor, request)[0], raw)
        # Hash/stat and file ACL checks occur before T0, outside any SQLite txn.
        self._verify_source(actor)
        job_id, snapshot_id = self._job_identity(self.ledger.installation_id, actor, request)
        run_id = run.run_id
        receipt = SubmissionReceipt(request.operation_id, job_id, run_id, snapshot_id,
                                   request.request_digest, 1)
        now = _now()

        def create(connection):
            row = connection.execute("SELECT * FROM durable_job_commands WHERE tenant_id=? AND owner_id=? AND operation_id=?",
                (actor.tenant_id, actor.owner_id, request.operation_id)).fetchone()
            if row is not None:
                if row["kind"] not in ("submit_begin", "submit_ready") or row["request_digest"] != request.request_digest:
                    raise CoordinationError("operation_conflict")
                return self._receipt_from_row(row, request.request_digest), False
            self._verify_registered_financials(connection, actor, financial)
            # These indexed counts, all uniqueness checks, snapshots, pages and
            # command receipt commit under this single BEGIN IMMEDIATE.
            existing = connection.execute("SELECT 1 FROM durable_jobs WHERE tenant_id=? AND document_id=?"
                " AND state IN (" + _ACTIVE_SQL + ") LIMIT 1", (actor.tenant_id, actor.document_id)).fetchone()
            if existing:
                raise CoordinationError("durable_document_busy")
            generation = connection.execute("SELECT 1 FROM durable_jobs WHERE tenant_id=? AND document_id=? AND generation=?",
                (actor.tenant_id, actor.document_id, actor.generation)).fetchone()
            if generation:
                raise CoordinationError("durable_generation_already_submitted")
            self._check_capacity(connection, actor, len(request.pages))
            connection.execute("INSERT INTO durable_job_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, run_id, 1, binding.binding_id, binding.revision, run_json, config_json,
                 binding_json, pricing_digest, snapshot_digest))
            connection.execute("INSERT INTO durable_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id, run_id, actor.generation,
                 snapshot_id, request.request_digest, execution_id, execution_digest, 1, execution_size,
                 "pending", "preparing_input", 1, 0, None, 0, now, now, None))
            for item in request.pages:
                candidate_id = uuid5(_PAGE_NAMESPACE, f"{run_id}:{item.page_number}:1").hex
                connection.execute("INSERT INTO durable_job_pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, run_id, item.page_number, candidate_id, None, "queued", 0, None, 1, None, 0, None))
            connection.execute("INSERT INTO durable_job_commands VALUES(?,?,?,?,?,?,?)",
                (actor.tenant_id, actor.owner_id, request.operation_id, "submit_begin", job_id,
                 request.request_digest, canonical(asdict(receipt))))
            self._fault("before_t0_commit")
            return receipt, True

        try:
            receipt, created = self._write(create, "after_t0_commit")
        except BaseException:
            # Resolve a possibly-lost commit receipt. No second T0 is attempted.
            try:
                receipt = self._find_submit(actor, request)
            except CoordinationError:
                raise
            if receipt is None:
                raise
            # Return the durable T0 receipt. Recovery is explicit and must find
            # valid pending bytes or reproduce the exact committed content hash.
            return receipt
        if not created:
            return receipt
        self._complete_staging(actor, receipt, execution, allow_create=True)
        return receipt

    def _verify_source(self, actor):
        path = actor.source_path
        try:
            for item in (path, *path.parents):
                _reject_reparse(item)
            absolute = path.absolute()
            resolved = path.resolve(strict=True)
            if absolute != resolved:
                raise CoordinationError("execution_source_changed")
            verify_private_path(path, directory=False)
            before = path.stat()
            if (not path.is_file() or before.st_size != actor.source_size_bytes
                    or before.st_mtime_ns != actor.source_mtime_ns or before.st_size > 1_073_741_824):
                raise CoordinationError("execution_source_changed")
            checksum = hashlib.sha256()
            with _open_readonly(path) as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                    raise CoordinationError("execution_source_changed")
                total = 0
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > actor.source_size_bytes:
                        raise CoordinationError("execution_source_changed")
                    checksum.update(chunk)
            after = path.stat()
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    or total != actor.source_size_bytes or checksum.hexdigest() != actor.source_digest):
                raise CoordinationError("execution_source_changed")
        except CoordinationError:
            raise
        except (OSError, PrivateAclError):
            raise CoordinationError("execution_source_unverified") from None

    def _job_artifact(self, actor, receipt):
        def query(connection):
            row = connection.execute("SELECT job_id,execution_id,execution_digest,execution_bytes,state,"
                "execution_state,revision FROM durable_jobs WHERE tenant_id=? AND owner_id=? AND document_id=? AND job_id=?",
                (actor.tenant_id, actor.owner_id, actor.document_id, receipt.job_id)).fetchone()
            if row is None:
                raise CoordinationError("durable_job_not_found")
            return dict(row)
        return self._read(query)

    def _complete_staging(self, actor, receipt, candidate, *, allow_create):
        row = self._job_artifact(actor, receipt)
        if row["state"] in ("queued", "running", "paused_restart", "cancelling"):
            return
        if row["state"] in ("cancelled", "blocked", "failed", "completed", "completed_with_errors"):
            return
        if row["state"] != "preparing_input":
            raise CoordinationError("durable_job_state_conflict")
        try:
            # T0 pins source identity and exact execution bytes. Recheck the source
            # immediately before filesystem installation so a swap between T0 and
            # fsync cannot produce an execution spec for a different document.
            self._verify_source(actor)
            staged = self.execution_store.stage(row["job_id"], row["execution_id"], row["execution_digest"],
                row["execution_bytes"], candidate, allow_create=allow_create)
            if staged.document_id != actor.document_id:
                raise CoordinationError("execution_source_mismatch")
        except CoordinationError as exc:
            if exc.code in {"execution_input_missing", "execution_input_corrupt", "execution_input_mismatch",
                            "execution_source_mismatch", "execution_source_changed",
                            "execution_source_unverified", "execution_storage_unverified"}:
                self._block_execution_input(actor, receipt.job_id, exc.code)
            raise
        self._commit_ready(actor, receipt)

    def _commit_ready(self, actor, receipt):
        ready_operation = uuid5(UUID(receipt.operation_id), "averon-b32-submit-ready-v1").hex
        now = _now()
        ready_receipt = {"operation_id": ready_operation, "job_id": receipt.job_id,
                         "kind": "submit_ready", "revision": 2, "state": "queued",
                         "request_digest": receipt.request_digest}

        def commit(connection):
            job = connection.execute("SELECT state,revision,request_digest FROM durable_jobs"
                " WHERE job_id=? AND tenant_id=? AND owner_id=? AND document_id=?",
                (receipt.job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if job is None:
                raise CoordinationError("durable_job_not_found")
            if job["request_digest"] != receipt.request_digest:
                raise CoordinationError("durable_receipt_corrupt")
            if job["state"] == "queued":
                self._verify_ready_receipt(connection, actor, receipt, ready_operation)
                return
            if job["state"] != "preparing_input":
                raise CoordinationError("durable_job_cancelled_during_staging")
            connection.execute("UPDATE durable_jobs SET execution_state='ready',state='queued',revision=revision+1,"
                "updated_at=? WHERE job_id=? AND state='preparing_input'", (now, receipt.job_id))
            connection.execute("INSERT INTO durable_job_commands VALUES(?,?,?,?,?,?,?)",
                (actor.tenant_id, actor.owner_id, ready_operation, "submit_ready", receipt.job_id,
                 fingerprint({"operation_id": receipt.operation_id, "request_digest": receipt.request_digest,
                             "execution_digest": self._execution_digest(connection, receipt.job_id)}),
                 canonical(ready_receipt)))

        try:
            self._write(commit, "after_t1_commit")
        except BaseException:
            # A committed T1 is recognized by state AND the immutable phase receipt.
            def verify(connection):
                job = connection.execute("SELECT state FROM durable_jobs WHERE job_id=? AND tenant_id=? AND owner_id=?",
                    (receipt.job_id, actor.tenant_id, actor.owner_id)).fetchone()
                if job is None or job["state"] != "queued":
                    return False
                self._verify_ready_receipt(connection, actor, receipt, ready_operation)
                return True
            if not self._read(verify):
                raise

    @staticmethod
    def _execution_digest(connection, job_id):
        row = connection.execute("SELECT execution_digest FROM durable_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise CoordinationError("durable_job_not_found")
        return row[0]

    @staticmethod
    def _verify_ready_receipt(connection, actor, receipt, operation_id):
        row = connection.execute("SELECT kind,request_digest,job_id,receipt_json FROM durable_job_commands"
            " WHERE tenant_id=? AND owner_id=? AND operation_id=?",
            (actor.tenant_id, actor.owner_id, operation_id)).fetchone()
        if row is None or row["kind"] != "submit_ready" or row["job_id"] != receipt.job_id:
            raise CoordinationError("durable_receipt_missing")
        expected_digest = fingerprint({"operation_id": receipt.operation_id,
            "request_digest": receipt.request_digest,
            "execution_digest": DurableJobStore._execution_digest(connection, receipt.job_id)})
        if row["request_digest"] != expected_digest:
            raise CoordinationError("durable_receipt_corrupt")
        try:
            saved = json.loads(row["receipt_json"])
            expected = {"operation_id": operation_id, "job_id": receipt.job_id,
                "kind": "submit_ready", "revision": 2, "state": "queued",
                "request_digest": receipt.request_digest}
            if canonical(saved) != row["receipt_json"] or saved != expected:
                raise ValueError
        except (ValueError, TypeError):
            raise CoordinationError("durable_receipt_corrupt") from None

    def _block_execution_input(self, actor, job_id, safe_code):
        safe = "execution_input_missing" if safe_code == "execution_input_missing" else "execution_input_corrupt"
        now = _now()

        def block(connection):
            job = connection.execute("SELECT state FROM durable_jobs WHERE job_id=? AND tenant_id=? AND owner_id=?",
                (job_id, actor.tenant_id, actor.owner_id)).fetchone()
            if job is None or job["state"] in ("cancelled", "completed", "completed_with_errors", "failed"):
                return
            if connection.execute("SELECT 1 FROM durable_job_pages WHERE job_id=? AND"
                    " (attempt_id IS NOT NULL OR state IN ('claimed','intent','send_committed','settlement_pending',"
                    "'completed','incomplete','failed','unknown_outcome')) LIMIT 1", (job_id,)).fetchone():
                raise CoordinationError("execution_input_requires_attempt_recovery")
            connection.execute("UPDATE durable_jobs SET state='blocked',execution_state='blocked',revision=revision+1,"
                "updated_at=? WHERE job_id=?", (now, job_id))
            connection.execute("UPDATE durable_job_pages SET state='blocked',safe_code=?,revision=revision+1"
                " WHERE job_id=? AND state IN ('queued','quota_wait') AND attempt_id IS NULL", (safe, job_id))

        self._write(block)

    def recover_preparing(self, actor: AuthorizedDocumentContext, job_id: str,
                          *, recovery_input: ExecutionInput | None = None) -> SubmissionReceipt:
        """Complete from valid final/pending bytes or exact-hash re-supply; else block.

        A supplied input can only reproduce bytes already pinned by T0's digest.
        Current settings or prompt text are never substituted on a guess.
        """
        self._check_actor(actor)
        if type(job_id) is not str or len(job_id) != 35 or not job_id.startswith("dj_"):
            raise CoordinationError("durable_job_not_found")
        row, receipt = self._preparing_record(actor, job_id)
        if row["state"] != "preparing_input":
            if row["state"] == "queued":
                self.read_execution_input(actor, job_id)
                return receipt
            raise CoordinationError("durable_job_state_conflict")
        if (actor.generation != row["generation"] or actor.source_digest != row["source_digest"]):
            self._block_execution_input(actor, job_id, "execution_source_changed")
            raise CoordinationError("execution_source_changed")
        # Historical Run/config/pricing/Binding snapshots remain the authority;
        # recovery validates them without consulting current settings or secrets.
        try:
            self.load_financial_snapshot(actor, job_id).materialize()
        except CoordinationError as exc:
            if exc.code == "financial_snapshot_corrupt":
                self._block_execution_input(actor, job_id, "execution_input_corrupt")
            raise
        if recovery_input is not None:
            if (type(recovery_input) is not ExecutionInput
                    or (recovery_input.tenant_id, recovery_input.document_id,
                        recovery_input.generation, recovery_input.source_sha256, recovery_input.run_id)
                    != (actor.tenant_id, actor.document_id, row["generation"], row["source_digest"], row["run_id"])):
                raise CoordinationError("execution_source_mismatch")
            self._verify_source(actor)
        self._complete_staging(actor, receipt, recovery_input, allow_create=recovery_input is not None)
        return receipt

    def read_execution_input(self, actor: AuthorizedDocumentContext, job_id: str) -> ExecutionInput:
        """Validate and return private execution input for trusted B3 storage callers.

        This method is deliberately not wired to an API or dispatcher in B3.2.
        Any missing, damaged, mismatched or unverified artifact blocks the job;
        it never reconstructs content from active settings.
        """
        self._check_actor(actor)
        job = self._get_private_job(actor, job_id)
        if job["state"] == "preparing_input":
            raise CoordinationError("execution_input_not_ready")
        if (actor.generation != job["generation"] or actor.source_digest != job["source_digest"]):
            raise CoordinationError("execution_source_changed")
        try:
            self._verify_source(actor)
            value = self.execution_store.read(job_id, job["execution_id"],
                job["execution_digest"], job["execution_bytes"])
            if (value.tenant_id, value.document_id, value.generation, value.run_id,
                    value.source_sha256) != (actor.tenant_id, actor.document_id,
                    job["generation"], job["run_id"], job["source_digest"]):
                raise CoordinationError("execution_input_corrupt")
            if fingerprint({"document_id": value.document_id,
                    "pages": [asdict(page) for page in value.pages],
                    "user_context": value.user_context}) != job["request_digest"]:
                raise CoordinationError("execution_input_corrupt")
            restored_run, restored_config = self.load_financial_snapshot(actor, job_id).materialize()
            if (restored_run.run_id != value.run_id or restored_run.source_digest != value.source_sha256
                    or restored_config.dpi != value.dpi
                    or restored_config.max_image_bytes != value.max_image_bytes
                    or restored_config.max_image_pixels != value.max_image_pixels):
                raise CoordinationError("execution_input_corrupt")
            return value
        except CoordinationError as exc:
            if exc.code in {"execution_input_missing", "execution_input_corrupt",
                            "execution_input_mismatch", "execution_storage_unverified",
                            "financial_snapshot_corrupt", "execution_source_changed",
                            "execution_source_unverified"}:
                self._block_execution_input(actor, job_id, exc.code)
            raise

    def _preparing_record(self, actor, job_id):
        def query(connection):
            row = connection.execute("SELECT j.*,s.run_json,s.config_json,s.binding_json,s.pricing_digest,s.snapshot_digest,"
                "json_extract(r.body_json,'$.source_digest') AS source_digest FROM durable_jobs j"
                " JOIN durable_job_snapshots s USING(snapshot_id,run_id) JOIN runs r ON r.run_id=j.run_id"
                " WHERE j.job_id=? AND j.tenant_id=? AND j.owner_id=? AND j.document_id=?",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if row is None:
                raise CoordinationError("durable_job_not_found")
            command = connection.execute("SELECT operation_id,kind,request_digest,receipt_json"
                " FROM durable_job_commands WHERE tenant_id=? AND owner_id=? AND job_id=? AND kind='submit_begin'",
                (actor.tenant_id, actor.owner_id, job_id)).fetchone()
            if command is None:
                raise CoordinationError("durable_receipt_missing")
            receipt = self._receipt_from_row({"receipt_json": command["receipt_json"],
                "request_digest": command["request_digest"], "kind": command["kind"],
                "operation_id": command["operation_id"], "job_id": job_id}, command["request_digest"])
            return dict(row), receipt
        return self._read(query)

    def _command_replay(self, connection, actor, operation_id, kind, request_digest):
        row = connection.execute("SELECT kind,request_digest,job_id,receipt_json FROM durable_job_commands"
            " WHERE tenant_id=? AND owner_id=? AND operation_id=?",
            (actor.tenant_id, actor.owner_id, operation_id)).fetchone()
        if row is None:
            return None
        if row["kind"] != kind or row["request_digest"] != request_digest:
            raise CoordinationError("operation_conflict")
        try:
            saved = json.loads(row["receipt_json"])
            receipt = CommandReceipt(**saved)
            if (canonical(saved) != row["receipt_json"] or receipt.operation_id != operation_id
                    or receipt.job_id != row["job_id"] or receipt.kind != kind
                    or receipt.request_digest != request_digest):
                raise ValueError
            return receipt
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise CoordinationError("durable_receipt_corrupt") from None

    def cancel(self, actor: AuthorizedDocumentContext, job_id: str, operation_id: str,
               expected_revision: int) -> CommandReceipt:
        self._check_actor(actor)
        _uuid(operation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise CoordinationError("invalid_expected_revision")
        payload_digest = fingerprint({"document_id": actor.document_id, "job_id": job_id,
                                      "expected_revision": expected_revision, "kind": "cancel"})
        now = _now()

        def change(connection):
            replay = self._command_replay(connection, actor, operation_id, "cancel", payload_digest)
            if replay:
                cleanup = None
                if replay.state == "cancelled":
                    row = connection.execute("SELECT execution_id,execution_digest,execution_bytes"
                        " FROM durable_jobs WHERE job_id=? AND tenant_id=? AND owner_id=?",
                        (job_id, actor.tenant_id, actor.owner_id)).fetchone()
                    if row is not None:
                        cleanup = (row["execution_id"], row["execution_digest"], row["execution_bytes"])
                return replay, cleanup
            job = connection.execute("SELECT * FROM durable_jobs WHERE job_id=? AND tenant_id=? AND owner_id=? AND document_id=?",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if job is None:
                raise CoordinationError("durable_job_not_found")
            if job["revision"] != expected_revision:
                raise CoordinationError("job_revision_conflict")
            if job["state"] in ("completed", "completed_with_errors", "cancelled", "failed"):
                raise CoordinationError("job_not_cancellable")
            pages = connection.execute("SELECT state,attempt_id FROM durable_job_pages WHERE job_id=?", (job_id,)).fetchall()
            safe_preintent = (job["state"] == "preparing_input" and all(p["attempt_id"] is None for p in pages))
            safe_blocked = (job["state"] == "blocked" and all(p["state"] not in _UNKNOWN_PAGES
                                                                  and p["attempt_id"] is None for p in pages))
            target = "cancelled" if safe_preintent or safe_blocked else "cancelling"
            changed = job["state"] != target
            revision = expected_revision + int(changed)
            cancel_revision = job["cancel_revision"] + 1
            if changed:
                connection.execute("UPDATE durable_jobs SET state=?,revision=?,cancel_revision=?,updated_at=? WHERE job_id=?",
                                   (target, revision, cancel_revision, now, job_id))
                if target == "cancelled":
                    connection.execute("UPDATE durable_job_pages SET state='cancelled',safe_code='cancelled',"
                        "revision=revision+1 WHERE job_id=? AND attempt_id IS NULL AND state IN ('queued','quota_wait','blocked')",
                        (job_id,))
            receipt = CommandReceipt(operation_id, job_id, "cancel", revision, target, payload_digest)
            connection.execute("INSERT INTO durable_job_commands VALUES(?,?,?,?,?,?,?)",
                (actor.tenant_id, actor.owner_id, operation_id, "cancel", job_id, payload_digest,
                 canonical(asdict(receipt))))
            cleanup = (job["execution_id"], job["execution_digest"], job["execution_bytes"]) if target == "cancelled" else None
            return receipt, cleanup

        receipt, cleanup = self._write(change)
        if cleanup is not None:
            self.execution_store.remove_pending(job_id, *cleanup)
        return receipt

    def resume(self, actor: AuthorizedDocumentContext, job_id: str, operation_id: str,
               expected_revision: int) -> CommandReceipt:
        self._check_actor(actor)
        _uuid(operation_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise CoordinationError("invalid_expected_revision")
        payload_digest = fingerprint({"document_id": actor.document_id, "job_id": job_id,
                                      "expected_revision": expected_revision, "kind": "resume"})
        job = self._get_private_job(actor, job_id)
        if job["revision"] != expected_revision:
            # Command replay is still checked first in its transaction below.
            existing = self._read(lambda c: self._command_replay(c, actor, operation_id, "resume", payload_digest))
            if existing:
                return existing
            raise CoordinationError("job_revision_conflict")
        if job["state"] != "paused_restart" or job["execution_state"] != "ready":
            raise CoordinationError("job_not_resumable")
        if any(page in _UNKNOWN_PAGES for page in job["page_states"]):
            raise CoordinationError("unknown_outcome_not_resumable")
        if actor.generation != job["generation"] or actor.source_digest != job["source_digest"]:
            self._block_execution_input(actor, job_id, "execution_source_mismatch")
            raise CoordinationError("execution_source_changed")
        self._verify_source(actor)
        self.execution_store.read(job_id, job["execution_id"], job["execution_digest"], job["execution_bytes"])
        now = _now()

        def change(connection):
            replay = self._command_replay(connection, actor, operation_id, "resume", payload_digest)
            if replay:
                return replay
            row = connection.execute("SELECT state,execution_state,revision,run_id FROM durable_jobs"
                " WHERE job_id=? AND tenant_id=? AND owner_id=? AND document_id=?",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if row is None:
                raise CoordinationError("durable_job_not_found")
            if row["revision"] != expected_revision:
                raise CoordinationError("job_revision_conflict")
            if row["state"] != "paused_restart" or row["execution_state"] != "ready":
                raise CoordinationError("job_not_resumable")
            if connection.execute("SELECT 1 FROM durable_job_pages WHERE job_id=? AND state IN "
                    "('intent','send_committed','settlement_pending','unknown_outcome') LIMIT 1", (job_id,)).fetchone():
                raise CoordinationError("unknown_outcome_not_resumable")
            connection.execute("UPDATE durable_jobs SET state='queued',revision=revision+1,updated_at=? WHERE job_id=?",
                               (now, job_id))
            receipt = CommandReceipt(operation_id, job_id, "resume", expected_revision + 1, "queued", payload_digest)
            connection.execute("INSERT INTO durable_job_commands VALUES(?,?,?,?,?,?,?)",
                (actor.tenant_id, actor.owner_id, operation_id, "resume", job_id, payload_digest,
                 canonical(asdict(receipt))))
            return receipt
        return self._write(change)

    def _get_private_job(self, actor, job_id):
        def query(connection):
            row = connection.execute("SELECT j.job_id,j.run_id,j.document_id,j.generation,j.request_digest,j.execution_id,"
                "j.execution_digest,j.execution_bytes,j.execution_state,j.state,j.revision,"
                "json_extract(r.body_json,'$.source_digest') AS source_digest FROM durable_jobs j"
                " JOIN runs r ON r.run_id=j.run_id"
                " WHERE j.job_id=? AND j.tenant_id=? AND j.owner_id=? AND j.document_id=?",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if row is None:
                raise CoordinationError("durable_job_not_found")
            pages = connection.execute("SELECT state FROM durable_job_pages WHERE job_id=? ORDER BY page_number LIMIT ?",
                (job_id, QUEUE_LIMITS.active_pages_global + 1)).fetchall()
            return {**dict(row), "page_states": tuple(item[0] for item in pages)}
        return self._read(query)

    def get_status(self, actor: AuthorizedDocumentContext, job_id: str, *,
                   page_after: int = 0, page_limit: int = 50,
                   expected_revision: int | None = None) -> JobStatusSnapshot:
        self._check_actor(actor)
        if (type(page_after) is not int or page_after < 0
                or type(page_limit) is not int or not 1 <= page_limit <= 100
                or (page_after and (type(expected_revision) is not int or expected_revision < 1))):
            raise CoordinationError("status_page_invalid")

        def query(connection):
            job = connection.execute("SELECT job_id,run_id,document_id,generation,state,revision,cancel_revision,"
                "created_at,updated_at,finished_at FROM durable_jobs WHERE job_id=? AND tenant_id=?"
                " AND owner_id=? AND document_id=?", (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if job is None:
                raise CoordinationError("durable_job_not_found")
            if expected_revision is not None and job["revision"] != expected_revision:
                raise CoordinationError("status_revision_changed")
            pages = connection.execute("SELECT page_number,state,revision,safe_code,result_bytes"
                " FROM durable_job_pages WHERE job_id=? AND page_number>? ORDER BY page_number LIMIT ?",
                (job_id, page_after, page_limit + 1)).fetchall()
            more = len(pages) > page_limit
            visible = pages[:page_limit]
            page_status = tuple(PageStatus(row["page_number"], row["state"], row["revision"],
                public_safe_code(row["safe_code"]), row["result_bytes"]) for row in visible)
            next_cursor = visible[-1]["page_number"] if more and visible else None
            return JobStatusSnapshot(job["job_id"], job["run_id"], job["document_id"], job["generation"],
                job["state"], job["revision"], job["cancel_revision"], job["created_at"], job["updated_at"],
                job["finished_at"], "not_published", page_status, next_cursor)
        return self._read(query)

    def load_financial_snapshot(self, actor: AuthorizedDocumentContext, job_id: str) -> HistoricalSettlementContext:
        """Return immutable Run/config preimages for B2 settlement; no file or current binding lookup."""
        self._check_actor(actor)

        def query(connection):
            row = connection.execute("SELECT s.run_json,s.config_json,s.binding_json,s.binding_id,s.binding_revision,"
                "s.pricing_digest,s.snapshot_digest,j.run_id FROM durable_jobs j"
                " JOIN durable_job_snapshots s USING(snapshot_id,run_id)"
                " WHERE j.job_id=? AND j.tenant_id=? AND j.owner_id=? AND j.document_id=?",
                (job_id, actor.tenant_id, actor.owner_id, actor.document_id)).fetchone()
            if row is None:
                raise CoordinationError("durable_job_not_found")
            try:
                run_value, config_value, binding_value = (json.loads(row[name]) for name in
                    ("run_json", "config_json", "binding_json"))
                from .coordination_contracts import Binding
                from .orchestration import OrchestrationConfig
                run = Run(**run_value)
                binding_value["quota_scope_ids"] = tuple(binding_value["quota_scope_ids"])
                binding = Binding(**binding_value)
                config = OrchestrationConfig.model_validate_json(row["config_json"])
                if (canonical(asdict(run)) != row["run_json"]
                        or canonical(asdict(binding)) != row["binding_json"]
                        or binding.binding_id != row["binding_id"]
                        or binding.revision != row["binding_revision"]
                        or canonical(config.model_dump(mode="json")) != row["config_json"]
                        or run.run_id != row["run_id"]):
                    raise ValueError
                b1_run = connection.execute("SELECT body_json FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
                if b1_run is None or b1_run[0] != row["run_json"]:
                    raise ValueError
                _run_json, _config_json, _binding_json, price_digest, snapshot_digest = validate_snapshot(run, config, binding)
                if price_digest != row["pricing_digest"] or snapshot_digest != row["snapshot_digest"]:
                    raise ValueError
                # B1 retains credential binding metadata as immutable history.
                historical_binding = connection.execute("SELECT body_json,digest FROM credential_bindings"
                    " WHERE binding_id=? AND revision=?", (binding.binding_id, binding.revision)).fetchone()
                if (historical_binding is None or historical_binding["body_json"] != row["binding_json"]
                        or historical_binding["digest"] != binding.fingerprint):
                    raise ValueError
                return HistoricalSettlementContext(row["run_json"], row["config_json"])
            except CoordinationError:
                raise
            except Exception:
                raise CoordinationError("financial_snapshot_corrupt") from None
        return self._read(query)

