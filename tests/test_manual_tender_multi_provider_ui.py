from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from averon_import.services.manual_tenders.multi_provider_public import PublicProviderOutcome
from averon_import.services.sourcing.providers.contracts import ProviderSearchState


ROOT = Path(__file__).resolve().parents[1]


def test_m4a_ui_uses_lazy_opt_in_without_adding_boot_requests():
    app = (ROOT / "averon_import" / "static" / "app.js").read_text(encoding="utf-8")
    template = (ROOT / "averon_import" / "templates" / "index.html").read_text(encoding="utf-8")
    boot = app.split("async function boot()", 1)[1].split("function updateCloudStatus", 1)[0]
    assert "/api/manual-tenders/sourcing/multi-provider-capabilities" not in boot
    assert 'id="multi-provider-toggle" type="checkbox"' in template
    assert 'id="multi-provider-panel"' in template and 'id="multi-provider-panel" class="multi-provider-panel"' in template

    node = shutil.which("node")
    assert node, "Node.js is required for M4A UI lifecycle regression"
    public_outcomes = [
        PublicProviderOutcome(
            provider_key="etm_ipro", state=state,
            offers_returned_count=0, retained_offer_count=0, failure_category=None,
        ).model_dump(mode="json")
        for state in ProviderSearchState
    ]
    assert {item["state"] for item in public_outcomes} == {
        "success", "empty", "partial_success", "failure", "suppressed", "not_attempted",
    }
    environment = os.environ.copy()
    environment["M4A_PUBLIC_PROVIDER_OUTCOMES"] = json.dumps(public_outcomes)
    result = subprocess.run(
        [node, str(ROOT / "tests" / "js" / "multi_provider_ui_lifecycle.cjs"), str(ROOT / "averon_import" / "static" / "app.js")],
        capture_output=True, text=True, timeout=20, check=False, env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: M4A lazy opt-in" in result.stdout
