"""Real process/SQLite fault windows; no external network or paid provider."""
import asyncio
from dataclasses import asdict
import json
import ipaddress
from pathlib import Path
import socket
import sqlite3
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from averon_import.services.ai_first.coordination import CoordinationLedger
from averon_import.services.ai_first.coordination_contracts import CoordinationError
from averon_import.services.ai_first.coordination_maintenance import MaintenanceSession
from averon_import.services.ai_first.coordination_migrations import MigrationRequest, migrate_v1_to_v2
from b3_test_support import SyntheticQuiescence, open_v2_runtime


def emit(value):
    print(json.dumps(value), flush=True)


def pause(window):
    emit({"window": window})
    sys.stdin.readline()


def main():
    def denied(*_args, **_kwargs):
        raise AssertionError("b3_network_forbidden")
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)
        def local_only(sock, address, _original=original):
            if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
                return denied()
            return _original(sock, address)
        setattr(socket.socket, method, local_only)
    mode = sys.argv[1]
    payload = json.loads(sys.stdin.readline())
    root, installation, control = payload["ledger"], payload["installation_id"], payload["control"]
    if mode == "old_reader":
        # Execute the actual pinned pre-B3 module, not a mock of its open check.
        import types
        legacy = types.ModuleType("averon_import.services.ai_first.legacy_reader_test")
        legacy.__file__ = str(Path(__file__).resolve().parents[1] /
                              "averon_import/services/ai_first/coordination.py")
        exec(compile(payload["legacy_source"], legacy.__file__, "exec"), legacy.__dict__)
        try:
            legacy.CoordinationLedger(root, installation_id=installation)
        except CoordinationError as exc:
            emit({"error": exc.code})
            return
        raise AssertionError("old runtime unexpectedly opened schema v2")
    if mode.startswith("raw_"):
        connection = sqlite3.connect(Path(root) / "coordination.sqlite3", isolation_level=None)
        try:
            if mode == "raw_reader":
                connection.execute("BEGIN")
            elif mode == "raw_writer":
                connection.execute("BEGIN IMMEDIATE")
            connection.execute("SELECT * FROM meta").fetchall()
            pause(mode)
        finally:
            connection.close()
        return
    if mode == "client":
        with CoordinationLedger(root, installation_id=installation,
                schema_version=payload.get("version", 1), maintenance_root=control) as ledger:
            # An open IDLE connection is covered by the lifetime guard as well.
            with ledger._connection():
                pause(mode)
        return
    if mode == "b2_evidence":
        from test_ai_first_durable_dispatch import request
        value = open_v2_runtime(payload)
        try:
            original = value.dispatcher.ledger.record_outcome
            def after_evidence(owner, outcome):
                assert value.dispatcher.evidence.path(outcome.attempt_id).is_file()
                emit({"window": "b2_evidence", "posts": len(value.transport.calls)})
                sys.stdin.readline()
                return original(owner, outcome)
            value.dispatcher.ledger.record_outcome = after_evidence
            asyncio.run(value.adapter.extract(request(value)))
        finally:
            value.dispatcher.close()
            value.dispatcher.ledger.close()
        return
    guard = SyntheticQuiescence(Path(root), installation)
    try:
        with MaintenanceSession(control, root, installation, legacy_guard=guard) as session:
            if mode == "maintenance":
                pause(mode)
                return
            original_connect = sqlite3.connect
            class Window(sqlite3.Connection):
                lost = False
                def execute(self, sql, *args, **kwargs):
                    result = super().execute(sql, *args, **kwargs)
                    if mode == "during_ddl" and sql.strip().startswith("CREATE TABLE durable_jobs"):
                        pause(mode)
                    return result
                def commit(self):
                    if self.in_transaction and self.execute("PRAGMA user_version").fetchone()[0] == 2:
                        if mode == "before_commit":
                            pause(mode)
                        if mode == "write_failure":
                            raise sqlite3.OperationalError("synthetic precommit failure")
                        super().commit()
                        if mode == "after_commit":
                            pause(mode)
                        if mode == "lost_receipt" and not self.lost:
                            self.lost = True
                            raise sqlite3.OperationalError("synthetic receipt lost AFTER real commit")
                        return
                    return super().commit()
            def connect(*args, **kwargs):
                kwargs["factory"] = Window
                return original_connect(*args, **kwargs)
            sqlite3.connect = connect
            result = migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(payload["migration_id"], installation))
            emit({"receipt": asdict(result.receipt), "new": result.newly_applied,
                  "recovered": result.commit_receipt_recovered})
    except CoordinationError as exc:
        emit({"error": exc.code})


if __name__ == "__main__":
    main()
