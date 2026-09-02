from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from averon_import.ai.router import AIRouter
from averon_import.ai.rules import apply_safe_rules
from averon_import.ai.service import AiCorrectionService, EDITABLE_FIELDS, ProgressCallback
from averon_import.ai.settings import AIPipelineSettings
from averon_import.ai.stats import AIPipelineStats


@dataclass
class AIPipelineResult:
    result: dict
    stats: AIPipelineStats


class AIPipeline:
    """Conservative OCR post-processing pipeline.

    Order is intentional:
    1. deterministic rules first;
    2. route only uncertain rows to AI, judged by the weakest OCR cell;
    3. keep all AI edits behind existing review flow.
    """

    def __init__(
        self,
        ai_service: AiCorrectionService,
        router: AIRouter | None = None,
        settings: AIPipelineSettings | None = None,
    ) -> None:
        self.ai_service = ai_service
        self.settings = settings or AIPipelineSettings()
        self.router = router or AIRouter(min_confidence=self.settings.min_confidence)

    def run(
        self,
        result: dict,
        provider_key: str,
        progress: ProgressCallback | None = None,
    ) -> AIPipelineResult:
        stats = AIPipelineStats()
        rows = result.get("rows", [])
        stats.total_rows = len(rows)

        if not self.settings.enabled:
            result["ai_pipeline_stats"] = stats.public()
            return AIPipelineResult(result=result, stats=stats)

        ai_candidates: list[dict] = []
        for row in rows:
            rule_fixed = False
            if self.settings.rules_enabled:
                before = dict(row)
                for key in EDITABLE_FIELDS:
                    value = row.get(key)
                    if isinstance(value, str):
                        row[key] = apply_safe_rules(value)
                rule_fixed = before != row
            if rule_fixed:
                stats.rule_fixed_rows += 1

            decision = self.router.decide(
                _effective_confidence(row),
                has_rule_candidate=rule_fixed,
            )
            if decision.action == "ai":
                ai_candidates.append(row)
            else:
                stats.skipped_rows += 1
                if decision.action == "rule":
                    stats.skipped_rule_fixed += 1
                else:
                    stats.skipped_confident += 1

        partial = {"rows": ai_candidates, "errors": []}
        corrected = self.ai_service.correct_result(partial, provider_key, progress)
        stats.ai_rows = len(ai_candidates)
        stats.ai_calls = corrected.get("ai", {}).get("batches", 0)
        result["ai"] = corrected.get("ai", {})

        result["ai_pipeline_stats"] = stats.public()
        return AIPipelineResult(result=result, stats=stats)


def _effective_confidence(row: dict) -> float | None:
    """Weakest OCR cell on a 0-1 scale; falls back to the row average."""
    confidences = row.get("confidences")
    cell_values: list[float] = []
    if isinstance(confidences, dict):
        for key, value in confidences.items():
            if not str(row.get(key, "")).strip():
                continue
            normalized = _normalized_confidence(value)
            if normalized is not None:
                cell_values.append(normalized)
    if cell_values:
        return min(cell_values)
    return _normalized_confidence(row.get("confidence"))


def _normalized_confidence(value: Any) -> float | None:
    """RecognitionService stores confidence on a 0-100 scale; router expects 0-1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value > 1:
        return value / 100
    return float(value)


__all__ = ["AIPipeline", "AIPipelineResult"]
