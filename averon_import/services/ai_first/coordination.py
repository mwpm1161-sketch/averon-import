"""Inert SQLite B1 ledger. Explicit bootstrap only; never composes a live client.

All methods are synchronous: future async composition must call them off-loop
and drain an outstanding commit before handling cancellation. There is no queue,
HTTP permission, result-file writer, reconciliation API or checkpoint migration.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from .coordination_contracts import (
    Attempt, BillingScope, Binding, Bootstrap, ClockStamp, CoordinationError,
    Outcome, Reservation, Run, canonical, fingerprint,
    identifier, integer, uuid_hex,
)
from .coordination_schema import (
    APPLICATION_ID, RECONCILIATION_MIN_SCHEMA_VERSION, SCHEMA_SQL, SCHEMA_VERSION,
)
from .private_acl import (
    PrivateAclError, create_private_directory, secure_private_path,
    verify_private_path, verify_private_tree,
)


def system_clock() -> ClockStamp:
    """A shared host boot identity and suspend-aware uptime, never UTC expiry."""
    try:
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetTickCount64.restype = ctypes.c_ulonglong
            ntdll = ctypes.WinDLL("ntdll")
            buffer = ctypes.create_string_buffer(64)
            length = ctypes.c_ulong()
            status = ntdll.NtQuerySystemInformation(90, buffer, len(buffer), ctypes.byref(length))
            if status < 0 or length.value < 16:
                raise ValueError
            boot = UUID(bytes_le=buffer.raw[:16]).hex
            elapsed = int(kernel.GetTickCount64())
        elif os.name == "posix" and hasattr(time, "CLOCK_BOOTTIME"):
            boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()).hex
            elapsed = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000
        else:
            raise ValueError
        return ClockStamp(boot, elapsed)
    except Exception:
        raise CoordinationError("clock_unverified") from None


def _file_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def _schema_digest(connection):
    return fingerprint([tuple(row) for row in connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )])


def _expected_schema_digest():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_SQL)
        return _schema_digest(connection)
    finally:
        connection.close()


def _safe_root(root):
    root = Path(os.path.abspath(root))
    for path in (root, *root.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise CoordinationError("storage_unverified")
    if root.is_relative_to(Path(__file__).resolve().parents[3]):
        raise CoordinationError("ledger_must_be_outside_repository")
    return root


class DispatcherLease:
    """Unserializable process-bound lifetime OS lock; TTL never enables stealing."""
    def __init__(self, ledger, stream, epoch, nonce):
        self._ledger, self._stream = ledger, stream
        self.epoch, self._nonce = epoch, nonce
        self._pid = os.getpid()
        self._guard = threading.RLock()
        self._active = True

    def __reduce__(self):
        raise TypeError("dispatcher_lease_is_process_bound")

    def _check(self, ledger):
        if (not self._active or self._pid != os.getpid() or self._ledger is not ledger
                or self._stream.closed
                or _file_identity(ledger.lock_path) != ledger._lock_identity):
            raise CoordinationError("owner_fenced")

    def renew(self):
        with self._ledger._transaction(self) as (connection, stamp, _meta):
            connection.execute("UPDATE meta SET lease_until_ms=? WHERE id=1",
                               (integer(stamp.milliseconds + self._ledger.lease_ms),))

    def close(self):
        with self._guard:
            if not self._active:
                return
            self._active = False
            try:
                if self._pid == os.getpid():
                    if os.name == "nt":
                        import msvcrt
                        self._stream.seek(0)
                        msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class CoordinationLedger:
    def __init__(self, root, *, installation_id: str, clock=system_clock,
                 busy_timeout_ms: int = 250, lease_ms: int = 30_000):
        self.root = _safe_root(root)
        self.path, self.lock_path = self.root / "coordination.sqlite3", self.root / "dispatcher.lock"
        self.installation_id = uuid_hex(installation_id)
        self.clock = clock
        self.busy_timeout_ms = integer(busy_timeout_ms)
        self.lease_ms = integer(lease_ms, minimum=1)
        self._schema_digest = _expected_schema_digest()
        self._db_identity = self._lock_identity = None
        try:
            self._verify_storage()
            self._db_identity, self._lock_identity = _file_identity(self.path), _file_identity(self.lock_path)
            with self._connection() as connection:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise CoordinationError("ledger_corrupt")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise CoordinationError("ledger_corrupt")
                if _schema_digest(connection) != self._schema_digest:
                    raise CoordinationError("schema_mismatch")
        except (OSError, PrivateAclError, sqlite3.Error):
            raise CoordinationError("ledger_unavailable") from None

    @classmethod
    def bootstrap(cls, root, spec: Bootstrap, *, clock=system_clock, **options):
        """Explicit attested initialization into a NEW directory; never overwrite."""
        if type(spec) is not Bootstrap:
            raise CoordinationError("invalid_bootstrap")
        root = _safe_root(root)
        if root.exists():
            raise CoordinationError("bootstrap_target_exists")
        stamp = clock()
        try:
            create_private_directory(root)
            for name in ("coordination.sqlite3", "dispatcher.lock"):
                with (root / name).open("xb") as stream:
                    if name.endswith(".lock"):
                        stream.write(b"\0")
                    stream.flush()
                    os.fsync(stream.fileno())
                secure_private_path(root / name, directory=False)
            connection = sqlite3.connect((root / "coordination.sqlite3").as_uri() + "?mode=rw", uri=True)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=EXTRA")
                connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("INSERT INTO meta VALUES(1,?,?,?,?,0,NULL,?,?,?,0,?)", (
                    spec.installation_id, spec.authorization_id, spec.history_manifest_digest,
                    SCHEMA_VERSION, stamp.boot_id, stamp.milliseconds, stamp.milliseconds, spec.global_concurrency,
                ))
                for item in spec.billings:
                    blocked = (not item.history_complete or item.opening_held_units > 0
                               or item.opening_spent_units > item.ceiling_units)
                    connection.execute("INSERT INTO billing_scopes VALUES(?,?,?,?,?,?,?,?)", (
                        item.scope_id, item.revision, item.ceiling_units, item.opening_spent_units,
                        item.opening_held_units, int(item.history_complete), item.evidence_digest, int(blocked),
                    ))
                for item in spec.quotas:
                    connection.execute("INSERT INTO quota_scopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        item.scope_id, item.revision, item.requests, item.tokens, item.concurrency,
                        item.window_ms, item.opening_requests, item.opening_tokens, item.opening_inflight,
                        stamp.boot_id, stamp.milliseconds, item.window_ms, int(item.history_complete), item.evidence_digest,
                        int(not item.history_complete or item.opening_inflight > 0),
                    ))
                for item in spec.bindings:
                    if item.revision != 1:
                        raise CoordinationError("invalid_initial_revision")
                    cls._insert_binding(connection, item)
                connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,0,?,?)", (
                    spec.authorization_id, "bootstrap", fingerprint(asdict(spec)), canonical(asdict(spec)),
                    canonical({"installation_id": spec.installation_id}), stamp.boot_id, stamp.milliseconds,
                ))
                connection.commit()
            finally:
                connection.close()
            if os.name != "nt":
                fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            verify_private_tree(root)
        except (OSError, PrivateAclError, sqlite3.Error):
            # A failed bootstrap is deliberately left unusable, never re-created.
            raise CoordinationError("bootstrap_failed") from None
        return cls(root, installation_id=spec.installation_id, clock=clock, **options)

    def _verify_storage(self):
        verify_private_path(self.root, directory=True)
        for path, expected in ((self.path, self._db_identity), (self.lock_path, self._lock_identity)):
            verify_private_path(path, directory=False)
            if expected is not None and _file_identity(path) != expected:
                raise CoordinationError("storage_replaced")
        # A hot rollback journal may disappear while a reader checks its ACL.
        # Tolerate only that disappearance, never a permissive existing file.
        journal = self.path.with_name(self.path.name + "-journal")
        for path in self.root.iterdir():
            if path not in (self.path, self.lock_path, journal):
                raise CoordinationError("storage_unverified")
            if path == journal:
                try:
                    verify_private_path(path, directory=False)
                except PrivateAclError:
                    if path.exists() or path.is_symlink():
                        raise

    @contextmanager
    def _connection(self):
        connection = None
        try:
            self._verify_storage()
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                         timeout=self.busy_timeout_ms / 1000, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=EXTRA")
            if (connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA synchronous").fetchone()[0] != 3):
                raise CoordinationError("durability_unverified")
            if (connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
                    or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
                raise CoordinationError("schema_mismatch")
            meta = connection.execute("SELECT installation_id,schema_version FROM meta WHERE id=1").fetchone()
            if meta != (self.installation_id, SCHEMA_VERSION):
                raise CoordinationError("installation_mismatch")
            if _schema_digest(connection) != self._schema_digest:
                raise CoordinationError("schema_mismatch")
            yield connection
        except (OSError, PrivateAclError):
            raise CoordinationError("storage_unverified") from None
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            raise CoordinationError("ledger_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                    else "ledger_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self, owner=None, *, acquiring=False):
        guard = owner._guard if type(owner) is DispatcherLease else threading.RLock()
        with guard:
            if not acquiring and type(owner) is not DispatcherLease:
                raise CoordinationError("owner_required")
            with self._connection() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.row_factory = sqlite3.Row
                    meta = connection.execute("SELECT * FROM meta WHERE id=1").fetchone()
                    stamp = self.clock()
                    if type(stamp) is not ClockStamp:
                        raise CoordinationError("clock_unverified")
                    if stamp.boot_id == meta["boot_id"] and stamp.milliseconds < meta["high_water_ms"]:
                        raise CoordinationError("clock_regressed")
                    if not acquiring:
                        owner._check(self)
                        if (meta["epoch"] != owner.epoch or meta["owner_nonce"] != owner._nonce
                                or stamp.boot_id != meta["boot_id"]
                                or stamp.milliseconds >= meta["lease_until_ms"]):
                            raise CoordinationError("owner_fenced")
                        connection.execute("UPDATE meta SET high_water_ms=? WHERE id=1", (stamp.milliseconds,))
                    yield connection, stamp, meta
                    if not acquiring:
                        owner._check(self)
                        end = self.clock()
                        if (end.boot_id != stamp.boot_id or end.milliseconds < stamp.milliseconds
                                or end.milliseconds >= connection.execute(
                                    "SELECT lease_until_ms FROM meta WHERE id=1").fetchone()[0]):
                            raise CoordinationError("owner_fenced")
                    self._verify_storage()
                    connection.commit()
                    # A slow fsync must not return a usable new receipt after
                    # lease expiry. The committed operation remains replayable
                    # as history, never as a second newly-created reservation.
                    completed = self.clock()
                    until = connection.execute("SELECT lease_until_ms FROM meta WHERE id=1").fetchone()[0]
                    if (completed.boot_id != stamp.boot_id or completed.milliseconds < stamp.milliseconds
                            or completed.milliseconds >= until):
                        raise CoordinationError("owner_fenced")
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

    def dispatcher(self) -> DispatcherLease:
        stream = None
        transferred = False
        try:
            self._verify_storage()
            stream = self.lock_path.open("r+b")
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CoordinationError("dispatcher_busy") from None
            with self._transaction(acquiring=True) as (connection, stamp, meta):
                epoch, nonce = integer(meta["epoch"] + 1), uuid4().hex
                # Every prior un-settled intent is ambiguous, regardless of TTL.
                intents = connection.execute("SELECT attempt_id FROM attempts WHERE state='intent'").fetchall()
                for row in intents:
                    self._block_attempt(connection, row[0])
                connection.execute("UPDATE attempts SET state='unknown' WHERE state='intent'")
                boot_started = stamp.milliseconds if stamp.boot_id != meta["boot_id"] else meta["boot_started_ms"]
                connection.execute("UPDATE meta SET epoch=?,owner_nonce=?,boot_id=?,boot_started_ms=?,"
                                   "high_water_ms=?,lease_until_ms=? WHERE id=1", (
                    epoch, nonce, stamp.boot_id, boot_started, stamp.milliseconds,
                    integer(stamp.milliseconds + self.lease_ms),
                ))
            lease = DispatcherLease(self, stream, epoch, nonce)
            transferred = True
            return lease
        except (OSError, PrivateAclError):
            raise CoordinationError("storage_unverified") from None
        finally:
            # Ownership transfers to the returned lease only after durable commit.
            if stream is not None and not transferred:
                stream.close()

    @staticmethod
    def _replay(connection, operation_id, kind, payload):
        uuid_hex(operation_id)
        row = connection.execute("SELECT kind,request_digest,result_json FROM operations WHERE operation_id=?",
                                 (operation_id,)).fetchone()
        if row is None:
            return None
        if row[0] != kind or row[1] != fingerprint(payload):
            raise CoordinationError("operation_conflict")
        import json
        return json.loads(row[2])

    @staticmethod
    def _receipt(connection, owner, stamp, operation_id, kind, payload, result):
        connection.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (
            operation_id, kind, fingerprint(payload), canonical(payload), canonical(result), owner.epoch,
            stamp.boot_id, stamp.milliseconds,
        ))
        return result

    @staticmethod
    def _insert_binding(connection, item):
        if connection.execute("SELECT 1 FROM billing_scopes WHERE scope_id=?", (item.billing_scope_id,)).fetchone() is None:
            raise CoordinationError("unknown_billing_scope")
        connection.execute("INSERT INTO credential_bindings VALUES(?,?,?,?,?,1)", (
            item.binding_id, item.revision, item.billing_scope_id, canonical(asdict(item)), item.fingerprint,
        ))
        for scope in item.quota_scope_ids:
            if connection.execute("SELECT 1 FROM quota_scopes WHERE scope_id=?", (scope,)).fetchone() is None:
                raise CoordinationError("unknown_quota_scope")
            connection.execute("INSERT INTO binding_quotas VALUES(?,?,?)", (item.binding_id, item.revision, scope))

    def put_binding(self, owner, operation_id: str, item: Binding):
        if type(item) is not Binding:
            raise CoordinationError("invalid_binding")
        payload = asdict(item)
        with self._transaction(owner) as (connection, stamp, _):
            replay = self._replay(connection, operation_id, "binding", payload)
            if replay is not None:
                return replay
            current = connection.execute("SELECT MAX(revision) FROM credential_bindings WHERE binding_id=?",
                                         (item.binding_id,)).fetchone()[0] or 0
            if item.revision != current + 1:
                raise CoordinationError("revision_conflict")
            connection.execute("UPDATE credential_bindings SET active=0 WHERE binding_id=?", (item.binding_id,))
            self._insert_binding(connection, item)
            return self._receipt(connection, owner, stamp, operation_id, "binding", payload,
                                 {"binding_digest": item.fingerprint})

    def register_run(self, owner, operation_id: str, run: Run):
        if type(run) is not Run:
            raise CoordinationError("invalid_run")
        payload = asdict(run)
        with self._transaction(owner) as (connection, stamp, _):
            replay = self._replay(connection, operation_id, "run", payload)
            if replay is not None:
                return replay
            old = connection.execute("SELECT generation FROM documents WHERE tenant_id=? AND document_id=?",
                                     (run.tenant_id, run.document_id)).fetchone()
            if old is not None and run.generation <= old[0]:
                raise CoordinationError("generation_conflict")
            connection.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(tenant_id,document_id)"
                               " DO UPDATE SET generation=excluded.generation,active_run_id=excluded.active_run_id",
                               (run.tenant_id, run.document_id, run.generation, run.run_id))
            connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (
                run.run_id, run.tenant_id, run.document_id, run.generation,
                run.budget_units, run.request_cap, canonical(payload),
            ))
            return self._receipt(connection, owner, stamp, operation_id, "run", payload, {"run_id": run.run_id})

    @staticmethod
    def _liability(connection, column, identity):
        # Python integers avoid SQLite SUM's signed-64-bit aggregate overflow.
        # Each persisted amount is strict int64; an overrun remains visible.
        rows = connection.execute(f"SELECT reserve_units,actual_units FROM attempts WHERE {column}=? AND state!='not_sent'",
                                  (identity,))
        spent = held = 0
        for reserved, actual in rows:
            if actual is None:
                held += reserved
            else:
                spent += actual
        return spent, held

    @staticmethod
    def _quota_usage(connection, scope, stamp, meta):
        requests = tokens = inflight = 0
        ended = scope["opening_ended_ms"] if scope["opening_boot_id"] == stamp.boot_id else meta["boot_started_ms"]
        if stamp.milliseconds < ended + max(scope["window_ms"], scope["opening_window_ms"]):
            requests, tokens = scope["opening_requests"], scope["opening_tokens"]
        inflight = scope["opening_inflight"]
        rows = connection.execute("SELECT a.state,a.ended_boot_id,a.ended_ms,q.window_ms,q.reserved_tokens"
                                  " FROM attempt_quota_reservations q INDEXED BY quota_attempts"
                                  " JOIN attempts a INDEXED BY attempt_quota_state USING(attempt_id)"
                                  " WHERE q.quota_scope_id=?", (scope["scope_id"],)).fetchall()
        for row in rows:
            if row[0] == "not_sent":
                continue
            if row[0] in ("intent", "unknown"):
                inflight += 1
                requests += 1
                tokens += row[4]
            else:
                ended = row[2] if row[1] == stamp.boot_id else meta["boot_started_ms"]
                if stamp.milliseconds < ended + max(row[3], scope["window_ms"]):
                    requests += 1
                    tokens += row[4]
        return requests, tokens, inflight

    def reserve(self, owner, attempt: Attempt) -> Reservation:
        if type(attempt) is not Attempt:
            raise CoordinationError("invalid_attempt")
        payload = asdict(attempt)
        with self._transaction(owner) as (connection, stamp, meta):
            replay = self._replay(connection, attempt.operation_id, "reserve", payload)
            if replay is not None:
                return Reservation(replay["attempt_id"], replay["owner_epoch"], False)
            run = connection.execute("SELECT r.* FROM runs r JOIN documents d USING(tenant_id,document_id)"
                                     " WHERE r.run_id=? AND d.active_run_id=r.run_id AND d.generation=r.generation",
                                     (attempt.run_id,)).fetchone()
            if run is None:
                raise CoordinationError("run_fenced")
            if connection.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('intent','unknown')").fetchone()[0] >= meta["global_concurrency"]:
                raise CoordinationError("global_capacity_exhausted")
            binding = connection.execute("SELECT digest FROM credential_bindings WHERE binding_id=? AND revision=? AND active=1",
                                         (attempt.binding.binding_id, attempt.binding.revision)).fetchone()
            if binding is None or binding[0] != attempt.binding.fingerprint:
                raise CoordinationError("binding_mismatch")
            billing = connection.execute("SELECT * FROM billing_scopes WHERE scope_id=?",
                                         (attempt.binding.billing_scope_id,)).fetchone()
            if billing["revision"] != attempt.billing_revision:
                raise CoordinationError("policy_revision_mismatch")
            if billing["blocked"]:
                raise CoordinationError("needs_reconciliation")
            spent, held = self._liability(connection, "billing_scope_id", billing["scope_id"])
            liability = spent + held + billing["opening_spent_units"] + billing["opening_held_units"]
            if liability + attempt.reserve_units > billing["ceiling_units"]:
                raise CoordinationError("billing_budget_exhausted")
            spent, held = self._liability(connection, "run_id", attempt.run_id)
            if spent + held + attempt.reserve_units > run["budget_units"]:
                raise CoordinationError("run_budget_exhausted")
            if connection.execute("SELECT COUNT(*) FROM attempts WHERE run_id=? AND state!='not_sent'",
                                  (attempt.run_id,)).fetchone()[0] >= run["request_cap"]:
                raise CoordinationError("request_cap_exhausted")
            last = connection.execute("SELECT MAX(attempt_no),MAX(claim_seq) FROM attempts WHERE run_id=? AND page_number=?",
                                      (attempt.run_id, attempt.page_number)).fetchone()
            if attempt.attempt_no != (last[0] or 0) + 1 or attempt.claim_seq != (last[1] or 0) + 1:
                raise CoordinationError("attempt_sequence_conflict")
            policies = []
            for scope_id, revision in attempt.quota_revisions:
                scope = connection.execute("SELECT * FROM quota_scopes WHERE scope_id=?", (scope_id,)).fetchone()
                if scope["revision"] != revision:
                    raise CoordinationError("policy_revision_mismatch")
                if scope["blocked"]:
                    raise CoordinationError("needs_reconciliation")
                requests, tokens, inflight = self._quota_usage(connection, scope, stamp, meta)
                if (requests + 1 > scope["requests"] or inflight + 1 > scope["concurrency"]
                        or tokens + attempt.input_tokens + attempt.output_tokens > scope["tokens"]):
                    raise CoordinationError("quota_exhausted")
                policies.append(scope)
            connection.execute("INSERT INTO attempts(attempt_id,operation_id,run_id,page_number,attempt_no,claim_seq,"
                               "owner_epoch,binding_id,binding_revision,billing_scope_id,billing_revision,request_digest,"
                               "body_json,input_tokens,output_tokens,reserve_units,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'intent')", (
                attempt.attempt_id, attempt.operation_id, attempt.run_id, attempt.page_number,
                attempt.attempt_no, attempt.claim_seq, owner.epoch, attempt.binding.binding_id,
                attempt.binding.revision, billing["scope_id"], billing["revision"], attempt.request_digest,
                canonical(payload), attempt.input_tokens, attempt.output_tokens, attempt.reserve_units,
            ))
            for scope in policies:
                connection.execute("INSERT INTO attempt_quota_reservations VALUES(?,?,?,?,?,?)", (
                    attempt.attempt_id, scope["scope_id"], scope["revision"], scope["window_ms"],
                    attempt.input_tokens + attempt.output_tokens, canonical(dict(scope)),
                ))
            self._receipt(connection, owner, stamp, attempt.operation_id, "reserve", payload,
                          {"attempt_id": attempt.attempt_id, "owner_epoch": owner.epoch})
            return Reservation(attempt.attempt_id, owner.epoch, True)

    @staticmethod
    def _block_attempt(connection, attempt_id):
        connection.execute("UPDATE billing_scopes SET blocked=1 WHERE scope_id=(SELECT billing_scope_id FROM attempts WHERE attempt_id=?)",
                           (attempt_id,))
        connection.execute("UPDATE quota_scopes SET blocked=1 WHERE scope_id IN (SELECT quota_scope_id FROM attempt_quota_reservations WHERE attempt_id=?)",
                           (attempt_id,))

    def record_outcome(self, owner, outcome: Outcome):
        """Trusted-core facts only. B2/B6 must validate artifact/provider evidence.

        This API does not read files, infer terminality from an HTTP status, or
        clear needs_reconciliation. It is not an administrative release API.
        """
        if type(outcome) is not Outcome:
            raise CoordinationError("invalid_outcome")
        payload = asdict(outcome)
        with self._transaction(owner) as (connection, stamp, _):
            replay = self._replay(connection, outcome.operation_id, "outcome", payload)
            if replay is not None:
                return replay
            attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (outcome.attempt_id,)).fetchone()
            if attempt is None or attempt["request_digest"] != outcome.request_digest:
                raise CoordinationError("evidence_mismatch")
            if attempt["state"] in ("terminal", "not_sent"):
                raise CoordinationError("outcome_conflict")
            if outcome.state == "not_sent" and (attempt["owner_epoch"] != owner.epoch or attempt["state"] != "intent"):
                raise CoordinationError("not_sent_unproven")
            if outcome.state == "unknown":
                self._block_attempt(connection, outcome.attempt_id)
                connection.execute("UPDATE attempts SET state='unknown',evidence_digest=? WHERE attempt_id=?",
                                   (outcome.evidence_digest, outcome.attempt_id))
            else:
                connection.execute("UPDATE attempts SET state=?,actual_units=?,evidence_digest=?,ended_boot_id=?,ended_ms=? WHERE attempt_id=?", (
                    outcome.state, outcome.actual_units, outcome.evidence_digest, stamp.boot_id,
                    stamp.milliseconds, outcome.attempt_id,
                ))
                if outcome.actual_units is not None and outcome.actual_units > attempt["reserve_units"]:
                    self._block_attempt(connection, outcome.attempt_id)
            return self._receipt(connection, owner, stamp, outcome.operation_id, "outcome", payload,
                                 {"attempt_id": outcome.attempt_id, "state": outcome.state})

    def revise_billing(self, owner, operation_id, scope_id, expected_revision, ceiling_units):
        identifier(scope_id)
        integer(expected_revision, minimum=1)
        integer(ceiling_units)
        payload = [scope_id, expected_revision, ceiling_units]
        with self._transaction(owner) as (connection, stamp, _):
            replay = self._replay(connection, operation_id, "billing_policy", payload)
            if replay is not None:
                return replay
            changed = connection.execute("UPDATE billing_scopes SET ceiling_units=?,revision=revision+1 WHERE scope_id=? AND revision=?",
                                         (ceiling_units, scope_id, expected_revision)).rowcount
            if changed != 1:
                raise CoordinationError("revision_conflict")
            return self._receipt(connection, owner, stamp, operation_id, "billing_policy", payload,
                                 {"revision": expected_revision + 1})

    def revise_quota(self, owner, operation_id, scope_id, expected_revision, *, requests, tokens, concurrency, window_ms):
        identifier(scope_id)
        for value in (expected_revision, requests, tokens, concurrency, window_ms):
            integer(value, minimum=1)
        payload = [scope_id, expected_revision, requests, tokens, concurrency, window_ms]
        with self._transaction(owner) as (connection, stamp, _):
            replay = self._replay(connection, operation_id, "quota_policy", payload)
            if replay is not None:
                return replay
            changed = connection.execute("UPDATE quota_scopes SET requests=?,tokens=?,concurrency=?,window_ms=?,revision=revision+1"
                                         " WHERE scope_id=? AND revision=?", (
                requests, tokens, concurrency, window_ms, scope_id, expected_revision,
            )).rowcount
            if changed != 1:
                raise CoordinationError("revision_conflict")
            return self._receipt(connection, owner, stamp, operation_id, "quota_policy", payload,
                                 {"revision": expected_revision + 1})

    def snapshot(self):
        """Private diagnostic accounting, never a public API/authorization."""
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            result = {"installation_id": self.installation_id, "billing": {}, "attempts": []}
            for scope in connection.execute("SELECT * FROM billing_scopes ORDER BY scope_id"):
                spent, held = self._liability(connection, "billing_scope_id", scope["scope_id"])
                result["billing"][scope["scope_id"]] = {
                    "spent_units": spent + scope["opening_spent_units"],
                    "held_units": held + scope["opening_held_units"],
                    "ceiling_units": scope["ceiling_units"], "blocked": bool(scope["blocked"]),
                }
            result["attempts"] = [dict(row) for row in connection.execute(
                "SELECT attempt_id,run_id,owner_epoch,state,reserve_units,actual_units,evidence_digest FROM attempts ORDER BY attempt_id"
            )]
            result["epoch"] = connection.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]
            connection.commit()
            return result

    def schema_contract(self):
        """Read-only compatibility contract, not an upgrade or B6 operation."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM schema_contract").fetchone()
            expected = (SCHEMA_VERSION, RECONCILIATION_MIN_SCHEMA_VERSION,
                        "sealed_history", "explicit_transactional_migration")
            if row != expected:
                raise CoordinationError("schema_contract_mismatch")
            return dict(zip(("schema_version", "reconciliation_min_schema_version",
                             "history_policy", "reconciliation_upgrade"), row))

    def require_reconciliation_schema(self):
        """B6 must never reinterpret v1 immutable rows as mutable release state.

        A future v2 runtime/migrator is separately reviewed. This v1 binary
        cannot open v2 and cannot authorize a reconciliation mutation.
        """
        if self.schema_contract()["reconciliation_min_schema_version"] > SCHEMA_VERSION:
            raise CoordinationError("reconciliation_requires_schema_v2")
