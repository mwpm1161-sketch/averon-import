from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from averon_import.ai.config import ProviderSettings


class AiProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "request_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


@dataclass
class OpenAICompatibleProvider:
    settings: ProviderSettings
    timeout_seconds: float
    temperature: float
    max_tokens: int
    last_call_diagnostics: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def key(self) -> str:
        return self.settings.key

    @property
    def label(self) -> str:
        return self.settings.label

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        if not self.configured:
            raise AiProviderError(
                f"Провайдер {self.label} не настроен",
                category="access_denied",
            )

        payload_data: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload_data["response_format"] = _validate_response_format(response_format)
        if reasoning_effort is not None:
            if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError("unsupported reasoning_effort")
            payload_data["reasoning_effort"] = reasoning_effort

        url = f"{self.settings.base_url}/chat/completions"
        payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")

        auth_schemes = self.settings.auth_schemes if self.settings.api_key else ("",)
        last_error: Exception | None = None
        for index, scheme in enumerate(auth_schemes):
            headers = {"Content-Type": "application/json"}
            if self.settings.api_key:
                headers["Authorization"] = f"{scheme} {self.settings.api_key}".strip()
            request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_body = response.read()
                    self.last_call_diagnostics = _response_diagnostics(
                        response,
                        raw_body,
                        latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                    body = json.loads(raw_body.decode("utf-8"))
                return _extract_message_content(body)
            except urllib.error.HTTPError as exc:
                last_error = exc
                # Yandex AI Studio supports OpenAI-compatible clients. Depending
                # on credential type/configuration, Bearer or Api-Key can be used.
                self.last_call_diagnostics = {
                    "http_status": int(exc.code),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_category": "access_denied" if exc.code in {401, 403} else "response_error",
                }
                if exc.code in {401, 403} and index + 1 < len(auth_schemes):
                    continue
                raise AiProviderError(
                    f"{self.label}: HTTP {exc.code}",
                    category=self.last_call_diagnostics["error_category"],
                    status_code=int(exc.code),
                ) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = getattr(exc, "reason", None)
                category = "timeout" if _looks_like_timeout(reason) else "request_error"
                self.last_call_diagnostics = {
                    "http_status": None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_category": category,
                }
                message = "превышено время ожидания" if category == "timeout" else "ошибка соединения"
                raise AiProviderError(
                    f"{self.label}: {message}",
                    category=category,
                ) from exc
            except (TimeoutError, ValueError, KeyError, TypeError) as exc:
                last_error = exc
                category = "timeout" if isinstance(exc, TimeoutError) else "response_error"
                self.last_call_diagnostics = {
                    "http_status": None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error_category": category,
                }
                if isinstance(exc, TimeoutError):
                    raise AiProviderError(
                        f"{self.label}: превышено время ожидания",
                        category=category,
                    ) from exc
                raise AiProviderError(
                    f"{self.label}: некорректный ответ API",
                    category=category,
                ) from exc

        raise AiProviderError(
            f"{self.label}: ошибка запроса",
            category="request_error",
        ) from last_error


def _extract_message_content(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise KeyError("choices")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    raise TypeError("message.content")


def _response_diagnostics(response: Any, raw_body: bytes, *, latency_ms: float) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "http_status": int(getattr(response, "status", 0) or response.getcode()),
        "latency_ms": latency_ms,
        "content_type": str((getattr(response, "headers", {}) or {}).get("Content-Type", "")),
        "body_bytes": len(raw_body),
    }
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        diagnostics["error_category"] = "response_error"
        return diagnostics
    if not isinstance(body, dict):
        diagnostics["error_category"] = "response_error"
        return diagnostics
    choices = body.get("choices") or []
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    diagnostics.update({
        "finish_reason": first.get("finish_reason"),
        "message_content_type": type(content).__name__,
        "message_content_length": len(content) if isinstance(content, (str, list, dict)) else None,
        "reasoning_fields": sorted(
            key for key in set(body) | set(message)
            if "reason" in str(key).casefold()
        ),
        "usage": {
            str(key): value for key, value in (body.get("usage") or {}).items()
            if isinstance(value, (int, float, str))
        } if isinstance(body.get("usage"), dict) else None,
    })
    return diagnostics


def _validate_response_format(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("response_format must be an object")
    format_type = value.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type != "json_schema" or not isinstance(value.get("json_schema"), dict):
        raise ValueError("unsupported response_format")
    schema_value = value["json_schema"]
    name = schema_value.get("name")
    schema = schema_value.get("schema")
    if not isinstance(name, str) or not name or not isinstance(schema, dict):
        raise ValueError("invalid json_schema response_format")
    result = {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": schema,
        },
    }
    for key in ("description", "strict"):
        if key in schema_value:
            result["json_schema"][key] = schema_value[key]
    return result


def _looks_like_timeout(value: object) -> bool:
    if isinstance(value, TimeoutError):
        return True
    return "timed out" in str(value or "").casefold() or "timeout" in str(value or "").casefold()
