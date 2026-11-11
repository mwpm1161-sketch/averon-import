"""Inert SQLite B1 ledger. Explicit bootstrap only; never composes a live client.

All methods are synchronous: future async composition must call them off-loop
and drain an outstanding commit before handling cancellation. There is no queue,
HTTP permission, result-file writer, reconciliation API or checkpoint migration.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid4, uuid5

from .coordination_contracts import (
    Attempt, BillingScope, Binding, Bootstrap, ClockStamp, CommitReceiptUncertain, CoordinationError,
    DeferredWithoutIntent, DispatchGateRejected, Outcome, Reservation, Run, canonical, fingerprint,
    identifier, integer, uuid_hex,
)
from .coordination_schema import (
    APPLICATION_ID, RECONCILIATION_MIN_SCHEMA_VERSION, SCHEMA_SQL, SCHEMA_VERSION,
)
from .coordination_schema_v2 import schema_sql, capabilities as schema_capabilities
from .coordination_maintenance import MaintenanceClient, _BorrowedMaintenance
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


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _settlement_operation_id(attempt_id):
    return uuid5(UUID(attempt_id), "settlement").hex


def _schema_digest(connection):
    return fingerprint([tuple(row) for row in connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )])


def _expected_schema_digest(version=1):
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema_sql(version))
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
                self._ledger._release_use()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class CoordinationLedger:
    def __init__(self, root, *, installation_id: str, clock=system_clock,
                 busy_timeout_ms: int = 250, lease_ms: int = 30_000,
                 schema_version: int = 1, maintenance_root=None, _maintenance=None):
        self.root = _safe_root(root)
        self.path, self.lock_path = self.root / "coordination.sqlite3", self.root / "dispatcher.lock"
        self.installation_id = uuid_hex(installation_id)
        self.clock = clock
        self.busy_timeout_ms = integer(busy_timeout_ms)
        self.lease_ms = integer(lease_ms, minimum=1)
        schema_sql(schema_version)
        self.schema_version = schema_version
        self._schema_digest = _expected_schema_digest(schema_version)
        self._db_identity = self._lock_identity = None
        self._lifetime_guard = threading.RLock()
        self._users, self._closed, self._maintenance = 0, False, None
        try:
            if _maintenance is not None:
                if type(_maintenance) is not _BorrowedMaintenance or maintenance_root is not None:
                    raise CoordinationError("invalid_maintenance_lease")
                session = _maintenance.session
                if (session.layout.ledger_root != self.root
                        or session.layout.installation_id != self.installation_id):
                    raise CoordinationError("maintenance_binding_mismatch")
                self._maintenance = _maintenance
            elif maintenance_root is not None:
                self._maintenance = MaintenanceClient(maintenance_root, self.root, self.installation_id)
            elif schema_version == 2:
                raise CoordinationError("maintenance_protocol_required")
            self._verify_storage()
            self._db_identity, self._lock_identity = _file_identity(self.path), _file_identity(self.lock_path)
            with self._connection() as connection:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise CoordinationError("ledger_corrupt")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise CoordinationError("ledger_corrupt")
                if _schema_digest(connection) != self._schema_digest:
                    raise CoordinationError("schema_mismatch")
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, PrivateAclError, sqlite3.Error)):
                raise CoordinationError("ledger_unavailable") from None
            raise

    def _retain_use(self):
        with self._lifetime_guard:
            if self._closed:
                raise CoordinationError("ledger_closed")
            self._users += 1

    def _release_use(self):
        with self._lifetime_guard:
            self._users -= 1

    def close(self):
        with self._lifetime_guard:
            if self._users:
                raise CoordinationError("ledger_in_use")
            if not self._closed:
                self._closed = True
                if self._maintenance is not None:
                    self._maintenance.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    def bootstrap(cls, root, spec: Bootstrap, *, clock=system_clock, **options):
        """Explicit attested initialization into a NEW directory; never overwrite."""
        if type(spec) is not Bootstrap:
            raise CoordinationError("invalid_bootstrap")
        if options.get("schema_version", 1) != 1:
            raise CoordinationError("bootstrap_requires_schema_v1")
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
        if self._maintenance is not None:
            self._maintenance.check()
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
        self._retain_use()
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
                    or connection.execute("PRAGMA user_version").fetchone()[0] != self.schema_version
                    or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
                raise CoordinationError("schema_mismatch")
            meta = connection.execute("SELECT installation_id,schema_version FROM meta WHERE id=1").fetchone()
            if meta != (self.installation_id, self.schema_version):
                raise CoordinationError("installation_mismatch")
            if _schema_digest(connection) != self._schema_digest:
                raise CoordinationError("schema_mismatch")
            if self.schema_version == 2 and connection.execute(
                    "SELECT * FROM schema_capabilities").fetchall() != [(1, 1, 0)]:
                raise CoordinationError("capability_mismatch")
            yield connection
        except (OSError, PrivateAclError):
            raise CoordinationError("storage_unverified") from None
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            raise CoordinationError("ledger_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                    else "ledger_unavailable") from None
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._release_use()

    @contextmanager
    def _transaction(self, owner=None, *, acquiring=False):
        guard = owner._guard if type(owner) is DispatcherLease else threading.RLock()
        with guard:
            if not acquiring and type(owner) is not DispatcherLease:
                raise CoordinationError("owner_required")
            committed = False
            try:
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
                            connection.execute("UPDATE meta SET high_water_ms=? WHERE id=1",
                                               (stamp.milliseconds,))
                        yield connection, stamp, meta
                        if not acquiring:
                            owner._check(self)
                            end = self.clock()
                            if (end.boot_id != stamp.boot_id or end.milliseconds < stamp.milliseconds
                                    or end.milliseconds >= connection.execute(
                                        "SELECT lease_until_ms FROM meta WHERE id=1").fetchone()[0]):
                                raise CoordinationError("owner_fenced")
                        self._verify_storage()
                        try:
                            connection.commit()
                        except BaseException:
                            # A failed COMMIT is definitive only if SQLite
                            # still reports an open transaction and rollback
                            # below then succeeds.
                            try:
                                transaction_open = connection.in_transaction
                            except BaseException:
                                raise CommitReceiptUncertain("commit_receipt_uncertain") from None
                            if not transaction_open:
                                raise CommitReceiptUncertain("commit_receipt_uncertain") from None
                            raise
                        committed = True
                        # A slow fsync must not return a usable receipt after
                        # lease expiry. All checks here happen after durable
                        # state changed, so every exception is ambiguous.
                        try:
                            completed = self.clock()
                            until = connection.execute(
                                "SELECT lease_until_ms FROM meta WHERE id=1").fetchone()[0]
                            if (completed.boot_id != stamp.boot_id
                                    or completed.milliseconds < stamp.milliseconds
                                    or completed.milliseconds >= until):
                                raise CoordinationError("owner_fenced")
                        except CommitReceiptUncertain:
                            raise
                        except CoordinationError as exc:
                            # The post-commit phase itself proves this is an
                            # uncertain receipt, even when lease validation
                            # has a familiar deterministic code.
                            raise CommitReceiptUncertain(exc.code) from None
                        except BaseException:
                            raise CommitReceiptUncertain("post_commit_receipt_uncertain") from None
                    except BaseException:
                        try:
                            transaction_open = connection.in_transaction
                        except BaseException:
                            raise CommitReceiptUncertain("rollback_receipt_uncertain") from None
                        if transaction_open:
                            try:
                                connection.rollback()
                            except BaseException:
                                # A failed rollback does not prove the write
                                # was undone, so callers must retain liability.
                                raise CommitReceiptUncertain("rollback_receipt_uncertain") from None
                        raise
            except CommitReceiptUncertain:
                raise
            except BaseException:
                # Also cover exceptions raised while _connection closes or
                # releases storage after a successful commit.
                if committed:
                    raise CommitReceiptUncertain("post_commit_receipt_uncertain") from None
                raise

    def dispatcher(self) -> DispatcherLease:
        """Legacy B2 acquisition: blanket ``intent -> unknown`` is preserved."""
        return self._acquire(legacy=True)

    def dispatcher_for_jobs(self) -> DispatcherLease:
        """B3.4 durable-job acquisition: advance the owner without touching B1.

        The caller must run owner adoption, publication recovery and
        ``recover_unsettled`` before opening any claim. No old ``intent`` row is
        converted to ``unknown`` here; classification needs page identity and
        final evidence first.
        """
        return self._acquire(legacy=False)

    def _acquire(self, *, legacy: bool) -> DispatcherLease:
        self._retain_use()
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
                if legacy:
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
            if not transferred:
                self._release_use()

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
        return self._reserve(owner, attempt, job_claim=None)

    def reserve_job_managed(self, owner, attempt: Attempt, job_claim) -> Reservation:
        """Reserve the B1 intent for one validated B3 job/page claim."""
        return self._reserve(owner, attempt, job_claim=job_claim)

    def _reserve(self, owner, attempt: Attempt, *, job_claim):
        if type(attempt) is not Attempt:
            raise CoordinationError("invalid_attempt")
        if job_claim is not None:
            from .durable_job_contracts import JobPageDispatchClaim
            if type(job_claim) is not JobPageDispatchClaim:
                raise CoordinationError("invalid_job_dispatch_claim")
        payload = asdict(attempt)
        with self._transaction(owner) as (connection, stamp, meta):
            replay = self._replay(connection, attempt.operation_id, "reserve", payload)
            if replay is not None:
                return Reservation(
                    replay["attempt_id"], replay["owner_epoch"], False,
                    replay.get("page_revision"), replay.get("claim_seq"),
                    replay.get("claim_cancel_revision"), replay.get("job_revision"),
                )
            managed = False
            if self.schema_version == 2:
                managed = connection.execute("SELECT 1 FROM durable_jobs WHERE run_id=? LIMIT 1",
                                              (attempt.run_id,)).fetchone() is not None
            if managed and job_claim is None:
                raise CoordinationError("job_dispatch_claim_required")
            if job_claim is not None:
                if not managed:
                    raise CoordinationError("job_dispatch_claim_mismatch")
                self._validate_job_page_claim(connection, owner, job_claim, attempt=attempt, for_gate=False)
            run = connection.execute("SELECT r.* FROM runs r JOIN documents d USING(tenant_id,document_id)"
                                     " WHERE r.run_id=? AND d.active_run_id=r.run_id AND d.generation=r.generation",
                                     (attempt.run_id,)).fetchone()
            if run is None:
                raise CoordinationError("run_fenced")
            if connection.execute("SELECT COUNT(*) FROM attempts WHERE state IN ('intent','unknown')").fetchone()[0] >= meta["global_concurrency"]:
                raise DeferredWithoutIntent("global_capacity_exhausted")
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
                    raise DeferredWithoutIntent("quota_exhausted")
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
            if job_claim is None:
                result = {"attempt_id": attempt.attempt_id, "owner_epoch": owner.epoch}
                self._receipt(connection, owner, stamp, attempt.operation_id, "reserve", payload, result)
                return Reservation(attempt.attempt_id, owner.epoch, True)
            # B3.4a reserve: the B1 attempt and the immutable page identity are
            # one transaction. A partial visible state is never permitted.
            changed = connection.execute(
                "UPDATE durable_job_pages SET state='intent',attempt_id=?,revision=revision+1 "
                "WHERE job_id=? AND run_id=? AND page_number=? AND candidate_attempt_id=? "
                "AND attempt_id IS NULL AND state='claimed' AND claim_seq=? AND owner_epoch=? AND revision=?",
                (job_claim.candidate_attempt_id, job_claim.job_id, job_claim.run_id,
                 job_claim.page_number, job_claim.candidate_attempt_id, job_claim.claim_seq,
                 owner.epoch, job_claim.page_revision),
            ).rowcount
            if changed != 1:
                raise CoordinationError("job_dispatch_claim_stale")
            job_revision = job_claim.job_revision
            if job_revision is None:
                row = connection.execute("SELECT revision FROM durable_jobs WHERE job_id=?",
                                         (job_claim.job_id,)).fetchone()
                if row is None:
                    raise CoordinationError("job_dispatch_claim_stale")
                job_revision = row[0]
            changed = connection.execute(
                "UPDATE durable_jobs SET revision=revision+1,updated_at=? WHERE job_id=? "
                "AND state='running' AND authorized_owner_epoch=? AND cancel_revision=? AND revision=?",
                (_now_iso(), job_claim.job_id, owner.epoch, job_claim.cancel_revision, job_revision),
            ).rowcount
            if changed != 1:
                raise CoordinationError("job_dispatch_claim_stale")
            job_revision += 1
            page_revision = job_claim.page_revision + 1
            result = {
                "attempt_id": attempt.attempt_id,
                "owner_epoch": owner.epoch,
                "page_revision": page_revision,
                "claim_seq": job_claim.claim_seq,
                "claim_cancel_revision": job_claim.cancel_revision,
                "job_revision": job_revision,
            }
            self._receipt(connection, owner, stamp, attempt.operation_id, "reserve", payload, result)
            return Reservation(attempt.attempt_id, owner.epoch, True, page_revision,
                               job_claim.claim_seq, job_claim.cancel_revision, job_revision)

    @staticmethod
    def _validate_job_page_claim(connection, owner, claim, *, attempt=None, for_gate=False):
        """Validate a durable page claim while the caller holds BEGIN IMMEDIATE.

        ``for_gate=False`` is the pre-reserve claim (``claimed`` with no attempt).
        ``for_gate=True`` requires the post-reserve exact ``intent`` page whose
        immutable ``attempt_id`` already equals the candidate identity.
        """
        from .durable_job_contracts import JobPageDispatchClaim
        if type(claim) is not JobPageDispatchClaim:
            raise CoordinationError("invalid_job_dispatch_claim")
        if claim.owner_epoch != owner.epoch:
            raise CoordinationError("job_dispatch_claim_stale")
        job = connection.execute(
            "SELECT job_id,tenant_id,owner_id,document_id,run_id,generation,execution_state,state,"
            "authorized_owner_epoch,cancel_revision,revision FROM durable_jobs WHERE job_id=?",
            (claim.job_id,),
        ).fetchone()
        page = connection.execute(
            "SELECT run_id,page_number,candidate_attempt_id,attempt_id,state,claim_seq,owner_epoch,revision "
            "FROM durable_job_pages WHERE job_id=? AND page_number=?",
            (claim.job_id, claim.page_number),
        ).fetchone()
        expected_state = "intent" if for_gate else "claimed"
        expected_attempt = claim.candidate_attempt_id if for_gate else None
        if (job is None or page is None
                or (job["tenant_id"], job["owner_id"], job["document_id"], job["run_id"], job["generation"])
                != (claim.tenant_id, claim.owner_id, claim.document_id, claim.run_id, claim.generation)
                or job["execution_state"] != "ready" or job["state"] not in ("queued", "running")
                or job["authorized_owner_epoch"] != owner.epoch
                or job["cancel_revision"] != claim.cancel_revision
                or (page["run_id"], page["page_number"], page["candidate_attempt_id"],
                    page["state"], page["claim_seq"], page["owner_epoch"], page["revision"])
                != (claim.run_id, claim.page_number, claim.candidate_attempt_id,
                    expected_state, claim.claim_seq, owner.epoch, claim.page_revision)
                or page["attempt_id"] != expected_attempt):
            raise CoordinationError("job_dispatch_claim_stale")
        if attempt is not None and (
                attempt.run_id != claim.run_id or attempt.page_number != claim.page_number
                or attempt.attempt_id != claim.candidate_attempt_id
                or attempt.claim_seq != claim.claim_seq or attempt.attempt_no != 1):
            raise CoordinationError("job_dispatch_attempt_mismatch")
        return job, page

    @staticmethod
    def _block_attempt(connection, attempt_id):
        connection.execute("UPDATE billing_scopes SET blocked=1 WHERE scope_id=(SELECT billing_scope_id FROM attempts WHERE attempt_id=?)",
                           (attempt_id,))
        connection.execute("UPDATE quota_scopes SET blocked=1 WHERE scope_id IN (SELECT quota_scope_id FROM attempt_quota_reservations WHERE attempt_id=?)",
                           (attempt_id,))

    def checkpoint_attempts(self, owner, expected_run: Run):
        """Owner-validated dispatch history for checkpoint preparation recovery.

        Absence is observed only in this verified existing ledger/current run.
        This read neither settles money nor authorizes HTTP or historical replay.
        """
        if type(expected_run) is not Run:
            raise CoordinationError("invalid_run")
        with self._transaction(owner) as (connection, _stamp, _meta):
            run = connection.execute(
                "SELECT r.body_json FROM runs r JOIN documents d USING(tenant_id,document_id)"
                " WHERE r.run_id=? AND d.active_run_id=r.run_id AND d.generation=r.generation",
                (expected_run.run_id,),
            ).fetchone()
            if run is None or run[0] != canonical(asdict(expected_run)):
                raise CoordinationError("run_fenced")
            return tuple(dict(row) for row in connection.execute(
                "SELECT attempt_id,page_number,attempt_no,claim_seq,state,input_tokens,output_tokens,"
                "reserve_units,actual_units,owner_epoch FROM attempts WHERE run_id=? ORDER BY page_number,attempt_no",
                (expected_run.run_id,),
            ))

    def historical_evidence_facts(self, owner, attempt: Attempt, expected_run: Run):
        """Verify immutable B1 facts for settlement of already-saved evidence.

        This is deliberately independent of current run selection, binding
        activity, current policy revisions, and current admission stops. It is
        a read-only predicate under the current dispatcher owner; callers must
        still pass the exact Outcome to record_outcome() for atomic replay and
        mutation. It never authorizes dispatch.
        """
        if (type(attempt) is not Attempt or type(expected_run) is not Run
                or expected_run.run_id != attempt.run_id):
            raise CoordinationError("historical_identity_mismatch")
        import json
        with self._transaction(owner) as (connection, _stamp, _meta):
            quick_check = tuple(tuple(item) for item in connection.execute("PRAGMA quick_check").fetchall())
            foreign_key_errors = tuple(tuple(item) for item in connection.execute("PRAGMA foreign_key_check").fetchall())
            if quick_check != (("ok",),) or foreign_key_errors:
                raise CoordinationError("ledger_corrupt")

            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (expected_run.run_id,)).fetchone()
            if (run is None or run["body_json"] != canonical(asdict(expected_run))
                    or (run["tenant_id"], run["document_id"], run["generation"],
                        run["budget_units"], run["request_cap"])
                    != (expected_run.tenant_id, expected_run.document_id, expected_run.generation,
                        expected_run.budget_units, expected_run.request_cap)):
                raise CoordinationError("historical_run_mismatch")
            document = connection.execute(
                "SELECT generation FROM documents WHERE tenant_id=? AND document_id=?",
                (expected_run.tenant_id, expected_run.document_id),
            ).fetchone()
            if document is None or document[0] < expected_run.generation:
                raise CoordinationError("historical_run_mismatch")

            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt.attempt_id,)).fetchone()
            expected_attempt = canonical(asdict(attempt))
            if (row is None or row["body_json"] != expected_attempt
                    or (row["operation_id"], row["run_id"], row["page_number"], row["attempt_no"],
                        row["claim_seq"], row["binding_id"], row["binding_revision"],
                        row["billing_scope_id"], row["billing_revision"], row["request_digest"],
                        row["input_tokens"], row["output_tokens"], row["reserve_units"])
                    != (attempt.operation_id, attempt.run_id, attempt.page_number, attempt.attempt_no,
                        attempt.claim_seq, attempt.binding.binding_id, attempt.binding.revision,
                        attempt.binding.billing_scope_id, attempt.billing_revision, attempt.request_digest,
                        attempt.input_tokens, attempt.output_tokens, attempt.reserve_units)
                    or row["state"] not in {"intent", "unknown", "terminal", "not_sent"}
                    or type(row["owner_epoch"]) is not int or row["owner_epoch"] < 1):
                raise CoordinationError("historical_attempt_mismatch")

            binding = connection.execute(
                "SELECT body_json,digest FROM credential_bindings WHERE binding_id=? AND revision=?",
                (attempt.binding.binding_id, attempt.binding.revision),
            ).fetchone()
            if (binding is None or binding["body_json"] != canonical(asdict(attempt.binding))
                    or binding["digest"] != attempt.binding.fingerprint):
                raise CoordinationError("historical_binding_mismatch")
            binding_edges = tuple(item[0] for item in connection.execute(
                "SELECT quota_scope_id FROM binding_quotas WHERE binding_id=? AND binding_revision=?"
                " ORDER BY quota_scope_id", (attempt.binding.binding_id, attempt.binding.revision),
            ))
            if binding_edges != attempt.binding.quota_scope_ids:
                raise CoordinationError("historical_binding_mismatch")

            billing = connection.execute(
                "SELECT revision FROM billing_scopes WHERE scope_id=?",
                (attempt.binding.billing_scope_id,),
            ).fetchone()
            if billing is None or billing[0] < attempt.billing_revision:
                raise CoordinationError("historical_billing_scope_mismatch")

            reservations = connection.execute(
                "SELECT q.*,s.revision AS current_revision FROM attempt_quota_reservations q"
                " LEFT JOIN quota_scopes s ON s.scope_id=q.quota_scope_id"
                " WHERE q.attempt_id=? ORDER BY q.quota_scope_id",
                (attempt.attempt_id,),
            ).fetchall()
            if len(reservations) != len(attempt.quota_revisions):
                raise CoordinationError("historical_quota_reservation_mismatch")
            policy_fields = {
                "scope_id", "revision", "requests", "tokens", "concurrency", "window_ms",
                "opening_requests", "opening_tokens", "opening_inflight", "opening_boot_id",
                "opening_ended_ms", "opening_window_ms", "history_complete", "evidence_digest", "blocked",
            }
            for reservation, (scope_id, revision) in zip(reservations, attempt.quota_revisions):
                try:
                    policy = json.loads(reservation["policy_json"])
                except (TypeError, ValueError, RecursionError):
                    raise CoordinationError("historical_quota_reservation_mismatch") from None
                if (type(policy) is not dict
                        or reservation["quota_scope_id"] != scope_id
                        or reservation["policy_revision"] != revision
                        or reservation["current_revision"] is None
                        or reservation["current_revision"] < revision
                        or reservation["window_ms"] != policy.get("window_ms")
                        or reservation["reserved_tokens"] != attempt.input_tokens + attempt.output_tokens
                        or set(policy) != policy_fields
                        or canonical(policy) != reservation["policy_json"]
                        or policy.get("scope_id") != scope_id or policy.get("revision") != revision
                        or any(type(policy.get(key)) is not int or policy[key] < 1 for key in (
                            "requests", "tokens", "concurrency", "window_ms", "opening_window_ms",
                        ))
                        or any(type(policy.get(key)) is not int or policy[key] < 0 for key in (
                            "opening_requests", "opening_tokens", "opening_inflight", "opening_ended_ms",
                        ))
                        or type(policy.get("history_complete")) is not int or policy["history_complete"] not in (0, 1)
                        or type(policy.get("blocked")) is not int or policy["blocked"] not in (0, 1)
                        or not isinstance(policy.get("opening_boot_id"), str)
                        or not isinstance(policy.get("evidence_digest"), str)
                        or len(policy["evidence_digest"]) != 64
                        or any(character not in "0123456789abcdef" for character in policy["evidence_digest"])):
                    raise CoordinationError("historical_quota_reservation_mismatch")
                try:
                    uuid_hex(policy["opening_boot_id"])
                except CoordinationError:
                    raise CoordinationError("historical_quota_reservation_mismatch") from None

            receipt = connection.execute(
                "SELECT kind,request_digest,request_json,result_json,epoch,boot_id,created_ms"
                " FROM operations WHERE operation_id=?", (attempt.operation_id,),
            ).fetchone()
            expected_reservation = canonical({"attempt_id": attempt.attempt_id, "owner_epoch": row["owner_epoch"]})
            if (receipt is None or receipt["kind"] != "reserve"
                    or receipt["request_digest"] != fingerprint(asdict(attempt))
                    or receipt["request_json"] != expected_attempt
                    or receipt["result_json"] != expected_reservation
                    or receipt["epoch"] != row["owner_epoch"]
                    or type(receipt["created_ms"]) is not int or receipt["created_ms"] < 0):
                raise CoordinationError("historical_reserve_receipt_mismatch")
            try:
                uuid_hex(receipt["boot_id"])
            except CoordinationError:
                raise CoordinationError("historical_reserve_receipt_mismatch") from None

            return {
                "owner_epoch": row["owner_epoch"],
                "state": row["state"],
                "actual_units": row["actual_units"],
                "evidence_digest": row["evidence_digest"],
            }

    def validate_dispatch(self, owner, attempt: Attempt, *, expected_run: Run, evidence_only=False):
        """Revalidate an existing intent at the B2 boundary; never mint a permit.

        B2 separately requires a fresh reserve receipt and consumes its one-shot
        process capability. This short transaction is not held during HTTP.
        evidence_only validates old facts for settlement/checkpoint recovery;
        it is never used to mint or consume a send capability.
        """
        if type(attempt) is not Attempt or type(expected_run) is not Run or expected_run.run_id != attempt.run_id:
            raise CoordinationError("invalid_attempt")
        with self._transaction(owner) as (connection, _stamp, _meta):
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                     (attempt.attempt_id,)).fetchone()
            if (row is None or row["body_json"] != canonical(asdict(attempt))
                    or (not evidence_only and (row["owner_epoch"] != owner.epoch or row["state"] != "intent"))):
                raise CoordinationError("dispatch_intent_mismatch")
            current = connection.execute(
                "SELECT r.body_json FROM runs r JOIN documents d USING(tenant_id,document_id)"
                " WHERE r.run_id=? AND d.active_run_id=r.run_id AND d.generation=r.generation",
                (attempt.run_id,),
            ).fetchone()
            if current is None or current[0] != canonical(asdict(expected_run)):
                raise CoordinationError("run_fenced")
            binding = connection.execute(
                "SELECT digest FROM credential_bindings WHERE binding_id=? AND revision=? AND active=1",
                (attempt.binding.binding_id, attempt.binding.revision),
            ).fetchone()
            if binding is None or binding[0] != attempt.binding.fingerprint:
                raise CoordinationError("binding_mismatch")
            for table, scope, revision in (
                ("billing_scopes", attempt.binding.billing_scope_id, attempt.billing_revision),
                *(("quota_scopes", scope, revision) for scope, revision in attempt.quota_revisions),
            ):
                policy = connection.execute(f"SELECT revision,blocked FROM {table} WHERE scope_id=?",
                                            (scope,)).fetchone()
                if policy is None or policy["revision"] != revision:
                    raise CoordinationError("policy_revision_mismatch")
                if policy["blocked"] and not evidence_only:
                    raise CoordinationError("needs_reconciliation")

    def commit_job_send_gate(self, owner, attempt: Attempt, *, expected_run: Run, claim):
        """Atomically authorize a job-managed send and consume its page claim.

        This is the cancel/send linearization point. It validates all B1 intent,
        owner, run, binding and current-policy facts together with the durable
        job/page fences, then commits ``claimed -> send_committed`` in this same
        BEGIN IMMEDIATE transaction. It never creates a financial intent or
        performs transport work.
        """
        from .durable_job_contracts import JobPageDispatchClaim
        if (type(attempt) is not Attempt or type(expected_run) is not Run
                or type(claim) is not JobPageDispatchClaim
                or expected_run.run_id != attempt.run_id):
            raise DispatchGateRejected("invalid_job_send_gate")
        try:
            with self._transaction(owner) as (connection, _stamp, _meta):
                row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                         (attempt.attempt_id,)).fetchone()
                if (row is None or row["body_json"] != canonical(asdict(attempt))
                        or (row["operation_id"], row["run_id"], row["page_number"], row["attempt_no"],
                            row["claim_seq"], row["owner_epoch"], row["binding_id"],
                            row["binding_revision"], row["billing_scope_id"], row["billing_revision"],
                            row["request_digest"], row["input_tokens"], row["output_tokens"],
                            row["reserve_units"], row["state"])
                        != (attempt.operation_id, attempt.run_id, attempt.page_number, attempt.attempt_no,
                            attempt.claim_seq, owner.epoch, attempt.binding.binding_id,
                            attempt.binding.revision, attempt.binding.billing_scope_id,
                            attempt.billing_revision, attempt.request_digest, attempt.input_tokens,
                            attempt.output_tokens, attempt.reserve_units, "intent")):
                    raise CoordinationError("dispatch_intent_mismatch")

                reserve_receipt = connection.execute(
                    "SELECT kind,request_digest,request_json,result_json,epoch FROM operations WHERE operation_id=?",
                    (attempt.operation_id,),
                ).fetchone()
                expected_result = canonical({
                    "attempt_id": attempt.attempt_id,
                    "owner_epoch": claim.owner_epoch,
                    "page_revision": claim.page_revision,
                    "claim_seq": claim.claim_seq,
                    "claim_cancel_revision": claim.cancel_revision,
                    "job_revision": claim.job_revision,
                })
                if (reserve_receipt is None or reserve_receipt["kind"] != "reserve"
                        or reserve_receipt["request_digest"] != fingerprint(asdict(attempt))
                        or reserve_receipt["request_json"] != canonical(asdict(attempt))
                        or reserve_receipt["result_json"] != expected_result
                        or reserve_receipt["epoch"] != claim.owner_epoch):
                    raise CoordinationError("dispatch_reserve_receipt_mismatch")

                run = connection.execute(
                    "SELECT r.body_json,r.tenant_id,r.document_id,r.generation,d.generation AS current_generation,"
                    "d.active_run_id FROM runs r JOIN documents d USING(tenant_id,document_id) WHERE r.run_id=?",
                    (attempt.run_id,),
                ).fetchone()
                if (run is None or run["body_json"] != canonical(asdict(expected_run))
                        or (run["tenant_id"], run["document_id"], run["generation"],
                            run["current_generation"], run["active_run_id"])
                        != (claim.tenant_id, claim.document_id, claim.generation,
                            claim.generation, claim.run_id)
                        or (expected_run.tenant_id, expected_run.document_id, expected_run.generation,
                            expected_run.source_digest)
                        != (claim.tenant_id, claim.document_id, claim.generation,
                            expected_run.source_digest)):
                    raise CoordinationError("run_fenced")

                binding = connection.execute(
                    "SELECT body_json,digest,active,billing_scope_id FROM credential_bindings "
                    "WHERE binding_id=? AND revision=?",
                    (attempt.binding.binding_id, attempt.binding.revision),
                ).fetchone()
                quota_edges = tuple(item[0] for item in connection.execute(
                    "SELECT quota_scope_id FROM binding_quotas WHERE binding_id=? AND binding_revision=? "
                    "ORDER BY quota_scope_id", (attempt.binding.binding_id, attempt.binding.revision)))
                if (binding is None or binding["active"] != 1
                        or binding["body_json"] != canonical(asdict(attempt.binding))
                        or binding["digest"] != attempt.binding.fingerprint
                        or binding["billing_scope_id"] != attempt.binding.billing_scope_id
                        or quota_edges != attempt.binding.quota_scope_ids):
                    raise CoordinationError("binding_mismatch")

                billing = connection.execute(
                    "SELECT revision,blocked FROM billing_scopes WHERE scope_id=?",
                    (attempt.binding.billing_scope_id,),
                ).fetchone()
                if billing is None or billing["revision"] != attempt.billing_revision:
                    raise CoordinationError("policy_revision_mismatch")
                if billing["blocked"]:
                    raise CoordinationError("needs_reconciliation")
                quota_holds = connection.execute(
                    "SELECT quota_scope_id,policy_revision,window_ms,reserved_tokens,policy_json "
                    "FROM attempt_quota_reservations WHERE attempt_id=? ORDER BY quota_scope_id",
                    (attempt.attempt_id,),
                ).fetchall()
                if len(quota_holds) != len(attempt.quota_revisions):
                    raise CoordinationError("dispatch_quota_hold_mismatch")
                for hold, (scope_id, revision) in zip(quota_holds, attempt.quota_revisions):
                    scope = connection.execute("SELECT * FROM quota_scopes WHERE scope_id=?",
                                               (scope_id,)).fetchone()
                    if (scope is None or scope["revision"] != revision
                            or hold["quota_scope_id"] != scope_id
                            or hold["policy_revision"] != revision
                            or hold["window_ms"] != scope["window_ms"]
                            or hold["reserved_tokens"] != attempt.input_tokens + attempt.output_tokens
                            or hold["policy_json"] != canonical(dict(scope))):
                        raise CoordinationError("policy_revision_mismatch")
                    if scope["blocked"]:
                        raise CoordinationError("needs_reconciliation")

                self._validate_job_page_claim(connection, owner, claim, attempt=attempt, for_gate=True)
                changed = connection.execute(
                    "UPDATE durable_job_pages SET state='send_committed',revision=revision+1 "
                    "WHERE job_id=? AND run_id=? AND page_number=? AND candidate_attempt_id=? "
                    "AND attempt_id=? AND state='intent' AND claim_seq=? AND owner_epoch=? AND revision=? "
                    "AND EXISTS (SELECT 1 FROM durable_jobs j WHERE j.job_id=? AND j.tenant_id=? "
                    "AND j.owner_id=? AND j.document_id=? AND j.run_id=? AND j.generation=? "
                    "AND j.execution_state='ready' AND j.state IN ('queued','running') "
                    "AND j.authorized_owner_epoch=? AND j.cancel_revision=?)",
                    (claim.job_id, claim.run_id, claim.page_number,
                     claim.candidate_attempt_id, claim.candidate_attempt_id,
                     claim.claim_seq, owner.epoch, claim.page_revision,
                     claim.job_id, claim.tenant_id, claim.owner_id, claim.document_id, claim.run_id,
                     claim.generation, owner.epoch, claim.cancel_revision),
                ).rowcount
                if changed != 1:
                    raise CoordinationError("job_dispatch_claim_stale")
        except (DispatchGateRejected, CommitReceiptUncertain):
            raise
        except CoordinationError as exc:
            # Predicate failures happen before commit and are distinguishable
            # from an ambiguous SQLite commit receipt at the B2 boundary.
            raise DispatchGateRejected(exc.code) from None

    def record_outcome(self, owner, outcome: Outcome, *, job_claim=None):
        """Trusted-core facts only. B2/B6 must validate artifact/provider evidence.

        This API does not read files, infer terminality from an HTTP status, or
        clear needs_reconciliation. It is not an administrative release API.
        """
        if type(outcome) is not Outcome:
            raise CoordinationError("invalid_outcome")
        if job_claim is not None:
            from .durable_job_contracts import JobPageDispatchClaim
            if type(job_claim) is not JobPageDispatchClaim or job_claim.candidate_attempt_id != outcome.attempt_id:
                raise CoordinationError("invalid_job_dispatch_claim")
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
                if outcome.state == "not_sent" and job_claim is not None:
                    # A durable cancel that won before the gate has already
                    # invalidated the claim. Link the proven no-send result to
                    # the canceled page in the same financial transaction.
                    connection.execute(
                        "UPDATE durable_job_pages SET state='cancelled',safe_code='cancelled',revision=revision+1 "
                        "WHERE job_id=? AND run_id=? AND page_number=? AND candidate_attempt_id=? "
                        "AND attempt_id IS NULL AND state='claimed' AND claim_seq=? AND owner_epoch=? AND revision=? "
                        "AND EXISTS (SELECT 1 FROM durable_jobs j WHERE j.job_id=? AND j.tenant_id=? "
                        "AND j.owner_id=? AND j.document_id=? AND j.run_id=? AND j.generation=? "
                        "AND j.state IN ('cancelling','cancelled') AND j.cancel_revision>?)",
                        (job_claim.job_id, job_claim.run_id, job_claim.page_number,
                         job_claim.candidate_attempt_id, job_claim.claim_seq, job_claim.owner_epoch,
                         job_claim.page_revision, job_claim.job_id, job_claim.tenant_id,
                         job_claim.owner_id, job_claim.document_id, job_claim.run_id,
                         job_claim.generation, job_claim.cancel_revision),
                    )
                if outcome.actual_units is not None and outcome.actual_units > attempt["reserve_units"]:
                    self._block_attempt(connection, outcome.attempt_id)
            return self._receipt(connection, owner, stamp, outcome.operation_id, "outcome", payload,
                                 {"attempt_id": outcome.attempt_id, "state": outcome.state})

    # ------------------------------------------------------------------
    # B3.4a durable-job lifecycle: adoption, atomic settlement, recovery.
    # All writes below are owner-fenced; B1 attempt identity is never rewritten.
    # ------------------------------------------------------------------

    @staticmethod
    def _job_relevant_scopes(connection, job_id):
        """Derive scope membership only from this job's persisted B1 attempts."""
        billing = tuple(row[0] for row in connection.execute(
            "SELECT DISTINCT a.billing_scope_id FROM attempts a"
            " JOIN durable_job_pages p ON p.run_id=a.run_id AND p.page_number=a.page_number"
            " WHERE p.job_id=? ORDER BY a.billing_scope_id", (job_id,)))
        quotas = tuple(row[0] for row in connection.execute(
            "SELECT DISTINCT q.quota_scope_id FROM attempt_quota_reservations q"
            " JOIN attempts a USING(attempt_id)"
            " JOIN durable_job_pages p ON p.run_id=a.run_id AND p.page_number=a.page_number"
            " WHERE p.job_id=? ORDER BY q.quota_scope_id", (job_id,)))
        return billing, quotas

    @staticmethod
    def _terminal_target(states):
        if all(state == "completed" for state in states):
            return "completed"
        if any(state in ("completed", "incomplete") for state in states):
            return "completed_with_errors"
        return "failed"

    @classmethod
    def _rollup_job(cls, connection, job_id):
        """Recompute the job projection from the transaction snapshot."""
        job = connection.execute("SELECT state,revision FROM durable_jobs WHERE job_id=?",
                                 (job_id,)).fetchone()
        if job is None:
            raise CoordinationError("durable_job_not_found")
        states = tuple(row[0] for row in connection.execute(
            "SELECT state FROM durable_job_pages WHERE job_id=? ORDER BY page_number", (job_id,)))
        attempt_states = tuple(row[0] for row in connection.execute(
            "SELECT a.state FROM attempts a JOIN durable_job_pages p"
            " ON p.run_id=a.run_id AND p.page_number=a.page_number WHERE p.job_id=?", (job_id,)))
        billing, quotas = cls._job_relevant_scopes(connection, job_id)
        blocked_scope = False
        for scope in billing:
            row = connection.execute("SELECT blocked FROM billing_scopes WHERE scope_id=?",
                                     (scope,)).fetchone()
            if row is not None and row[0]:
                blocked_scope = True
        for scope in quotas:
            row = connection.execute("SELECT blocked FROM quota_scopes WHERE scope_id=?",
                                     (scope,)).fetchone()
            if row is not None and row[0]:
                blocked_scope = True
        overage = connection.execute(
            "SELECT 1 FROM attempts a JOIN durable_job_pages p"
            " ON p.run_id=a.run_id AND p.page_number=a.page_number"
            " WHERE p.job_id=? AND a.state='terminal' AND a.actual_units>a.reserve_units LIMIT 1",
            (job_id,)).fetchone() is not None
        unknown = ("unknown_outcome" in states or "unknown" in attempt_states
                   or blocked_scope or overage)
        current = job["state"]
        if unknown:
            target = "blocked"
        elif current in ("completed", "completed_with_errors", "cancelled", "failed"):
            target = current
        elif current == "cancelling":
            unsettled = (any(state in ("intent", "send_committed", "settlement_pending")
                             for state in states) or "intent" in attempt_states)
            target = "cancelling" if unsettled else "cancelled"
        elif current == "blocked":
            target = "blocked"
        else:
            nonterminal = any(state in ("queued", "claimed", "quota_wait", "intent",
                                        "send_committed", "settlement_pending") for state in states)
            if nonterminal:
                if current == "paused_restart":
                    target = "paused_restart"
                else:
                    target = current if current in ("queued", "running") else "running"
            else:
                target = cls._terminal_target(states)
        if target != current:
            finished = (_now_iso() if target in
                        ("completed", "completed_with_errors", "cancelled", "failed") else None)
            connection.execute(
                "UPDATE durable_jobs SET state=?,revision=revision+1,updated_at=?,finished_at=?"
                " WHERE job_id=?", (target, _now_iso(), finished, job_id))
        return target

    @staticmethod
    def _mark_paused_restart_tx(connection, job_id):
        row = connection.execute("SELECT state FROM durable_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is not None and row[0] in ("queued", "running"):
            connection.execute(
                "UPDATE durable_jobs SET state='paused_restart',revision=revision+1,updated_at=?"
                " WHERE job_id=?", (_now_iso(), job_id))
            return True
        return False

    @staticmethod
    def _pre_gate_proof(attempt_id, run_id, page, claim_seq, prior_owner_epoch, page_state):
        return fingerprint({
            "attempt_id": attempt_id, "run_id": run_id, "page": page,
            "claim_seq": claim_seq, "prior_owner_epoch": prior_owner_epoch,
            "page_state": page_state, "reason": "pre_gate_not_sent",
        })

    @staticmethod
    def _reserve_claim_cancel_revision(connection, attempt_operation_id):
        row = connection.execute("SELECT result_json FROM operations WHERE operation_id=?",
                                 (attempt_operation_id,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0]).get("claim_cancel_revision")
        except (TypeError, ValueError):
            return None
        return value if type(value) is int and value >= 0 else None

    def adopt_job_owner_epochs(self, owner, *, batch_size: int = 50):
        """Guarded, idempotent adoption of prior-epoch ``running`` jobs (A3)."""
        from .durable_job_contracts import AdoptionReport
        integer(batch_size, minimum=1)
        adopted, quarantined = [], []
        with self._transaction(owner) as (connection, _stamp, _meta):
            rows = connection.execute(
                "SELECT j.job_id,j.revision,j.authorized_owner_epoch,j.run_id,j.generation,"
                "j.execution_state,d.active_run_id,d.generation AS current_generation"
                " FROM durable_jobs j JOIN documents d USING(tenant_id,document_id)"
                " WHERE j.state='running' AND (j.authorized_owner_epoch IS NULL"
                " OR j.authorized_owner_epoch<?) ORDER BY j.job_id LIMIT ?",
                (owner.epoch, batch_size)).fetchall()
            for row in rows:
                if (row["authorized_owner_epoch"] is None
                        or row["authorized_owner_epoch"] >= owner.epoch
                        or row["execution_state"] != "ready"
                        or row["active_run_id"] != row["run_id"]
                        or row["current_generation"] != row["generation"]):
                    quarantined.append((row["job_id"], "job_adoption_integrity"))
                    continue
                changed = connection.execute(
                    "UPDATE durable_jobs SET authorized_owner_epoch=?,revision=revision+1,updated_at=?"
                    " WHERE job_id=? AND state='running' AND revision=?"
                    " AND authorized_owner_epoch=?",
                    (owner.epoch, _now_iso(), row["job_id"], row["revision"],
                     row["authorized_owner_epoch"])).rowcount
                if changed == 1:
                    adopted.append(row["job_id"])
                else:
                    quarantined.append((row["job_id"], "job_adoption_stale"))
        return AdoptionReport(tuple(adopted), tuple(quarantined))

    def verify_registered_run(self, owner, run: Run):
        """Read-only run adoption probe used by ``register()`` (IN-12/13/14).

        Returns ``exact``, ``absent`` or ``conflict``; never mutates.
        """
        if type(run) is not Run:
            raise CoordinationError("invalid_run")
        with self._transaction(owner) as (connection, _stamp, _meta):
            row = connection.execute(
                "SELECT r.body_json,r.tenant_id,r.document_id,r.generation,"
                "d.active_run_id,d.generation AS current_generation FROM runs r"
                " JOIN documents d USING(tenant_id,document_id) WHERE r.run_id=?",
                (run.run_id,)).fetchone()
            if row is None:
                return "absent"
            if (row["body_json"] == canonical(asdict(run))
                    and (row["tenant_id"], row["document_id"], row["generation"])
                    == (run.tenant_id, run.document_id, run.generation)
                    and row["active_run_id"] == run.run_id
                    and row["current_generation"] == run.generation):
                return "exact"
            return "conflict"

    def claim_for_attempt(self, owner, attempt_id):
        """Reconstruct the exact persisted claim identity for settlement.

        Settlement must never trust a mutable caller claim object: a stale or
        forged in-memory claim can only cause the gate to reject. The immutable
        B1 attempt, page identity and reserve receipt are the authority here.
        """
        from .durable_job_contracts import JobPageDispatchClaim
        uuid_hex(attempt_id)
        with self._transaction(owner) as (connection, _stamp, _meta):
            attempt = connection.execute(
                "SELECT run_id,page_number,claim_seq,owner_epoch,operation_id FROM attempts"
                " WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise CoordinationError("historical_attempt_mismatch")
            page = connection.execute(
                "SELECT p.job_id,p.run_id,p.page_number,p.candidate_attempt_id,p.attempt_id,"
                "p.claim_seq,p.revision,j.tenant_id,j.owner_id,j.document_id,j.generation,"
                "j.cancel_revision FROM durable_job_pages p JOIN durable_jobs j USING(job_id)"
                " WHERE p.run_id=? AND p.page_number=?",
                (attempt["run_id"], attempt["page_number"])).fetchone()
            if (page is None or page["attempt_id"] != attempt_id
                    or page["candidate_attempt_id"] != attempt_id
                    or (page["run_id"], page["page_number"], page["claim_seq"])
                    != (attempt["run_id"], attempt["page_number"], attempt["claim_seq"])):
                raise CoordinationError("job_dispatch_claim_stale")
            receipt = {}
            row = connection.execute("SELECT result_json FROM operations WHERE operation_id=?",
                                     (attempt["operation_id"],)).fetchone()
            if row is not None:
                try:
                    loaded = json.loads(row[0])
                    receipt = loaded if type(loaded) is dict else {}
                except (TypeError, ValueError):
                    receipt = {}
            cancel_revision = receipt.get("claim_cancel_revision")
            page_revision = receipt.get("page_revision")
            job_revision = receipt.get("job_revision")
            return JobPageDispatchClaim(
                page["tenant_id"], page["owner_id"], page["document_id"], page["job_id"],
                page["run_id"], page["generation"], page["page_number"],
                page["candidate_attempt_id"], page["claim_seq"],
                page_revision if type(page_revision) is int and page_revision >= 1 else page["revision"],
                attempt["owner_epoch"],
                cancel_revision if type(cancel_revision) is int and cancel_revision >= 0 else 0,
                job_revision if type(job_revision) is int and job_revision >= 1 else None,
            )

    def record_job_outcome(self, owner, outcome: Outcome, *, job_claim, page,
                           generation: int | None = None):
        """Atomic B1 attempt + durable page + job roll-up settlement."""
        from .durable_job_contracts import JobPageDispatchClaim, PageOutcomeProjection
        if (type(outcome) is not Outcome or type(job_claim) is not JobPageDispatchClaim
                or (page is not None and type(page) is not PageOutcomeProjection)):
            raise CoordinationError("invalid_job_outcome")
        with self._transaction(owner) as (connection, stamp, _meta):
            return self._record_job_outcome_tx(connection, owner, stamp, outcome,
                                               job_claim, page, generation)

    def _record_job_outcome_tx(self, connection, owner, stamp, outcome, claim, page, generation):
        from .durable_job_contracts import PageOutcomeProjection
        existing = connection.execute(
            "SELECT kind,request_json,result_json FROM operations WHERE operation_id=?",
            (outcome.operation_id,)).fetchone()
        if existing is not None:
            if existing["kind"] != "outcome":
                raise CoordinationError("operation_conflict")
            try:
                saved = json.loads(existing["request_json"])
            except (TypeError, ValueError):
                raise CoordinationError("historical_outcome_conflict") from None
            if (saved.get("attempt_id") != outcome.attempt_id
                    or saved.get("request_digest") != outcome.request_digest
                    or saved.get("resulting_attempt_state") != outcome.state
                    or saved.get("proof_or_evidence_digest") != outcome.evidence_digest
                    or saved.get("expected_attempt_state") != "intent"):
                raise CoordinationError("historical_outcome_conflict")
            return json.loads(existing["result_json"])
        attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                     (outcome.attempt_id,)).fetchone()
        if attempt is None or attempt["request_digest"] != outcome.request_digest:
            raise CoordinationError("evidence_mismatch")
        if attempt["state"] in ("terminal", "not_sent"):
            raise CoordinationError("outcome_conflict")
        page_row = connection.execute(
            "SELECT p.*,j.state AS job_state,j.cancel_revision AS job_cancel_revision,"
            "j.generation AS job_generation FROM durable_job_pages p"
            " JOIN durable_jobs j USING(job_id) WHERE p.job_id=? AND p.page_number=?",
            (claim.job_id, claim.page_number)).fetchone()
        if page_row is None:
            raise CoordinationError("job_dispatch_claim_stale")
        # A legacy page may still have attempt_id NULL while an immutable B1
        # attempt exists (R-02/R-23/R-25/R-30). Recovery attaches it atomically.
        if (page_row["run_id"], page_row["candidate_attempt_id"], page_row["claim_seq"]) != (
                claim.run_id, claim.candidate_attempt_id, claim.claim_seq
        ) or page_row["attempt_id"] not in (None, outcome.attempt_id):
            raise CoordinationError("job_dispatch_claim_stale")
        if generation is not None and page_row["job_generation"] != generation:
            raise CoordinationError("job_dispatch_claim_stale")
        prior_state = attempt["state"]
        reserve_cancel_revision = self._reserve_claim_cancel_revision(
            connection, attempt["operation_id"])
        if reserve_cancel_revision is None:
            reserve_cancel_revision = claim.cancel_revision
        if outcome.state == "not_sent":
            if prior_state != "intent" or page_row["state"] not in (
                    "intent", "claimed", "failed", "cancelled"):
                raise CoordinationError("not_sent_unproven")
            cancel_won = (page_row["job_state"] in ("cancelling", "cancelled", "blocked")
                          and page_row["job_cancel_revision"] > reserve_cancel_revision)
            projection = PageOutcomeProjection(
                "cancelled" if cancel_won else "failed",
                "cancelled_pre_gate" if cancel_won else "not_sent_pre_gate",
                0, outcome.evidence_digest)
        elif outcome.state == "terminal":
            if prior_state != "intent" or page is None:
                raise CoordinationError("outcome_conflict")
            if page_row["state"] not in ("send_committed", "settlement_pending"):
                raise CoordinationError("job_dispatch_claim_stale")
            projection = page
        elif outcome.state == "unknown":
            if prior_state not in ("intent", "unknown"):
                raise CoordinationError("outcome_conflict")
            if page is None:
                projection = None
            else:
                if page_row["state"] not in ("send_committed", "unknown_outcome", "blocked"):
                    raise CoordinationError("job_dispatch_claim_stale")
                projection = page
        else:
            raise CoordinationError("invalid_outcome")
        if outcome.state == "unknown":
            self._block_attempt(connection, outcome.attempt_id)
            connection.execute(
                "UPDATE attempts SET state='unknown',evidence_digest=? WHERE attempt_id=?",
                (outcome.evidence_digest, outcome.attempt_id))
        elif outcome.state == "not_sent":
            connection.execute(
                "UPDATE attempts SET state='not_sent',evidence_digest=?,ended_boot_id=?,ended_ms=?"
                " WHERE attempt_id=?", (outcome.evidence_digest, stamp.boot_id,
                                        stamp.milliseconds, outcome.attempt_id))
        else:
            connection.execute(
                "UPDATE attempts SET state='terminal',actual_units=?,evidence_digest=?,"
                "ended_boot_id=?,ended_ms=? WHERE attempt_id=?",
                (outcome.actual_units, outcome.evidence_digest, stamp.boot_id,
                 stamp.milliseconds, outcome.attempt_id))
            if outcome.actual_units is not None and outcome.actual_units > attempt["reserve_units"]:
                self._block_attempt(connection, outcome.attempt_id)
        if projection is not None:
            changed = connection.execute(
                "UPDATE durable_job_pages SET attempt_id=?,state=?,safe_code=?,result_bytes=?,"
                "evidence_digest=?,revision=revision+1 WHERE job_id=? AND page_number=? "
                "AND (attempt_id IS NULL OR attempt_id=?) AND revision=?",
                (outcome.attempt_id, projection.state, projection.safe_code,
                 projection.result_bytes, projection.evidence_digest, claim.job_id,
                 claim.page_number, outcome.attempt_id, page_row["revision"])).rowcount
            if changed != 1:
                raise CoordinationError("job_dispatch_claim_stale")
        job_state = self._rollup_job(connection, claim.job_id)
        payload = {
            "version": 1,
            "operation_id": outcome.operation_id,
            "attempt_id": outcome.attempt_id,
            "run_id": claim.run_id,
            "job_id": claim.job_id,
            "generation": generation if generation is not None else page_row["job_generation"],
            "page_number": claim.page_number,
            "attempt_no": attempt["attempt_no"],
            "claim_seq": claim.claim_seq,
            "request_digest": outcome.request_digest,
            "prior_owner_epoch": attempt["owner_epoch"],
            "expected_attempt_state": prior_state,
            "resulting_attempt_state": outcome.state,
            "claim_cancel_revision": reserve_cancel_revision,
            "proof_or_evidence_digest": outcome.evidence_digest,
            "page_projection": None if projection is None else {
                "state": projection.state, "safe_code": projection.safe_code,
                "result_bytes": projection.result_bytes},
        }
        result = {"attempt_id": outcome.attempt_id, "state": outcome.state,
                  "job_state": job_state}
        return self._receipt(connection, owner, stamp, outcome.operation_id, "outcome",
                             payload, result)

    def settle_pre_gate_not_sent(self, owner, *, attempt_id, claim, page_state="intent",
                                 proof=None):
        """Current-owner or recovery pre-gate settlement using the canonical identity."""
        from .durable_job_contracts import PageOutcomeProjection
        with self._transaction(owner) as (connection, stamp, _meta):
            attempt = connection.execute(
                "SELECT request_digest,run_id,page_number,claim_seq,owner_epoch FROM attempts"
                " WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise CoordinationError("historical_attempt_mismatch")
            if proof is None:
                proof = self._pre_gate_proof(attempt_id, attempt["run_id"],
                                             attempt["page_number"], attempt["claim_seq"],
                                             attempt["owner_epoch"], page_state)
            outcome = Outcome(_settlement_operation_id(attempt_id), attempt_id,
                              attempt["request_digest"], proof, "not_sent", None)
            return self._record_job_outcome_tx(
                connection, owner, stamp, outcome, claim,
                PageOutcomeProjection("failed", "not_sent_pre_gate", 0, proof), None)

    @staticmethod
    def _claim_from_recovery_row(row, owner_epoch):
        from .durable_job_contracts import JobPageDispatchClaim
        return JobPageDispatchClaim(
            row["tenant_id"], row["owner_id"], row["document_id"], row["job_id"],
            row["run_id"], row["job_generation"], row["page_number"],
            row["candidate_attempt_id"], row["page_claim_seq"], row["page_revision"],
            owner_epoch, row.get("claim_cancel_revision") or 0, None)

    def _evidence_page_projection(self, payload):
        from .durable_job_contracts import PageOutcomeProjection
        settlement = payload.get("settlement")
        result = payload.get("result")
        result_bytes = 0
        if result is not None:
            result_bytes = len(canonical(result).encode("utf-8"))
        if type(settlement) is dict:
            state = settlement.get("page_state")
            safe_code = settlement.get("safe_code")
            if state not in ("completed", "incomplete", "failed"):
                raise CoordinationError("result_evidence_invalid")
            stored_bytes = settlement.get("result_bytes", result_bytes)
            if type(stored_bytes) is not int or not 0 <= stored_bytes <= 16_000_000:
                raise CoordinationError("result_evidence_invalid")
            return PageOutcomeProjection(state, safe_code, stored_bytes,
                                         payload["outcome"]["evidence_digest"])
        status = None
        if type(result) is dict:
            extraction = result.get("extraction")
            if type(extraction) is dict:
                status = extraction.get("status")
                safe_code = extraction.get("error_code")
            else:
                safe_code = None
        else:
            safe_code = None
        if status in ("extracted", "no_specification"):
            state = "completed"
        elif status == "incomplete":
            state = "incomplete"
        elif status == "failed":
            state = "failed"
        else:
            state = "failed"
            safe_code = safe_code or "result_invalid"
        return PageOutcomeProjection(state, safe_code, result_bytes,
                                     payload["outcome"]["evidence_digest"])

    def _legacy_receipts(self, connection, attempt):
        reserve = connection.execute(
            "SELECT kind,request_digest,request_json,result_json FROM operations"
            " WHERE operation_id=?", (attempt["operation_id"],)).fetchone()
        outcome = connection.execute(
            "SELECT kind,request_digest,request_json,result_json FROM operations"
            " WHERE operation_id=?", (_settlement_operation_id(attempt["attempt_id"]),)).fetchone()
        return reserve, outcome

    @staticmethod
    def _receipt_matches(receipt, kind, attempt, expected_state):
        if receipt is None or receipt["kind"] != kind:
            return False
        try:
            request = json.loads(receipt["request_json"])
            result = json.loads(receipt["result_json"])
        except (TypeError, ValueError):
            return False
        if kind == "reserve":
            return (request.get("attempt_id") == attempt["attempt_id"]
                    and request.get("owner_epoch") == attempt["owner_epoch"]
                    and result.get("attempt_id") == attempt["attempt_id"])
        return (request.get("attempt_id") == attempt["attempt_id"]
                and request.get("state") == expected_state
                and result.get("attempt_id") == attempt["attempt_id"]
                and result.get("state") == expected_state)

    def _recover_one(self, connection, owner, stamp, row, final_payload, legacy_gate_proof):
        from .durable_job_contracts import PageOutcomeProjection
        job_id, page = row["job_id"], row["page_number"]
        page_state, attempt_state = row["page_state"], row["attempt_state"]
        attempt_id = row["attempt_id"] or row["page_attempt_id"]
        if attempt_id is None:
            if page_state == "claimed":
                if (row["page_owner_epoch"] is not None
                        and row["page_owner_epoch"] < owner.epoch):
                    changed = connection.execute(
                        "UPDATE durable_job_pages SET state='queued',owner_epoch=NULL,"
                        "revision=revision+1 WHERE job_id=? AND page_number=?"
                        " AND state='claimed' AND attempt_id IS NULL AND revision=?",
                        (job_id, page, row["page_revision"])).rowcount
                    if changed == 1:
                        self._rollup_job(connection, job_id)
                        return "released", None
                    return "quarantine", "recovery_state_conflict"
            return "noop", None
        if attempt_state is None:
            return "quarantine", "recovery_state_conflict"
        if (attempt_id != row["candidate_attempt_id"]
                or row["page_attempt_id"] not in (None, attempt_id)):
            return "quarantine", "recovery_identity_conflict"
        claim_cancel = self._reserve_claim_cancel_revision(connection, row["operation_id"])
        row["claim_cancel_revision"] = claim_cancel
        claim = self._claim_from_recovery_row(row, owner.epoch)
        attempt = connection.execute("SELECT * FROM attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
        if attempt_state == "intent":
            if page_state == "intent":
                if final_payload is not None:
                    return self._quarantine_unknown(connection, row, "recovery_evidence_conflict")
                return self._recover_pre_gate(connection, owner, stamp, row, claim, "intent")
            if page_state == "claimed":
                if final_payload is not None:
                    return self._quarantine_unknown(connection, row, "recovery_evidence_conflict")
                if legacy_gate_proof is not None and legacy_gate_proof(row):
                    return self._recover_pre_gate(connection, owner, stamp, row, claim, "claimed")
                return self._quarantine_unknown(connection, row, "recovery_state_conflict")
            if page_state == "send_committed":
                if final_payload is not None:
                    return self._settle_from_evidence(connection, owner, stamp, row, claim,
                                                      final_payload)
                return self._quarantine_unknown(connection, row, "dispatch_unknown")
            if page_state == "blocked":
                return self._quarantine_unknown(connection, row, "recovery_state_conflict")
            return "quarantine", "recovery_state_conflict"
        if attempt_state == "terminal":
            if page_state in ("completed", "incomplete"):
                if payload is None:
                    # R-10: a success projection without reconstructible final
                    # evidence is downgraded, never trusted.
                    changed = connection.execute(
                        "UPDATE durable_job_pages SET state='failed',safe_code='evidence_missing',"
                        "revision=revision+1 WHERE job_id=? AND page_number=? AND state=?"
                        " AND revision=?", (job_id, page, page_state,
                                            row["page_revision"])).rowcount
                    if changed != 1:
                        return "quarantine", "recovery_state_conflict"
                self._rollup_job(connection, job_id)
                return "repaired", None
            if page_state == "failed":
                self._rollup_job(connection, job_id)
                return "repaired", None
            if page_state == "send_committed":
                return self._repair_legacy_terminal(connection, row, attempt, final_payload)
            if page_state in ("unknown_outcome", "blocked"):
                return "noop", None
            return "quarantine", "recovery_state_conflict"
        if attempt_state == "not_sent":
            if page_state == "failed":
                return "noop", None
            if page_state == "cancelled":
                if row["page_attempt_id"] is None:
                    return self._repair_legacy_cancelled(connection, row, attempt)
                return "noop", None
            if page_state == "claimed":
                return self._repair_legacy_claimed_not_sent(connection, row, attempt,
                                                            final_payload)
            return "quarantine", "recovery_state_conflict"
        if attempt_state == "unknown":
            if page_state == "claimed":
                return self._repair_legacy_unknown(connection, row, attempt)
            if page_state == "send_committed":
                changed = connection.execute(
                    "UPDATE durable_job_pages SET state='unknown_outcome',safe_code='dispatch_unknown',"
                    "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id=?"
                    " AND state='send_committed' AND revision=?",
                    (job_id, page, attempt_id, row["page_revision"])).rowcount
                if changed != 1:
                    return "quarantine", "recovery_state_conflict"
                self._rollup_job(connection, job_id)
                return "repaired", None
            if page_state in ("unknown_outcome", "blocked"):
                return "noop", None
            return "quarantine", "recovery_state_conflict"
        return "quarantine", "recovery_state_conflict"

    def _recover_pre_gate(self, connection, owner, stamp, row, claim, page_state):
        from .durable_job_contracts import PageOutcomeProjection
        attempt_id = row["attempt_id"]
        proof = self._pre_gate_proof(attempt_id, row["run_id"], row["page_number"],
                                     row["attempt_claim_seq"], row["attempt_owner_epoch"],
                                     page_state)
        outcome = Outcome(_settlement_operation_id(attempt_id), attempt_id,
                          row["request_digest"], proof, "not_sent", None)
        result = self._record_job_outcome_tx(
            connection, owner, stamp, outcome, claim,
            PageOutcomeProjection("failed", "not_sent_pre_gate", 0, proof), None)
        if result["job_state"] == "running":
            schedulable = connection.execute(
                "SELECT 1 FROM durable_job_pages WHERE job_id=? AND state IN"
                " ('queued','quota_wait') LIMIT 1", (row["job_id"],)).fetchone()
            if schedulable and self._mark_paused_restart_tx(connection, row["job_id"]):
                return "paused", None
        return "settled", None

    def _settle_from_evidence(self, connection, owner, stamp, row, claim, payload):
        try:
            outcome = Outcome(**payload["outcome"])
            projection = self._evidence_page_projection(payload)
        except (KeyError, TypeError, ValueError):
            return "quarantine", "result_evidence_invalid"
        if (outcome.attempt_id != row["attempt_id"]
                or outcome.state != "terminal"):
            return "quarantine", "result_evidence_identity_mismatch"
        try:
            self._record_job_outcome_tx(connection, owner, stamp, outcome, claim,
                                        projection, row["job_generation"])
        except CoordinationError as exc:
            return "quarantine", exc.code
        return "settled", None

    def _quarantine_unknown(self, connection, row, safe_code):
        attempt_id = row["attempt_id"]
        if row["attempt_state"] == "intent":
            self._block_attempt(connection, attempt_id)
            connection.execute("UPDATE attempts SET state='unknown' WHERE attempt_id=?",
                               (attempt_id,))
        if row["page_attempt_id"] is None:
            changed = connection.execute(
                "UPDATE durable_job_pages SET attempt_id=?,state='blocked',safe_code=?,"
                "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id IS NULL"
                " AND revision=?", (attempt_id, safe_code, row["job_id"],
                                    row["page_number"], row["page_revision"])).rowcount
            if changed != 1:
                return "quarantine", "recovery_state_conflict"
        else:
            connection.execute(
                "UPDATE durable_job_pages SET state='blocked',safe_code=?,revision=revision+1"
                " WHERE job_id=? AND page_number=? AND attempt_id=? AND revision=?",
                (safe_code, row["job_id"], row["page_number"], attempt_id,
                 row["page_revision"]))
        self._rollup_job(connection, row["job_id"])
        return "quarantine", safe_code

    def _block_legacy_page(self, connection, row, safe_code):
        """Projection-only quarantine: attach exact identity and block the page.

        B1 attempt state, owner epoch, evidence digest and receipts are never
        written by this path.
        """
        attempt_id = row["attempt_id"] or row["candidate_attempt_id"]
        if row["page_attempt_id"] is None:
            changed = connection.execute(
                "UPDATE durable_job_pages SET attempt_id=?,state='blocked',safe_code=?,"
                "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id IS NULL"
                " AND revision=?", (attempt_id, safe_code, row["job_id"],
                                    row["page_number"], row["page_revision"])).rowcount
        else:
            changed = connection.execute(
                "UPDATE durable_job_pages SET state='blocked',safe_code=?,revision=revision+1"
                " WHERE job_id=? AND page_number=? AND attempt_id=? AND revision=?",
                (safe_code, row["job_id"], row["page_number"], attempt_id,
                 row["page_revision"])).rowcount
        if changed != 1:
            return "quarantine", "recovery_state_conflict"
        # R-24/R-26/R-28/R-29 quarantine owns the job projection: the job must
        # be blocked, not silently left running/completed.
        connection.execute(
            "UPDATE durable_jobs SET state='blocked',revision=revision+1,updated_at=?,"
            "finished_at=NULL WHERE job_id=?", (_now_iso(), row["job_id"]))
        return "quarantine", safe_code

    def _repair_legacy_terminal(self, connection, row, attempt, payload):
        reserve, outcome_receipt = self._legacy_receipts(connection, attempt)
        if (not self._receipt_matches(reserve, "reserve", attempt, None)
                or not self._receipt_matches(outcome_receipt, "outcome", attempt, "terminal")
                or payload is None):
            return self._block_legacy_page(connection, row, "recovery_receipt_conflict")
        try:
            projection = self._evidence_page_projection(payload)
            saved_outcome = Outcome(**payload["outcome"])
        except (KeyError, TypeError, ValueError):
            return self._block_legacy_page(connection, row, "result_evidence_invalid")
        if (saved_outcome.attempt_id != attempt["attempt_id"]
                or saved_outcome.evidence_digest != attempt["evidence_digest"]
                or saved_outcome.actual_units != attempt["actual_units"]):
            return self._block_legacy_page(connection, row, "historical_outcome_conflict")
        changed = connection.execute(
            "UPDATE durable_job_pages SET state=?,safe_code=?,result_bytes=?,evidence_digest=?,"
            "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id=?"
            " AND state='send_committed' AND revision=?",
            (projection.state, projection.safe_code, projection.result_bytes,
             projection.evidence_digest, row["job_id"], row["page_number"],
             attempt["attempt_id"], row["page_revision"])).rowcount
        if changed != 1:
            return "quarantine", "recovery_state_conflict"
        self._rollup_job(connection, row["job_id"])
        return "repaired", None

    def _repair_legacy_claimed_not_sent(self, connection, row, attempt, payload):
        reserve, outcome_receipt = self._legacy_receipts(connection, attempt)
        if payload is not None:
            # R-24: final evidence contradicts an immutable not_sent.
            return self._block_legacy_page(connection, row, "recovery_evidence_conflict")
        if (not self._receipt_matches(reserve, "reserve", attempt, None)
                or not self._receipt_matches(outcome_receipt, "outcome", attempt, "not_sent")):
            return self._block_legacy_page(connection, row, "recovery_receipt_conflict")
        changed = connection.execute(
            "UPDATE durable_job_pages SET attempt_id=?,state='failed',safe_code='not_sent_pre_gate',"
            "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id IS NULL"
            " AND state='claimed' AND revision=?", (attempt["attempt_id"], row["job_id"],
                                                   row["page_number"], row["page_revision"])).rowcount
        if changed != 1:
            return "quarantine", "recovery_state_conflict"
        self._rollup_job(connection, row["job_id"])
        return "repaired", None

    def _repair_legacy_cancelled(self, connection, row, attempt):
        reserve, outcome_receipt = self._legacy_receipts(connection, attempt)
        if (not self._receipt_matches(reserve, "reserve", attempt, None)
                or not self._receipt_matches(outcome_receipt, "outcome", attempt, "not_sent")):
            return self._block_legacy_page(connection, row, "recovery_receipt_conflict")
        command = connection.execute(
            "SELECT receipt_json FROM durable_job_commands WHERE job_id=? AND kind='cancel'"
            " ORDER BY rowid DESC LIMIT 1", (row["job_id"],)).fetchone()
        if command is None:
            return self._block_legacy_page(connection, row, "recovery_receipt_conflict")
        changed = connection.execute(
            "UPDATE durable_job_pages SET attempt_id=?,revision=revision+1 WHERE job_id=?"
            " AND page_number=? AND attempt_id IS NULL AND state='cancelled' AND revision=?",
            (attempt["attempt_id"], row["job_id"], row["page_number"],
             row["page_revision"])).rowcount
        if changed != 1:
            return "quarantine", "recovery_state_conflict"
        self._rollup_job(connection, row["job_id"])
        return "repaired", None

    def _repair_legacy_unknown(self, connection, row, attempt):
        reserve, _outcome_receipt = self._legacy_receipts(connection, attempt)
        if not self._receipt_matches(reserve, "reserve", attempt, None):
            return "quarantine", "recovery_receipt_conflict"
        changed = connection.execute(
            "UPDATE durable_job_pages SET attempt_id=?,state='blocked',safe_code='recovery_state_conflict',"
            "revision=revision+1 WHERE job_id=? AND page_number=? AND attempt_id IS NULL"
            " AND state='claimed' AND revision=?", (attempt["attempt_id"], row["job_id"],
                                                   row["page_number"], row["page_revision"])).rowcount
        if changed != 1:
            return "quarantine", "recovery_state_conflict"
        self._rollup_job(connection, row["job_id"])
        return "repaired", None

    def recover_unsettled(self, owner, *, evidence, batch_size: int = 50,
                          legacy_gate_proof=None):
        """Classify prior-owner attempts; settle new ones and repair legacy projections."""
        from .durable_job_contracts import RecoveryReport
        integer(batch_size, minimum=1)
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            candidates = [dict(row) for row in connection.execute(
                "SELECT p.job_id,p.run_id,p.page_number,p.state AS page_state,"
                "p.attempt_id AS page_attempt_id,p.candidate_attempt_id,"
                "p.claim_seq AS page_claim_seq,p.owner_epoch AS page_owner_epoch,"
                "p.revision AS page_revision,p.evidence_digest AS page_evidence_digest,"
                "j.tenant_id,j.owner_id,j.document_id,j.state AS job_state,"
                "j.cancel_revision AS job_cancel_revision,j.generation AS job_generation,"
                "a.attempt_id,a.state AS attempt_state,a.attempt_no,"
                "a.claim_seq AS attempt_claim_seq,a.owner_epoch AS attempt_owner_epoch,"
                "a.request_digest,a.operation_id"
                " FROM durable_job_pages p JOIN durable_jobs j USING(job_id)"
                " LEFT JOIN attempts a ON a.run_id=p.run_id AND a.page_number=p.page_number"
                " WHERE j.state IN ('queued','running','cancelling','paused_restart','blocked')"
                " AND (a.attempt_id IS NOT NULL OR p.attempt_id IS NOT NULL OR p.state='claimed')"
                " ORDER BY p.job_id,p.page_number LIMIT ?", (batch_size,))]
            connection.commit()
        cache = {}
        for row in candidates:
            attempt_id = row["attempt_id"] or row["page_attempt_id"]
            if attempt_id is not None and attempt_id not in cache:
                try:
                    cache[attempt_id] = evidence.read(attempt_id)
                except CoordinationError:
                    # Unvalidatable evidence is never proof of success; the
                    # classifier below quarantines a gated page as unknown.
                    cache[attempt_id] = None
        settled, repaired, quarantined, released, paused = [], [], [], [], []
        for row in candidates:
            attempt_id = row["attempt_id"] or row["page_attempt_id"]
            payload = cache.get(attempt_id) if attempt_id is not None else None
            try:
                with self._transaction(owner) as (connection, stamp, _meta):
                    outcome, code = self._recover_one(connection, owner, stamp, row,
                                                      payload, legacy_gate_proof)
            except CoordinationError as exc:
                quarantined.append((row["job_id"], exc.code))
                continue
            key = row["job_id"]
            if outcome == "settled":
                settled.append(key)
            elif outcome == "repaired":
                repaired.append(key)
            elif outcome == "released":
                released.append(key)
            elif outcome == "paused":
                paused.append(key)
            elif outcome == "quarantine":
                quarantined.append((key, code))
        return RecoveryReport(tuple(settled), tuple(repaired), tuple(quarantined),
                              tuple(released), tuple(paused))

    def startup_barrier(self, owner):
        """Return the unsettled prior-epoch identities blocking scheduler admission."""
        with self._transaction(owner) as (connection, _stamp, _meta):
            unauthorized = tuple(row[0] for row in connection.execute(
                "SELECT job_id FROM durable_jobs WHERE state='running'"
                " AND (authorized_owner_epoch IS NULL OR authorized_owner_epoch<?)"
                " ORDER BY job_id", (owner.epoch,)))
            unsettled = tuple(row[0] for row in connection.execute(
                "SELECT a.attempt_id FROM attempts a JOIN durable_job_pages p"
                " ON p.run_id=a.run_id AND p.page_number=a.page_number"
                " JOIN durable_jobs j USING(job_id) WHERE j.state IN"
                " ('queued','running','cancelling','paused_restart','blocked')"
                " AND a.state='intent' AND p.state NOT IN ('failed','cancelled','blocked')"
                " ORDER BY a.attempt_id"))
            stale_claims = tuple(row[0] for row in connection.execute(
                "SELECT p.job_id FROM durable_job_pages p WHERE p.state='claimed'"
                " AND p.attempt_id IS NULL AND p.owner_epoch IS NOT NULL AND p.owner_epoch<?"
                " ORDER BY p.job_id", (owner.epoch,)))
        return {"unauthorized": unauthorized, "unsettled": unsettled,
                "stale_claims": stale_claims}

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
            expected = (self.schema_version, 2 if self.schema_version == 1 else 3,
                        "sealed_history", "explicit_transactional_migration")
            if row != expected:
                raise CoordinationError("schema_contract_mismatch")
            return dict(zip(("schema_version", "reconciliation_min_schema_version",
                             "history_policy", "reconciliation_upgrade"), row))

    def require_reconciliation_schema(self):
        """Neither registered schema grants reconciliation, regardless of numbering."""
        self.schema_contract()
        if "b6_reconciliation" not in self.capabilities():
            raise CoordinationError("reconciliation_requires_schema_v2" if self.schema_version == 1
                                    else "reconciliation_requires_schema_v3")

    def capabilities(self):
        # Validate real storage on every capability check; never trust a caller's version.
        with self._connection():
            return schema_capabilities(self.schema_version)

    def require_capability(self, capability):
        if capability not in self.capabilities():
            raise CoordinationError("capability_not_authorized")
