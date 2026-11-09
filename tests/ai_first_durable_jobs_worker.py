"""Subprocess crash and independent-process worker. Synthetic inputs only."""
import json
import os
from dataclasses import asdict
from pathlib import Path
import sys

_protocol_stdout = sys.stdout
sys.stdout = sys.stderr  # Imported libraries may print startup notices; stdout stays JSON-only.
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[0])]

from averon_import.services.ai_first.coordination import CoordinationLedger
from averon_import.services.ai_first.coordination_contracts import Binding, Run
from averon_import.services.ai_first.durable_execution_store import PrivateExecutionStore
from averon_import.services.ai_first.durable_job_contracts import (
    ExecutionProfile, FinancialSnapshotInput, PageSelection, SubmissionRequest, _verified_document_context,
)
from averon_import.services.ai_first.durable_job_store import DurableJobStore


def emit(value):
    print(json.dumps(value), file=_protocol_stdout, flush=True)


def main():
    value = json.loads(sys.stdin.readline())
    actor_value = value["actor"]
    actor = _verified_document_context(**actor_value)
    request_value = value["request"]
    request = SubmissionRequest(request_value["operation_id"], request_value["document_id"],
        tuple(PageSelection(item["page_number"], tuple(item["crop"]) if item["crop"] else None)
              for item in request_value["pages"]), request_value["user_context"])
    run = Run(**value["financial"]["run"])
    binding = Binding(**{**value["financial"]["binding"],
                         "quota_scope_ids": tuple(value["financial"]["binding"]["quota_scope_ids"])})
    from averon_import.services.ai_first.orchestration import OrchestrationConfig
    config = OrchestrationConfig.model_validate_json(json.dumps(value["financial"]["config"]))
    financial = FinancialSnapshotInput(run, config, binding)
    profile = ExecutionProfile(**value["profile"])
    ledger = CoordinationLedger(value["ledger"], installation_id=value["installation_id"],
        schema_version=2, maintenance_root=value["control"])
    try:
        execution = PrivateExecutionStore(value["execution_root"], value["installation_id"])
        hook = None
        crash_at = value.get("crash_at")
        if crash_at:
            def hook(point):
                if point == crash_at:
                    emit({"point": point})
                    if value.get("wait_for_go"):
                        sys.stdin.readline()
                    elif value.get("crash_mode") == "lost_receipt":
                        raise OSError("synthetic receipt lost after commit")
                    else:
                        os._exit(73)
            execution._fault_hook = hook
        backend = DurableJobStore(ledger, execution, _fault_hook=hook)
        if value.get("queue_limits"):
            from averon_import.services.ai_first import durable_job_store as jobs
            from averon_import.services.ai_first.durable_job_contracts import QueueLimits
            jobs.QUEUE_LIMITS = QueueLimits(**value["queue_limits"])
        if value.get("wait_for_go") and not crash_at:
            emit({"ready": True})
            sys.stdin.readline()
        receipt = backend.submit(actor, request, financial=financial, execution_profile=profile)
        status = backend.get_status(actor, receipt.job_id)
        emit({"receipt": asdict(receipt), "state": status.state, "revision": status.revision})
    except Exception as exc:
        emit({"error": getattr(exc, "code", "worker_failed")})
        raise SystemExit(2)
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
