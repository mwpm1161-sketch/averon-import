from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from averon_import.ai.router import AIRouter
from averon_import.ai.rules import apply_safe_rules
from averon_import.ai.service import AiCorrectionService
from averon_import.ai.stats import AIPipelineStats


@dataclass
class AIPipelineResult:
    result: dict
    stats: AIPipelineStats


class AIPipeline:
    """Conservative OCR post-processing pipeline.

    Order is intentional:
    1. deterministic rules first;
    2. route only uncertain rows to AI;
    3. keep all AI edits behind existing review flow.
    """

    def __init__(
        self,
        ai_service: AiCorrectionService,
        router: AIRouter | None = None,
    ) -> None:
        self.ai_service = ai_service
        self.router = router or AIRouter()

    def run(self, result: dict, provider_key: str) -> AIPipelineResult:
        stats = AIPipelineStats()
        rows = result.get("rows", [])
        stats.total_rows = len(rows)

        ai_candidates: list[dict] = []
        for row in rows:
            before = dict(row)
            for key, value in list(row.items()):
                if isinstance(value, str):
                    row[key] = apply_safe_rules(value)
            if before != row:
                stats.rule_fixed_rows += 1

            decision = self.router.decide(
                row.get("confidence"),
                has_rule_candidate=before != row,
            )
            if decision.action == "ai":
                ai_candidates.append(row)
            else:
                stats.skipped_rows += 1

        if ai_candidates:
            partial = {"rows": ai_candidates, "errors": []}
            corrected = self.ai_service.correct_result(partial, provider_key)
            stats.ai_rows = len(ai_candidates)
            stats.ai_calls = corrected.get("ai", {}).get("batches", 0)
            result["ai"] = corrected.get("ai", {})

        result["ai_pipeline_stats"] = stats.as_dict()
        return AIPipelineResult(result=result, stats=stats)
