"""Explicit v1 -> v2 migration. Never imported by application startup.

Only an exclusive MaintenanceSession backed by a trusted host quiescence guard
can enter. No dispatcher() call, financial restore, bootstrap or schema downgrade.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3

from .coordination import CoordinationLedger, _expected_schema_digest, _schema_digest
from .coordination_contracts import (
    Attempt, BillingScope, Binding, Bootstrap, CoordinationError, Outcome, QuotaScope, Run,
    canonical, digest, fingerprint, integer, uuid_hex,
)
from .coordination_maintenance import MaintenanceSession, _NativeLock
from .coordination_schema import APPLICATION_ID
from .coordination_schema_v2 import V2_ADDITIONS_SQL, V2_CONTRACT_SQL, V2_META_SQL

PROTECTED_TABLES = (
    "billing_scopes", "quota_scopes", "credential_bindings", "binding_quotas",
    "documents", "runs", "attempts", "attempt_quota_reservations", "operations",
)


@dataclass(frozen=True)
class MigrationRequest:
    migration_id: str
    installation_id: str
    source_schema_digest: str
    target_schema_digest: str

    def __post_init__(self):
        uuid_hex(self.migration_id)
        uuid_hex(self.installation_id)
        digest(self.source_schema_digest)
        digest(self.target_schema_digest)

    @classmethod
    def v1_to_v2(cls, migration_id, installation_id):
        return cls(migration_id, installation_id, _expected_schema_digest(1), _expected_schema_digest(2))


@dataclass(frozen=True)
class MigrationReceipt:
    version: int
    migration_id: str
    installation_id: str
    source_schema_digest: str
    target_schema_digest: str
    financial_digest: str
    metadata_digest: str
    immutable_schema_digest: str
    quiescence_digest: str

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise CoordinationError("migration_receipt_invalid")
        uuid_hex(self.migration_id)
        uuid_hex(self.installation_id)
        for name, value in asdict(self).items():
            if name.endswith("digest"):
                digest(value)


@dataclass(frozen=True)
class MigrationResult:
    receipt: MigrationReceipt
    newly_applied: bool
    commit_receipt_recovered: bool = False


def protected_history(connection):
    """Streaming typed/canonical digests; never log/store original private rows."""
    tables = []
    for table in PROTECTED_TABLES:
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        names = [row[1] for row in columns]
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not keys:
            raise CoordinationError("protected_history_invalid")
        hasher, count = hashlib.sha256(), 0
        hasher.update(canonical([table, names]).encode())
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {','.join(keys)}"):
            typed = []
            for value in row:
                if value is not None and type(value) not in (str, int):
                    raise CoordinationError("protected_history_invalid")
                typed.append([type(value).__name__, value])
            hasher.update(b"\n" + canonical(typed).encode())
            count += 1
        tables.append([table, count, hasher.hexdigest()])
    connection.row_factory = None
    meta_columns = [row[1] for row in connection.execute("PRAGMA table_info(meta)")]
    rows = connection.execute("SELECT * FROM meta").fetchall()
    if len(rows) != 1:
        raise CoordinationError("protected_history_invalid")
    metadata = {name: value for name, value in zip(meta_columns, rows[0]) if name != "schema_version"}
    # All original financial tables/indexes/triggers must remain byte-identical.
    financial_schema = []
    for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema"
                                  " WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"):
        if row[2] in PROTECTED_TABLES:
            financial_schema.append(tuple(row))
    return (fingerprint(tables), fingerprint(metadata), fingerprint(financial_schema))


def _integrity(connection):
    if (connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchall()):
        raise CoordinationError("ledger_corrupt")


def _json_history(value):
    try:
        parsed = json.loads(value)
        if canonical(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError, RecursionError):
        raise CoordinationError("protected_history_invalid") from None


def _binding(value):
    return Binding(**{**value, "quota_scope_ids": tuple(value["quota_scope_ids"])})


def _attempt(value):
    return Attempt(**{**value, "binding": _binding(value["binding"]),
                      "quota_revisions": tuple(tuple(pair) for pair in value["quota_revisions"])})


def validate_financial_history(connection):
    """Read-only semantic checks of v1 receipts/identities, beyond page integrity.

    This does not recalculate/release liabilities or reinterpret historical
    pricing. It refuses malformed/canonical-hash-inconsistent original facts.
    """
    def rows(table):
        cursor = connection.execute(f"SELECT * FROM {table}")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]
    try:
        meta = rows("meta")[0]
        uuid_hex(meta["installation_id"])
        uuid_hex(meta["bootstrap_authorization"])
        uuid_hex(meta["boot_id"])
        digest(meta["history_manifest_digest"])
        if meta["owner_nonce"] is not None:
            uuid_hex(meta["owner_nonce"])
        operations = {}
        for row in rows("operations"):
            uuid_hex(row["operation_id"])
            uuid_hex(row["boot_id"])
            payload, result = _json_history(row["request_json"]), _json_history(row["result_json"])
            if fingerprint(payload) != row["request_digest"] or row["epoch"] > meta["epoch"]:
                raise ValueError
            operations[row["operation_id"]] = (row, payload, result)
        bootstrap_row, value, result = operations[meta["bootstrap_authorization"]]
        spec = Bootstrap(**{**value,
            "billings": tuple(BillingScope(**v) for v in value["billings"]),
            "quotas": tuple(QuotaScope(**v) for v in value["quotas"]),
            "bindings": tuple(_binding(v) for v in value["bindings"])})
        if (bootstrap_row["kind"] != "bootstrap" or bootstrap_row["epoch"] != 0
                or spec.installation_id != meta["installation_id"]
                or spec.authorization_id != meta["bootstrap_authorization"]
                or spec.history_manifest_digest != meta["history_manifest_digest"]
                or spec.global_concurrency != meta["global_concurrency"]
                or result != {"installation_id": spec.installation_id}):
            raise ValueError
        billings = {row["scope_id"]: row for row in rows("billing_scopes")}
        quotas = {row["scope_id"]: row for row in rows("quota_scopes")}
        if set(billings) != {v.scope_id for v in spec.billings} or set(quotas) != {v.scope_id for v in spec.quotas}:
            raise ValueError
        for original in spec.billings:
            row = billings[original.scope_id]
            for key in ("opening_spent_units", "opening_held_units", "history_complete", "evidence_digest"):
                if row[key] != getattr(original, key):
                    raise ValueError
        for original in spec.quotas:
            row = quotas[original.scope_id]
            for key in ("opening_requests", "opening_tokens", "opening_inflight", "history_complete", "evidence_digest"):
                if row[key] != getattr(original, key):
                    raise ValueError
            if (row["opening_boot_id"] != bootstrap_row["boot_id"]
                    or row["opening_ended_ms"] != bootstrap_row["created_ms"]
                    or row["opening_window_ms"] != original.window_ms):
                raise ValueError
        bindings = {}
        edges = {}
        for edge in rows("binding_quotas"):
            edges.setdefault((edge["binding_id"], edge["binding_revision"]), []).append(edge["quota_scope_id"])
        for row in rows("credential_bindings"):
            binding = _binding(_json_history(row["body_json"]))
            if ((binding.binding_id, binding.revision, binding.billing_scope_id, binding.fingerprint)
                    != (row["binding_id"], row["revision"], row["billing_scope_id"], row["digest"])):
                raise ValueError
            actual_edges = tuple(sorted(edges.get((binding.binding_id, binding.revision), ())))
            if actual_edges != binding.quota_scope_ids:
                raise ValueError
            bindings[(binding.binding_id, binding.revision)] = binding
        for original in spec.bindings:
            if bindings[(original.binding_id, original.revision)] != original:
                raise ValueError
        runs = {}
        for row in rows("runs"):
            run = Run(**_json_history(row["body_json"]))
            if any(row[key] != getattr(run, key) for key in ("run_id", "tenant_id", "document_id", "generation", "budget_units", "request_cap")):
                raise ValueError
            runs[run.run_id] = run
        for row in rows("documents"):
            run = runs[row["active_run_id"]]
            if (run.tenant_id, run.document_id, run.generation) != (row["tenant_id"], row["document_id"], row["generation"]):
                raise ValueError
        attempts = {}
        reservations = {}
        for saved in rows("attempt_quota_reservations"):
            reservations.setdefault(saved["attempt_id"], []).append(saved)
        for row in rows("attempts"):
            item = _attempt(_json_history(row["body_json"]))
            for key in ("attempt_id", "operation_id", "run_id", "page_number", "attempt_no", "claim_seq",
                        "billing_revision", "request_digest", "input_tokens", "output_tokens", "reserve_units"):
                if row[key] != getattr(item, key):
                    raise ValueError
            if (row["owner_epoch"] > meta["epoch"] or (row["binding_id"], row["binding_revision"], row["billing_scope_id"])
                    != (item.binding.binding_id, item.binding.revision, item.binding.billing_scope_id)
                    or bindings[(row["binding_id"], row["binding_revision"])] != item.binding):
                raise ValueError
            op, payload, result = operations[item.operation_id]
            if (op["kind"] != "reserve" or canonical(payload) != canonical(asdict(item))
                    or op["epoch"] != row["owner_epoch"]
                    or result != {"attempt_id": item.attempt_id, "owner_epoch": row["owner_epoch"]}):
                raise ValueError
            actual = sorted(reservations.get(item.attempt_id, ()), key=lambda v: v["quota_scope_id"])
            if len(actual) != len(item.quota_revisions):
                raise ValueError
            for saved, (scope, revision) in zip(actual, item.quota_revisions):
                policy = _json_history(saved["policy_json"])
                if (saved["quota_scope_id"] != scope or saved["policy_revision"] != revision
                        or policy["scope_id"] != scope or policy["revision"] != revision
                        or saved["window_ms"] != policy["window_ms"] or policy["blocked"] != 0
                        or saved["reserved_tokens"] != item.input_tokens + item.output_tokens
                        or quotas[scope]["revision"] < revision):
                    raise ValueError
            attempts[item.attempt_id] = (item, row)
        terminal_matches, run_receipts = set(), set()
        for op, payload, result in operations.values():
            kind = op["kind"]
            if kind == "bootstrap":
                if op["operation_id"] != spec.authorization_id:
                    raise ValueError
            elif kind == "run":
                run = Run(**payload)
                if runs[run.run_id] != run or result != {"run_id": run.run_id}:
                    raise ValueError
                run_receipts.add(run.run_id)
            elif kind == "binding":
                binding = _binding(payload)
                if bindings[(binding.binding_id, binding.revision)] != binding or result != {"binding_digest": binding.fingerprint}:
                    raise ValueError
            elif kind == "reserve":
                item = _attempt(payload)
                if attempts[item.attempt_id][0] != item or op["operation_id"] != item.operation_id:
                    raise ValueError
            elif kind == "outcome":
                outcome = Outcome(**payload)
                item, row = attempts[outcome.attempt_id]
                if (outcome.operation_id != op["operation_id"] or outcome.request_digest != item.request_digest
                        or result != {"attempt_id": outcome.attempt_id, "state": outcome.state}):
                    raise ValueError
                if outcome.state in ("terminal", "not_sent"):
                    if (row["state"], row["actual_units"], row["evidence_digest"]) != (
                            outcome.state, outcome.actual_units, outcome.evidence_digest):
                        raise ValueError
                    terminal_matches.add(outcome.attempt_id)
            elif kind in ("billing_policy", "quota_policy"):
                if not isinstance(payload, list) or len(payload) != (3 if kind == "billing_policy" else 6):
                    raise ValueError
                for value in payload[1:]:
                    integer(value, minimum=1 if kind == "quota_policy" else 0)
                integer(payload[1], minimum=1)
                if result != {"revision": payload[1] + 1}:
                    raise ValueError
            else:
                raise ValueError
        if set(runs) != run_receipts or any(row["state"] in ("terminal", "not_sent") and identity not in terminal_matches
                for identity, (_item, row) in attempts.items()):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, CoordinationError):
        raise CoordinationError("protected_history_invalid") from None


def _statements(script):
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ""
    if pending.strip():
        raise CoordinationError("migration_definition_invalid")


def _open_registered(session):
    for version in (1, 2):
        try:
            return CoordinationLedger(session.layout.ledger_root,
                installation_id=session.layout.installation_id, schema_version=version,
                _maintenance=session.borrow())
        except CoordinationError as exc:
            if exc.code != "schema_mismatch":
                raise
    raise CoordinationError("migration_schema_unrecognized")


def _receipt(connection, request):
    row = connection.execute("SELECT request_digest,receipt_json,receipt_digest FROM schema_migrations"
                             " WHERE migration_id=?", (request.migration_id,)).fetchone()
    if row is None:
        raise CoordinationError("migration_already_applied")
    if row[0] != fingerprint(asdict(request)):
        raise CoordinationError("migration_id_conflict")
    try:
        payload = json.loads(row[1])
        receipt = MigrationReceipt(**payload)
        if (canonical(payload) != row[1] or fingerprint(payload) != row[2]
                or receipt.migration_id != request.migration_id
                or receipt.installation_id != request.installation_id
                or receipt.source_schema_digest != request.source_schema_digest
                or receipt.target_schema_digest != request.target_schema_digest):
            raise ValueError
        return receipt
    except (ValueError, TypeError, KeyError):
        raise CoordinationError("migration_receipt_invalid") from None


def _apply(connection, request, receipt):
    # Avoid executescript(): its implicit commit would break atomicity.
    connection.execute("CREATE TEMP TABLE migration_meta AS SELECT * FROM meta")
    connection.execute("DROP TABLE meta")
    connection.execute(V2_META_SQL)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(meta)")]
    expressions = ["2" if name == "schema_version" else name for name in columns]
    connection.execute(f"INSERT INTO meta SELECT {','.join(expressions)} FROM migration_meta")
    connection.execute("DROP TABLE migration_meta")
    connection.execute("DROP VIEW schema_contract")
    connection.execute(V2_CONTRACT_SQL)
    for statement in _statements(V2_ADDITIONS_SQL):
        connection.execute(statement)
    connection.execute("INSERT INTO durable_scheduler_state VALUES(1,0)")
    payload = asdict(receipt)
    connection.execute("INSERT INTO schema_migrations VALUES(?,?,?,?)", (
        request.migration_id, fingerprint(asdict(request)), canonical(payload), fingerprint(payload),
    ))
    connection.execute("PRAGMA user_version=2")


def migrate_v1_to_v2(session, request):
    """Apply once, or resolve a lost commit receipt while still exclusive.

    Repeating a committed ID returns its original receipt even when legitimate
    v2 financial operations have since added history. It never restores data.
    """
    if type(session) is not MaintenanceSession or type(request) is not MigrationRequest:
        raise CoordinationError("maintenance_session_required")
    session.check()
    if (request.installation_id != session.layout.installation_id
            or request.source_schema_digest != _expected_schema_digest(1)
            or request.target_schema_digest != _expected_schema_digest(2)):
        raise CoordinationError("migration_request_mismatch")
    raw_lock = _NativeLock(session.layout.ledger_root / "dispatcher.lock", exclusive=True,
                           busy_code="dispatcher_busy")
    ledger, committing, before = None, False, None
    try:
        ledger = _open_registered(session)
        if ledger.schema_version == 2:
            with ledger._connection() as connection:
                connection.execute("BEGIN EXCLUSIVE")
                try:
                    session.check()
                    validate_financial_history(connection)
                    result = _receipt(connection, request)
                    connection.commit()
                    return MigrationResult(result, False)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        with ledger._connection() as connection:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                session.check()
                _integrity(connection)
                validate_financial_history(connection)
                if (_schema_digest(connection) != request.source_schema_digest
                        or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
                    raise CoordinationError("migration_schema_unrecognized")
                before = protected_history(connection)
                receipt = MigrationReceipt(1, request.migration_id, request.installation_id,
                    request.source_schema_digest, request.target_schema_digest, *before, session.evidence_digest)
                _apply(connection, request, receipt)
                _integrity(connection)
                if (protected_history(connection) != before
                        or _schema_digest(connection) != request.target_schema_digest):
                    raise CoordinationError("migration_history_mismatch")
                session.check()
                committing = True
                connection.commit()
                session.check()
                if _receipt(connection, request) != receipt or protected_history(connection) != before:
                    raise CoordinationError("migration_history_mismatch")
                return MigrationResult(receipt, True)
            finally:
                if connection.in_transaction:
                    connection.rollback()
    except (CoordinationError, sqlite3.Error, OSError):
        if not committing:
            raise
        if ledger is not None:
            ledger.close()
            ledger = None
        # Never retry DDL here. Reopen under the SAME guards and identify the
        # exact committed target/receipt or unchanged source transaction.
        session.check()
        ledger = _open_registered(session)
        with ledger._connection() as connection:
            if ledger.schema_version == 1:
                if protected_history(connection) == before:
                    raise CoordinationError("migration_not_committed") from None
                raise CoordinationError("migration_outcome_unverified") from None
            result = _receipt(connection, request)
            if protected_history(connection) != before:
                raise CoordinationError("migration_outcome_unverified") from None
            return MigrationResult(result, True, True)
    finally:
        try:
            if ledger is not None:
                ledger.close()
        finally:
            raw_lock.close()
