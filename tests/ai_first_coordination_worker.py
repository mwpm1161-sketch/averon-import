"""Real independent process for offline B1 tests. No provider imports or HTTP."""
from __future__ import annotations
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from averon_import.services.ai_first.coordination import CoordinationLedger
from averon_import.services.ai_first.coordination_contracts import Attempt, Binding, CoordinationError, Outcome, Run


def emit(value):
    print(json.dumps(value), flush=True)


def deny_network(*_args, **_kwargs):
    raise AssertionError("b1_network_forbidden")


def main():
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    root, installation, mode = sys.argv[1:4]
    if mode == "sql_lock":
        with sqlite3.connect(Path(root) / "coordination.sqlite3", isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            emit({"locked": True})
            sys.stdin.readline()
            connection.rollback()
        return
    ledger = CoordinationLedger(root, installation_id=installation)
    if mode in ("crash_before_commit", "crash_after_commit"):
        from averon_import.services.ai_first import coordination as core
        data = json.loads(sys.stdin.readline())
        binding = data["attempt"]["binding"]
        binding["quota_scope_ids"] = tuple(binding["quota_scope_ids"])
        data["attempt"]["binding"] = Binding(**binding)
        data["attempt"]["quota_revisions"] = tuple(tuple(v) for v in data["attempt"]["quota_revisions"])
        item = Attempt(**data["attempt"])
        with ledger.dispatcher() as owner:
            run = Run(**data["run"])
            ledger.register_run(owner, run.run_id, run)
            original = core.sqlite3.connect

            class CrashWindow(sqlite3.Connection):
                def commit(self):
                    if mode == "crash_after_commit":
                        super().commit()
                    emit({"window": mode})
                    sys.stdin.readline()
                    if mode == "crash_before_commit":
                        super().commit()

            def connect(*args, **kwargs):
                kwargs["factory"] = CrashWindow
                return original(*args, **kwargs)

            core.sqlite3.connect = connect
            ledger.reserve(owner, item)
        return
    if mode == "clock":
        from dataclasses import asdict
        from averon_import.services.ai_first.coordination import system_clock
        emit(asdict(system_clock()))
        return
    if mode == "hold":
        with ledger.dispatcher() as owner:
            emit({"epoch": owner.epoch})
            for line in sys.stdin:
                command = json.loads(line)
                try:
                    if command["action"] == "reserve":
                        run = Run(**command["run"])
                        ledger.register_run(owner, run.run_id, run)
                        binding = command["attempt"]["binding"]
                        binding["quota_scope_ids"] = tuple(binding["quota_scope_ids"])
                        request = command["attempt"]
                        request["binding"] = Binding(**binding)
                        request["quota_revisions"] = tuple(tuple(v) for v in request["quota_revisions"])
                        result = ledger.reserve(owner, Attempt(**request))
                        emit({"reserved": result.newly_created})
                    elif command["action"] == "renew":
                        owner.renew()
                        emit({"renewed": True})
                    elif command["action"] == "exit":
                        return
                except CoordinationError as exc:
                    emit({"error": exc.code})
        return
    if mode == "race":
        data = json.loads(sys.stdin.readline())
        binding = data["binding"]
        binding["quota_scope_ids"] = tuple(binding["quota_scope_ids"])
        binding = Binding(**binding)
        run = Run(**data["run"])
        successes, errors = 0, []
        for page in range(1, 11):
            for _ in range(300):
                try:
                    owner = ledger.dispatcher()
                    break
                except CoordinationError as exc:
                    if exc.code != "dispatcher_busy":
                        raise
                    time.sleep(0.005)
            else:
                raise RuntimeError("owner_wait_expired")
            with owner:
                try:
                    ledger.register_run(owner, run.run_id, run)
                    request = Attempt(uuid4().hex, uuid4().hex, run.run_id, page, 1, 1, binding,
                                      1, tuple((q, 1) for q in binding.quota_scope_ids), "a" * 64,
                                      data["tokens"], 0, data["cost"])
                    ledger.reserve(owner, request)
                    ledger.record_outcome(owner, Outcome(uuid4().hex, request.attempt_id,
                        request.request_digest, "b" * 64, "terminal", data["cost"]))
                    successes += 1
                except CoordinationError as exc:
                    errors.append(exc.code)
        emit({"successes": successes, "errors": errors})
        return
    raise ValueError("unknown_mode")


if __name__ == "__main__":
    main()
