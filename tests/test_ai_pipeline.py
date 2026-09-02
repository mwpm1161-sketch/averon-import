from __future__ import annotations

import json

from averon_import.ai.integration import SmartAIIntegration
from averon_import.ai.service import AiCorrectionService


class StubProvider:
    key = "local"
    label = "Stub local"
    model = "stub-qwen"
    configured = True

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {"rows": []}
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def base_row(**overrides) -> dict:
    row = {
        "id": "row-1",
        "position": "1",
        "name": "Вентилятор радиалъный",
        "type_mark": "ВР 280-46",
        "code": "",
        "manufacturer": "",
        "unit": "шт.",
        "quantity": "1",
        "mass": "",
        "note": "",
        "section": "Вентиляция",
        "system": "П1",
        "row_type": "item",
        "status": "recognized",
        "confidence": 72.0,
        "confidences": {"name": 72.0, "quantity": 95.0},
    }
    row.update(overrides)
    return row


def test_high_confidence_cells_do_not_reach_ai():
    provider = StubProvider()
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    result = {
        "rows": [
            base_row(confidence=98.5, confidences={"name": 98.5, "quantity": 99.0})
        ],
        "errors": [],
    }

    processed = integration.process(result, "local")

    assert provider.calls == 0
    assert processed.stats.skipped_rows == 1
    assert processed.stats.skipped_confident == 1
    assert processed.stats.ai_rows == 0
    assert processed.result["ai"]["status"] == "skipped"
    stats_block = processed.result["ai_pipeline_stats"]
    assert stats_block["total_rows"] == 1
    assert stats_block["skipped_confident"] == 1
    assert stats_block["ai_rows"] == 0


def test_single_weak_cell_sends_row_to_ai():
    provider = StubProvider()
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    row = base_row(confidence=93.0, confidences={"name": 96.0, "quantity": 41.0})
    result = {"rows": [row], "errors": []}

    processed = integration.process(result, "local")

    assert provider.calls == 1
    assert processed.stats.ai_rows == 1
    assert processed.stats.skipped_rows == 0


def test_rules_fix_safe_ocr_errors_without_ai():
    provider = StubProvider()
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    row = base_row(type_mark="BP 2З0-46", confidence=40.0)
    result = {"rows": [row], "errors": []}

    processed = integration.process(result, "local")

    assert row["type_mark"] == "ВР 230-46"
    assert provider.calls == 0
    assert processed.stats.rule_fixed_rows == 1
    assert processed.stats.skipped_rule_fixed == 1
    assert processed.result["ai_pipeline_stats"]["rule_fixed_rows"] == 1


def test_missing_cell_confidence_falls_back_to_row_confidence():
    provider = StubProvider()
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    row = base_row(confidence=91.0, confidences={})
    result = {"rows": [row], "errors": []}

    processed = integration.process(result, "local")

    assert provider.calls == 0
    assert processed.stats.skipped_confident == 1


def test_row_without_any_confidence_goes_to_ai():
    provider = StubProvider()
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    row = base_row()
    row.pop("confidence")
    row.pop("confidences")

    processed = integration.process({"rows": [row], "errors": []}, "local")

    assert provider.calls == 1
    assert processed.stats.ai_rows == 1


def test_env_min_confidence_changes_routing(monkeypatch):
    provider = StubProvider()
    service = AiCorrectionService(providers={"local": provider})
    row = base_row()

    processed_default = SmartAIIntegration(service=service).process(
        {"rows": [dict(row)], "errors": []}, "local"
    )
    assert processed_default.stats.ai_rows == 1

    monkeypatch.setenv("AVERON_AI_MIN_CONFIDENCE", "0.5")
    processed_lowered = SmartAIIntegration(service=service).process(
        {"rows": [dict(row)], "errors": []}, "local"
    )
    assert processed_lowered.stats.ai_rows == 0
    assert processed_lowered.stats.skipped_confident == 1


def test_ai_cannot_change_numbers():
    provider = StubProvider(
        {
            "rows": [
                {
                    "id": "row-1",
                    "values": {
                        "name": "Вентилятор радиальный",
                        "quantity": "10",
                        "type_mark": "ВР 999-46",
                    },
                    "confidence": 0.97,
                    "reason": "попытка изменить числа",
                }
            ]
        }
    )
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    row = base_row()
    result = {"rows": [row], "errors": []}

    processed = integration.process(result, "local")

    assert row["name"] == "Вентилятор радиальный"
    assert row["quantity"] == "1"
    assert row["type_mark"] == "ВР 280-46"
    assert row["status"] == "review"
    assert row["ai_original"] == {"name": "Вентилятор радиалъный"}
    assert processed.result["ai"]["changed_cells"] == 1


def test_pipeline_result_stays_compatible_with_frontend():
    provider = StubProvider(
        {
            "rows": [
                {
                    "id": "row-1",
                    "values": {"name": "Вентилятор радиальный"},
                    "confidence": 0.97,
                    "reason": "очевидная OCR-ошибка",
                }
            ]
        }
    )
    integration = SmartAIIntegration(service=AiCorrectionService(providers={"local": provider}))
    result = {"rows": [base_row()], "errors": [], "summary": {"total": 1}}

    processed = integration.process(result, "local")

    assert processed.result["summary"] == {"total": 1}
    assert processed.result["errors"] == []
    assert processed.result["ai"]["enabled"] is True
    assert processed.result["ai"]["provider"] == "local"
    assert processed.result["ai"]["provider_label"] == "Stub local"
    assert processed.result["ai"]["status"] == "completed"
    assert processed.result["ai_pipeline_stats"]["ai_calls"] == 1
    assert processed.stats.public() == processed.result["ai_pipeline_stats"]
