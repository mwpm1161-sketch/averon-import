"""Synthetic-only host guard and v2 B2 composition. Never production tooling."""
from pathlib import Path

from averon_import.services.ai_first.coordination_contracts import CoordinationError, fingerprint
from averon_import.services.ai_first.coordination_maintenance import LegacyQuiescenceGuard


class SyntheticQuiescence(LegacyQuiescenceGuard):
    def __init__(self, root, installation, *, processes=()):
        self.root, self.installation = Path(root), installation
        self.processes = tuple(processes)

    def verify(self, ledger_root, installation_id):
        if (Path(ledger_root) != self.root or installation_id != self.installation
                or any(process.poll() is None for process in self.processes)):
            raise CoordinationError("legacy_quiescence_unverified")
        return fingerprint(["SYNTHETIC FIXTURE ONLY; NOT VPS PROOF", str(self.root), self.installation])


def open_v2_runtime(payload):
    import json
    from types import SimpleNamespace
    from averon_import.services.ai_first.coordination_contracts import Binding, Run
    from averon_import.services.ai_first.durable_dispatch import DispatchBinding, DurableDispatcher
    from averon_import.services.ai_first.durable_qwen_adapter import DurableQwenVisionAdapter
    from averon_import.services.ai_first.orchestration import OrchestrationConfig
    from averon_import.services.ai_first.qwen_vision_adapter import QwenVisionQuotaRegistry
    from averon_import.services.ocr.qwen_vision_client import QwenVisionClient, QwenVisionConfig, VisionLimits
    from test_ai_first_qwen_vision_adapter import FakeTransport
    config = OrchestrationConfig.model_validate_json(json.dumps(payload["config"]))
    binding = Binding(**{**payload["binding"], "quota_scope_ids": tuple(payload["binding"]["quota_scope_ids"])})
    run = Run(**payload["run"])
    wire = QwenVisionConfig("https://provider.invalid/v1", "gpt://synthetic-project/qwen-test",
        "synthetic-b2-credential", "synthetic-project",
        limits=VisionLimits(max_output_tokens=100, max_pages=100, max_requests=100, dpi=72))
    dispatcher = DurableDispatcher(payload["ledger"], installation_id=payload["installation_id"],
        evidence_root=payload["evidence"], enabled=True, schema_version=2, maintenance_root=payload["control"])
    try:
        transport = FakeTransport()
        client = QwenVisionClient(wire, transport=transport, dispatch_boundary=dispatcher)
        grant = DispatchBinding(run, binding, 1, tuple((q, 1) for q in binding.quota_scope_ids), config.pricing)
        adapter = DurableQwenVisionAdapter(client, dispatcher=dispatcher, dispatch_binding=grant,
            quota_registry=QwenVisionQuotaRegistry(), requests_per_minute=config.requests_per_minute,
            tokens_per_minute=config.tokens_per_minute, input_token_upper_bound=100,
            pricing_profile_id="synthetic-b2")
        dispatcher.register(client)
        return SimpleNamespace(dispatcher=dispatcher, adapter=adapter, client=client,
            transport=transport, config=config, run=run, payload=payload, pdf=Path(payload["pdf"]))
    except BaseException:
        dispatcher.close()
        dispatcher.ledger.close()
        raise
