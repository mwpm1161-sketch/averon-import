from __future__ import annotations

import json

import pytest

from averon_import.ai.models import AiReviewSuggestion
from averon_import.ai.ollama import OllamaProvider
from averon_import.ai.service import AiService, AiUnavailableError


class FakeProvider:
    def health(self):
        return {"available": True, "status": "ready", "provider": "fake", "model": "fake-1"}

    def review_row(self, row, *, image_bytes=None):
        return AiReviewSuggestion(
            fields={"name": "Исправленное наименование"},
            uncertain_fields=["quantity"],
            notes="Проверить количество",
            provider="fake",
            model="fake-1",
        )


def test_ai_disabled_is_fail_safe():
    service = AiService(enabled=False)
    assert service.health()["status"] == "disabled"
    with pytest.raises(AiUnavailableError):
        service.review_row({"name": "Клапан"})


def test_ai_service_returns_suggestion_without_mutating_input():
    row = {"name": "Клапан", "quantity": "1"}
    original = dict(row)
    suggestion = AiService(FakeProvider(), enabled=True).review_row(row)
    assert row == original
    assert suggestion.fields["name"] == "Исправленное наименование"
    assert suggestion.uncertain_fields == ["quantity"]


def test_ollama_health_detects_requested_model(monkeypatch):
    provider = OllamaProvider("qwen3-vl:8b")
    monkeypatch.setattr(
        provider,
        "_json_request",
        lambda path, payload=None: {"models": [{"name": "qwen3-vl:8b"}]},
    )
    health = provider.health()
    assert health["available"] is True
    assert health["status"] == "ready"


def test_ollama_review_filters_unknown_fields(monkeypatch):
    provider = OllamaProvider("qwen3-vl:8b")
    payload = {
        "fields": {"name": "Клапан DN50", "quantity": None, "invented": "bad"},
        "uncertain_fields": ["quantity", "invented"],
        "notes": "Количество не читается",
    }
    monkeypatch.setattr(
        provider,
        "_json_request",
        lambda path, request_payload=None: {
            "message": {"content": json.dumps(payload, ensure_ascii=False)}
        },
    )
    suggestion = provider.review_row(
        {"name": "Клапан", "ocr_evidence": {}, "confidence": 80, "critical_confidence": 40}
    )
    assert suggestion.fields == {"name": "Клапан DN50", "quantity": None}
    assert suggestion.uncertain_fields == ["quantity"]
    assert suggestion.provider == "ollama"
