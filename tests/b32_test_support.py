"""Synthetic B3.2 fixtures built from the real B1/B2 coordination code."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

from averon_import.services.ai_first.coordination import CoordinationLedger
from averon_import.services.ai_first.coordination_contracts import Run, fingerprint
from averon_import.services.ai_first.coordination_maintenance import MaintenanceLayout, MaintenanceSession
from averon_import.services.ai_first.coordination_migrations import MigrationRequest, migrate_v1_to_v2
from averon_import.services.ai_first.durable_execution_store import PrivateExecutionStore
from averon_import.services.ai_first.durable_job_contracts import (
    ExecutionProfile, FinancialSnapshotInput, PageSelection, SubmissionRequest, _verified_document_context,
)
from averon_import.services.ai_first.durable_job_store import DurableJobStore
from averon_import.services.ai_first.private_acl import secure_private_path
from b3_test_support import SyntheticQuiescence
from test_ai_first_durable_dispatch import setup_runtime

WORKER = Path(__file__).with_name("ai_first_durable_jobs_worker.py")


def make_case(root: Path, *, pages=1, owner="owner"):
    root.mkdir(parents=True, exist_ok=True)
    runtime = setup_runtime(root, pages=pages)
    runtime.dispatcher.close()
    runtime.dispatcher.ledger.close()
    control = root / "control"
    MaintenanceLayout.provision(control, root / "ledger", runtime.payload["installation_id"])
    with MaintenanceSession(control, root / "ledger", runtime.payload["installation_id"],
            legacy_guard=SyntheticQuiescence(root / "ledger", runtime.payload["installation_id"])) as session:
        migrate_v1_to_v2(session, MigrationRequest.v1_to_v2(uuid4().hex, runtime.payload["installation_id"]))
    ledger = CoordinationLedger(root / "ledger", installation_id=runtime.payload["installation_id"],
                                 schema_version=2, maintenance_root=control)
    secure_private_path(runtime.pdf, directory=False)
    stat = runtime.pdf.stat()
    document = _verified_document_context(tenant_id=runtime.run.tenant_id, owner_id=owner,
        document_id=runtime.run.document_id, generation=runtime.run.generation,
        source_digest=runtime.run.source_digest, source_size_bytes=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns, page_count=pages, source_path=runtime.pdf,
        authorization_digest=fingerprint(["synthetic-server-authorization", owner, runtime.run.document_id]))
    execution_store = PrivateExecutionStore.provision(root / "private-document-storage",
                                                       runtime.payload["installation_id"])
    backend = DurableJobStore(ledger, execution_store)
    financial = FinancialSnapshotInput(runtime.run, runtime.config, runtime.adapter.dispatch_binding.binding)
    profile = ExecutionProfile("qwen", "qwen-vl-synthetic", "fixture-v1", "ocr-schema-v1",
                               "prompt-v1", "Synthetic OCR prompt: preserve page text exactly.")
    request = SubmissionRequest(uuid4().hex, runtime.run.document_id,
                                tuple(PageSelection(page) for page in range(1, pages + 1)),
                                "Synthetic document context only.")
    return SimpleNamespace(root=root, runtime=runtime, ledger=ledger, control=control,
        execution_store=execution_store, backend=backend, actor=document, financial=financial,
        profile=profile, request=request, installation_id=runtime.payload["installation_id"], pdf=runtime.pdf)


def close_case(case):
    case.ledger.close()


def new_generation(case, *, document_id=None, owner=None, source_path=None):
    """Register a second legitimate B1 run while retaining immutable v1 facts."""
    from averon_import.services.ai_first.durable_qwen_adapter import configuration_digest
    from averon_import.services.ai_first.coordination_contracts import uuid_hex
    owner = owner or case.actor.owner_id
    document_id = document_id or uuid4().hex
    old = case.financial
    binding = old.binding
    if binding.revision == 1 and document_id == case.actor.document_id:
        # Credential rotation is a new immutable metadata revision; the opaque
        # tag is synthetic and intentionally cannot be used as a credential.
        binding = replace(binding, revision=2, credential_generation="credential_generation_2",
                          credential_tag=hashlib.sha256(b"synthetic credential tag v2").hexdigest())
    run = Run(uuid4().hex, old.run.tenant_id, document_id, 2, old.run.source_digest,
        configuration_digest(old.config, binding), old.run.budget_units, old.run.request_cap)
    with case.ledger.dispatcher() as dispatcher:
        if binding.revision > 1:
            case.ledger.put_binding(dispatcher, uuid4().hex, binding)
            case.ledger.revise_billing(dispatcher, uuid4().hex, binding.billing_scope_id, 1,
                case.ledger.snapshot()["billing"][binding.billing_scope_id]["ceiling_units"] + 10_000)
            for scope in binding.quota_scope_ids:
                with case.ledger._connection() as connection:
                    quota = connection.execute("SELECT revision,requests,tokens,concurrency,window_ms"
                        " FROM quota_scopes WHERE scope_id=?", (scope,)).fetchone()
                case.ledger.revise_quota(dispatcher, uuid4().hex, scope, quota[0],
                    requests=quota[0] + 10, tokens=quota[2] + 10_000,
                    concurrency=quota[3], window_ms=quota[4])
        case.ledger.register_run(dispatcher, uuid4().hex, run)
    path = Path(source_path) if source_path else case.pdf
    stat = path.stat()
    actor = _verified_document_context(tenant_id=run.tenant_id, owner_id=owner, document_id=document_id,
        generation=run.generation, source_digest=run.source_digest, source_size_bytes=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns, page_count=case.actor.page_count, source_path=path,
        authorization_digest=fingerprint(["synthetic-server-authorization", owner, document_id, run.generation]))
    case.current_financial = FinancialSnapshotInput(run, old.config, binding)
    return actor, run


def payload(case, *, request=None, actor=None, financial=None, profile=None, operation=None,
            queue_limits=None, crash_at=None, crash_mode=None, wait_for_go=False):
    request = request or case.request
    actor = actor or case.actor
    financial = financial or case.financial
    profile = profile or case.profile
    return {"ledger": str(case.root / "ledger"), "installation_id": case.installation_id,
        "control": str(case.control), "execution_root": str(case.execution_store.root),
        "actor": {"tenant_id": actor.tenant_id, "owner_id": actor.owner_id,
            "document_id": actor.document_id, "generation": actor.generation,
            "source_digest": actor.source_digest, "source_size_bytes": actor.source_size_bytes,
            "source_mtime_ns": actor.source_mtime_ns, "page_count": actor.page_count,
            "source_path": str(actor.source_path), "authorization_digest": actor.authorization_digest},
        "request": {"operation_id": operation or request.operation_id, "document_id": request.document_id,
            "pages": [asdict(item) for item in request.pages], "user_context": request.user_context},
        "financial": {"run": asdict(financial.run), "binding": asdict(financial.binding),
            "config": financial.config.model_dump(mode="json")},
        "profile": asdict(profile), "queue_limits": asdict(queue_limits) if queue_limits else None,
        "crash_at": crash_at, "crash_mode": crash_mode, "wait_for_go": wait_for_go}


def start_worker(payload_value):
    process = subprocess.Popen([sys.executable, str(WORKER)], cwd=WORKER.parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1)
    process.stdin.write(json.dumps(payload_value) + "\n")
    process.stdin.flush()
    return process


def receive(process, timeout=20):
    from queue import Empty, Queue
    import threading
    result = Queue()
    thread = threading.Thread(target=lambda: result.put(process.stdout.readline()), daemon=True)
    thread.start()
    try:
        line = result.get(timeout=timeout)
    except Empty:
        raise AssertionError("worker_message_timeout")
    if not line:
        raise AssertionError(process.stderr.read())
    return json.loads(line)


def finish_worker(process, *, go=True, timeout=30):
    if go:
        process.stdin.write("GO\n")
        process.stdin.flush()
    process.stdin.close()
    code = process.wait(timeout=timeout)
    stderr = process.stderr.read()
    return code, stderr

