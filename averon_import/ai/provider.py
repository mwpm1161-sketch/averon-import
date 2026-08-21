from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from averon_import.ai.config import ProviderSettings


class AiProviderError(RuntimeError):
    pass


@dataclass
class OpenAICompatibleProvider:
    settings: ProviderSettings
    timeout_seconds: float
    temperature: float
    max_tokens: int

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

    def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.configured:
            raise AiProviderError(f"Провайдер {self.label} не настроен")

        url = f"{self.settings.base_url}/chat/completions"
        payload = json.dumps(
            {
                "model": self.settings.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        auth_schemes = self.settings.auth_schemes if self.settings.api_key else ("",)
        last_error: Exception | None = None
        for index, scheme in enumerate(auth_schemes):
            headers = {"Content-Type": "application/json"}
            if self.settings.api_key:
                headers["Authorization"] = f"{scheme} {self.settings.api_key}".strip()
            request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return _extract_message_content(body)
            except urllib.error.HTTPError as exc:
                last_error = exc
                # Yandex AI Studio supports OpenAI-compatible clients. Depending
                # on credential type/configuration, Bearer or Api-Key can be used.
                if exc.code in {401, 403} and index + 1 < len(auth_schemes):
                    continue
                detail = _http_error_detail(exc)
                raise AiProviderError(
                    f"{self.label}: HTTP {exc.code}{': ' + detail if detail else ''}"
                ) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = getattr(exc, "reason", exc)
                raise AiProviderError(f"{self.label}: нет соединения ({reason})") from exc
            except (ValueError, KeyError, TypeError) as exc:
                last_error = exc
                raise AiProviderError(f"{self.label}: некорректный ответ API") from exc

        raise AiProviderError(f"{self.label}: ошибка запроса ({last_error})")


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


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        data = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    if len(data) > 500:
        data = data[:500] + "…"
    return data.strip()
