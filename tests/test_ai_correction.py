from __future__ import annotations

import json

from averon_import.ai.provider import AiProviderError
from averon_import.ai.service import AiCorrectionService


class StubProvider:
    key = "local"
    label = "Stub local"
    model = "stub-qwen"
    configured = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class FailingProvider(StubProvider):
    def complete(self, messages):
        raise AiProviderError("локальная модель недоступна")


def sample_row() -> dict:
    return {
        "id": "row-1",
        "position": "1",
        "name": "Вентилятор радиалъный",
        "type_mark": "BP 280-46 З,15",
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
        "confidences": {"name": 71.0, "type_mark": 58.0, "quantity": 95.0},
    }


def test_ai_correction_preserves_original_and_does_not_fill_blanks():
    provider = StubProvider(
        {
            "rows": [
                {
                    "id": "row-1",
                    "values": {
                        "name": "Вентилятор радиальный",
                        "type_mark": "ВР 280-46 3,15",
                        "code": "ПРИДУМАННЫЙ-КОД",
                    },
                    "confidence": 0.97,
                    "reason": "очевидные OCR-ошибки",
                }
            ]
        }
    )
    service = AiCorrectionService(providers={"local": provider})
    result = service.correct_result({"rows": [sample_row()], "errors": []}, "local")
    row = result["rows"][0]

    assert row["name"] == "Вентилятор радиальный"
    assert row["type_mark"] == "ВР 280-46 3,15"
    assert row["code"] == ""
    assert row["ai_original"]["name"] == "Вентилятор радиалъный"
    assert row["ai_provider"] == "local"
    assert row["ai_confidence"] == 97.0
    assert row["status"] == "review"
    assert result["ai"]["status"] == "completed"
    assert result["ai"]["changed_rows"] == 1
    assert result["ai"]["changed_cells"] == 2


def test_ai_failure_keeps_ocr_result_available():
    provider = FailingProvider({})
    service = AiCorrectionService(providers={"local": provider})
    original = sample_row()
    result = service.correct_result({"rows": [original.copy()], "errors": []}, "local")

    assert result["rows"][0]["name"] == original["name"]
    assert result["rows"][0]["type_mark"] == original["type_mark"]
    assert result["ai"]["status"] == "failed"
    assert result["ai"]["warnings"] == ["локальная модель недоступна"]


def test_verified_rows_are_not_sent_to_ai():
    provider = StubProvider({"rows": []})
    service = AiCorrectionService(providers={"local": provider})
    row = sample_row()
    row["status"] = "verified"

    result = service.correct_result({"rows": [row], "errors": []}, "local")

    assert provider.calls == 0
    assert result["ai"]["status"] == "skipped"
