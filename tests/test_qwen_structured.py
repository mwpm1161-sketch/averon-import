from __future__ import annotations

import json

import pytest

from averon_import.ai.config import ProviderSettings
from averon_import.ai.provider import AiProviderError, OpenAICompatibleProvider
from averon_import.services.sourcing.product_understanding import (
    SourcingAIService,
    build_fallback_intent,
)


class CapturingQwenProvider:
    configured = True

    def __init__(self, response, *, model="gpt://folder/qwen/test"):
        self.response = response
        self.model = model
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeAITransport:
    def __init__(self, provider):
        self.provider = provider

    def ensure_provider(self, key):
        assert key == "yandex"
        return self.provider

    def public_config(self):
        return {
            "providers": {
                "yandex": {
                    "configured": bool(self.provider.configured),
                    "model": self.provider.model,
                }
            }
        }


def source_row(**updates):
    row = {
        "id": "qwen-structured-1",
        "source_text": "Клапан обратный фланцевый Ду50 Ру16",
        "name": "Клапан обратный фланцевый Ду50 Ру16",
        "type_mark": "",
        "code": "",
        "manufacturer": "",
        "quantity": "2",
        "unit": "шт.",
        "note": "",
    }
    row.update(updates)
    return row


def product_json(**updates):
    payload = {
        "product_class": "арматура",
        "normalized_name": "Клапан обратный фланцевый",
        "manufacturer": "",
        "brand": "",
        "model": "",
        "article": "",
        "attributes": {"diameter": 50, "pressure": 16},
        "required_attributes": {"diameter": 50, "pressure": 16},
        "preferred_attributes": {},
        "search_queries": ["клапан обратный фланцевый Ду50 Ру16"],
        "evidence": {"normalized_name": "Клапан обратный фланцевый"},
        "uncertainties": [],
    }
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False)


def run_understanding(response, row=None):
    provider = CapturingQwenProvider(response)
    service = SourcingAIService(FakeAITransport(provider))
    row = row or source_row()
    result = service.understand_with_audit(row, build_fallback_intent(row))
    return result, provider, service


def test_product_understanding_uses_structured_schema_and_disables_reasoning():
    result, provider, _ = run_understanding(product_json())

    assert result.mode == "qwen"
    options = provider.calls[0]["kwargs"]
    assert options["reasoning_effort"] == "none"
    response_format = options["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert "price" not in schema["properties"]
    assert "url" not in schema["properties"]


def test_schema_rejection_uses_documented_json_object_compatibility_fallback():
    class SchemaRejectingProvider(CapturingQwenProvider):
        def complete(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            if kwargs["response_format"]["type"] == "json_schema":
                raise AiProviderError("HTTP 400", category="response_error", status_code=400)
            return product_json()

    provider = SchemaRejectingProvider("")
    service = SourcingAIService(FakeAITransport(provider))
    row = source_row()
    result = service.understand_with_audit(row, build_fallback_intent(row))

    assert result.mode == "qwen"
    assert [call["kwargs"]["response_format"]["type"] for call in provider.calls] == [
        "json_schema", "json_object"
    ]
    assert all(call["kwargs"]["reasoning_effort"] == "none" for call in provider.calls)


def test_valid_structured_response_produces_audited_qwen_result():
    result, provider, _ = run_understanding(product_json())

    assert len(provider.calls) == 1
    assert result.ai_proposal is not None
    assert result.resolved_intent.attributes["diameter"] == 50
    assert result.resolved_intent.attributes["pressure"] == 16


def test_malformed_structured_response_uses_safe_fallback():
    result, _, service = run_understanding("{broken")

    assert result.mode == "fallback"
    assert result.ai_proposal is None
    assert result.warnings and "fallback" in result.warnings[0]
    assert service.public_config()["status"] == "invalid_json"


@pytest.mark.parametrize(
    "error",
    [
        AiProviderError("timed out", category="timeout"),
        AiProviderError("HTTP 503", category="response_error", status_code=503),
    ],
)
def test_timeout_and_provider_error_use_safe_fallback(error):
    result, _, service = run_understanding(error)

    assert result.mode == "fallback"
    assert result.ai_proposal is None
    assert result.warnings and "fallback" in result.warnings[0]
    assert "503" not in result.warnings[0]
    assert "timed out" not in result.warnings[0]
    assert service.public_config()["status"] in {"timeout", "response_error"}


def test_commercial_field_still_fails_closed_after_structured_transport():
    result, _, _ = run_understanding(product_json(price=10, currency="RUB"))

    assert result.mode == "fallback"
    assert result.ai_proposal is None
    assert result.resolved_intent == build_fallback_intent(source_row())


def test_source_owned_fields_remain_unchanged_after_structured_response():
    row = source_row(quantity="7", unit="компл.", manufacturer="NOBO", type_mark="NFK4N 07")
    result, _, _ = run_understanding(
        product_json(
            source_row_id="changed",
            source_text="changed source",
            quantity="99",
            unit="шт.",
            manufacturer="Other",
            model="invented",
        ),
        row,
    )

    resolved = result.resolved_intent
    assert resolved.source_row_id == row["id"]
    assert resolved.source_text == row["source_text"]
    assert resolved.quantity == row["quantity"]
    assert resolved.unit == row["unit"]
    assert resolved.manufacturer == row["manufacturer"]
    assert resolved.model == row["type_mark"]


class FakeHTTPResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload

    def getcode(self):
        return self.status


def test_default_provider_call_has_no_structured_options_and_diagnostics_are_sanitized(monkeypatch):
    settings = ProviderSettings(
        key="yandex",
        label="Yandex",
        base_url="https://ai.example/v1",
        model="gpt://folder/qwen/test",
        api_key="unit-test-api-key-value",
        auth_schemes=("Api-Key",),
    )
    provider = OpenAICompatibleProvider(
        settings,
        timeout_seconds=5,
        temperature=0,
        max_tokens=256,
    )
    request_body = {}

    def fake_urlopen(request, timeout):
        request_body.update(json.loads(request.data.decode("utf-8")))
        return FakeHTTPResponse({
            "choices": [{
                "message": {"content": "{}", "reasoning_content": "hidden"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert provider.complete([{"role": "user", "content": "ping"}]) == "{}"
    assert "response_format" not in request_body
    assert "reasoning_effort" not in request_body
    diagnostics = json.dumps(provider.last_call_diagnostics, ensure_ascii=False)
    assert "unit-test-api-key-value" not in diagnostics
    assert "Authorization" not in diagnostics
    assert provider.last_call_diagnostics["http_status"] == 200
    assert provider.last_call_diagnostics["finish_reason"] == "stop"
    assert provider.last_call_diagnostics["message_content_type"] == "str"
    assert provider.last_call_diagnostics["reasoning_fields"] == ["reasoning_content"]


def test_provider_forwards_supported_structured_options(monkeypatch):
    settings = ProviderSettings(
        key="yandex",
        label="Yandex",
        base_url="https://ai.example/v1",
        model="gpt://folder/qwen/test",
        api_key="unit-test-api-key-value",
        auth_schemes=("Api-Key",),
    )
    provider = OpenAICompatibleProvider(settings, timeout_seconds=5, temperature=0, max_tokens=256)
    request_body = {}

    def fake_urlopen(request, timeout):
        request_body.update(json.loads(request.data.decode("utf-8")))
        return FakeHTTPResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider.complete(
        [{"role": "user", "content": "ping"}],
        response_format={"type": "json_object"},
        reasoning_effort="none",
    )

    assert request_body["response_format"] == {"type": "json_object"}
    assert request_body["reasoning_effort"] == "none"
