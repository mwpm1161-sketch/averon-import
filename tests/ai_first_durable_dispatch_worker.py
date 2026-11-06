"""Independent B2 crash windows; only fake HTTP, no external sockets."""
import ipaddress
import json
from dataclasses import replace
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))


def wait_at(window, runtime):
    print(json.dumps({"window": window, "posts": len(runtime.transport.calls),
                      "epoch": runtime.dispatcher._owner.epoch}), flush=True)
    sys.stdin.readline()


def main():
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)
        def local_only(sock, address, _original=original):
            if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError("worker external socket forbidden")
            return _original(sock, address)
        setattr(socket.socket, method, local_only)
    from test_ai_first_durable_dispatch import open_runtime, run_orchestrator
    from averon_import.services.ai_first.coordination_contracts import CoordinationError
    mode = sys.argv[1]
    payload = json.loads(sys.stdin.readline())
    if mode == "busy":
        import sqlite3
        connection = sqlite3.connect(Path(payload["ledger"]) / "coordination.sqlite3", isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        print(json.dumps({"window": "busy"}), flush=True)
        sys.stdin.readline()
        connection.rollback()
        connection.close()
        return
    try:
        runtime = open_runtime(payload)
    except CoordinationError as exc:
        print(json.dumps({"error": exc.code}), flush=True)
        return
    try:
        if mode == "hold":
            wait_at("owner", runtime)
            return
        if mode == "after_evidence_no_usage":
            from test_ai_first_qwen_vision_adapter import _response
            def without_usage(page, _count, _kwargs):
                response = _response(page)
                body = json.loads(response.body)
                body.pop("usage", None)
                return replace(response, body=json.dumps(body).encode())
            runtime.transport.handler = without_usage
        if mode == "before_intent_commit":
            from averon_import.services.ai_first import coordination as core
            import sqlite3
            connect = core.sqlite3.connect
            class PauseIntentCommit(sqlite3.Connection):
                def commit(self):
                    if self.in_transaction and self.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                        wait_at("before_intent_commit", runtime)
                    return super().commit()
            def paused_connect(*args, **kwargs):
                kwargs["factory"] = PauseIntentCommit
                return connect(*args, **kwargs)
            core.sqlite3.connect = paused_connect
        elif mode == "after_intent_commit":
            original = runtime.dispatcher.prepare
            def after_intent_commit(*args, **kwargs):
                permit = original(*args, **kwargs)
                assert not permit._used
                wait_at("after_intent_commit", runtime)
                return permit
            runtime.dispatcher.prepare = after_intent_commit
        elif mode == "before_post":
            original = runtime.dispatcher.consume
            def before_post(*args, **kwargs):
                original(*args, **kwargs)
                wait_at("before_post", runtime)
            runtime.dispatcher.consume = before_post
        elif mode == "during_http":
            def during_http(page, count, kwargs):
                wait_at("during_http", runtime)
                raise TimeoutError
            runtime.transport.handler = during_http
        elif mode in ("after_evidence", "after_evidence_no_usage"):
            original = runtime.dispatcher.ledger.record_outcome
            def after_evidence(owner, outcome):
                assert runtime.dispatcher.evidence.path(outcome.attempt_id).is_file()
                wait_at(mode, runtime)
                return original(owner, outcome)
            runtime.dispatcher.ledger.record_outcome = after_evidence
        run_orchestrator(runtime)
        print(json.dumps({"done": True, "posts": len(runtime.transport.calls)}), flush=True)
    finally:
        runtime.dispatcher.close()


if __name__ == "__main__":
    main()
