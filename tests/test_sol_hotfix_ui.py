from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_predeploy_sol_hotfix_javascript_lifecycle_regressions():
    script = ROOT / "tests" / "js" / "sol_hotfix_lifecycle.cjs"
    completed = subprocess.run(
        ["node", str(script), str(ROOT / "averon_import" / "static" / "app.js")],
        capture_output=True,
        check=True,
        text=True,
    )
    assert "PASS: SOL F1/F2/F3 lifecycle regressions" in completed.stdout
